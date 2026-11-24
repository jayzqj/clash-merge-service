"""Security review S4: FastAPI docs and OpenAPI schema are disabled."""
import copy
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import pipeline, web
from app.config import Config, DEFAULTS


class DocsDisabledTests(unittest.TestCase):
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

    def test_docs_endpoints_are_gone(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)


if __name__ == "__main__":
    unittest.main()
