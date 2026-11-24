"""Security review S3: CSP + security response headers on admin UI and API."""
import copy
import os
import re
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import pipeline, web
from app.config import Config, DEFAULTS


class SecurityHeaderTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {"CLASH_MERGE_DESKTOP": "0"}))
        data = copy.deepcopy(DEFAULTS)
        data.update(notify_enabled=False)
        cfg = Config(data=data, path=root / "config.json")
        self.stack.enter_context(patch.object(web, "CFG", cfg))
        self.stack.enter_context(patch.object(web, "STATE", pipeline.ServiceState()))
        self.stack.enter_context(patch.object(web, "_admin_password", "preview"))
        self.client = TestClient(web.app)
        self.addCleanup(self.client.close)
        self.client.auth = (web.ADMIN_USER, "preview")
        self.client.headers["X-Requested-With"] = "XMLHttpRequest"

    def test_admin_page_has_csp_with_matching_script_nonce(self):
        resp = self.client.get("/admin")
        self.assertEqual(resp.status_code, 200)
        csp = resp.headers["content-security-policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(resp.headers["x-frame-options"], "DENY")
        self.assertEqual(resp.headers["x-content-type-options"], "nosniff")
        nonce = re.search(r"script-src 'nonce-([^']+)'", csp).group(1)
        # The inline script in the returned HTML carries exactly that nonce,
        # and no unsigned <script> slips through.
        self.assertIn(f'<script nonce="{nonce}">', resp.text)
        self.assertEqual(resp.text.count("<script"), 1)
        self.assertIn("'unsafe-inline'", csp.split("style-src")[1].split(";")[0])
        self.assertNotRegex(csp, r"script-src[^;]*unsafe-inline")

    def test_nonce_differs_per_request(self):
        first = self.client.get("/admin").headers["content-security-policy"]
        second = self.client.get("/admin").headers["content-security-policy"]
        self.assertNotEqual(first, second)

    def test_api_has_headers_and_no_store(self):
        resp = self.client.get("/api/status")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["cache-control"], "no-store")
        self.assertEqual(resp.headers["x-frame-options"], "DENY")
        self.assertIn("content-security-policy", resp.headers)

    def test_headers_present_even_on_401(self):
        bad = TestClient(web.app)
        bad.headers["X-Requested-With"] = "XMLHttpRequest"
        resp = bad.get("/api/status")
        self.assertEqual(resp.status_code, 401)
        self.assertIn("content-security-policy", resp.headers)
        self.assertEqual(resp.headers["x-content-type-options"], "nosniff")
        bad.close()


if __name__ == "__main__":
    unittest.main()
