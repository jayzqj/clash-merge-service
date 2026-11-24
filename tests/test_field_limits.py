"""Security review S7 (field length/count bounds) and S8 (webhook must be https)."""
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


class WebhookHttpsTests(unittest.TestCase):
    def _cfg(self, webhook):
        data = copy.deepcopy(DEFAULTS)
        data.update(notify_enabled=True, wechat_webhook=webhook)
        return Config(data=data, path=Path(tempfile.mkdtemp()) / "config.json")

    def test_https_webhook_ok(self):
        errs = self._cfg("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc").validate()
        self.assertFalse([e for e in errs if "wechat_webhook" in e])

    def test_http_webhook_rejected(self):
        errs = self._cfg("http://qyapi.weixin.qq.com/send?key=abc").validate()
        self.assertTrue(any("wechat_webhook" in e for e in errs))

    def test_garbage_webhook_rejected(self):
        errs = self._cfg("not a url").validate()
        self.assertTrue(any("wechat_webhook" in e for e in errs))

    def test_oversized_check_front_rejected(self):
        cfg = self._cfg("https://qyapi.weixin.qq.com/send?key=abc")
        cfg.data["check_front"] = {"name": "f", "type": "socks5", "server": "1.2.3.4",
                                   "port": 1080, "padding": "x" * 9000}
        self.assertTrue(any("check_front" in e for e in cfg.validate()))
        cfg.data["check_front"]["padding"] = "x" * 10
        self.assertFalse([e for e in cfg.validate() if "check_front" in e])


class FieldLimitWebTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {"CLASH_MERGE_DESKTOP": "0"}))
        data = copy.deepcopy(DEFAULTS)
        data.update(notify_enabled=False)
        self.cfg = Config(data=data, path=root / "config.json")
        self.stack.enter_context(patch.object(web, "CFG", self.cfg))
        self.stack.enter_context(patch.object(web, "STATE", pipeline.ServiceState()))
        self.stack.enter_context(patch.object(web, "_admin_password", "preview"))
        self.stack.enter_context(patch.object(web, "_start_run", return_value=False))
        self.stack.enter_context(patch.object(web, "reschedule"))
        self.client = TestClient(web.app)
        self.addCleanup(self.client.close)
        self.client.auth = (web.ADMIN_USER, "preview")
        self.client.headers["X-Requested-With"] = "XMLHttpRequest"

    def test_overlong_url_rejected(self):
        resp = self.client.put("/api/config", json={"run_now": False,
                                                     "ini_url": "https://x/" + "a" * 4000})
        self.assertEqual(resp.status_code, 400)
        errors = resp.json()["errors"]
        self.assertTrue(any("ini_url" in e and "2048" in e for e in errors), errors)
        # The rejected value is never echoed back.
        self.assertNotIn("a" * 100, resp.text)

    def test_too_many_excluded_protocols_rejected(self):
        resp = self.client.put("/api/config", json={"run_now": False,
                                                     "excluded_protocols": ["ss"] * 100})
        self.assertEqual(resp.status_code, 400)
        errors = resp.json()["errors"]
        self.assertTrue(any("excluded_protocols" in e and "64" in e for e in errors), errors)

    def test_overlong_subscription_url_rejected(self):
        resp = self.client.post("/api/subscriptions", json={"url": "https://x/" + "a" * 4000})
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(any(e.startswith("url：") for e in resp.json()["errors"]))

    def test_wrong_type_is_400_with_errors(self):
        resp = self.client.put("/api/config", json={"run_now": "yes"})
        self.assertEqual(resp.status_code, 400)
        self.assertIsInstance(resp.json()["errors"], list)
        self.assertTrue(resp.json()["errors"])

    def test_subscription_count_cap(self):
        with patch.object(web, "MAX_SUBSCRIPTIONS", 2):
            self.cfg.data["subscriptions"] = [
                {"id": "a", "url": "vless://x@1.1.1.1:443?security=none#a", "enabled": True},
                {"id": "b", "url": "vless://x@1.1.1.2:443?security=none#b", "enabled": True}]
            resp = self.client.post("/api/subscriptions",
                                    json={"url": "vless://x@1.1.1.3:443?security=none#c"})
            self.assertEqual(resp.status_code, 400)
            self.assertIn("上限", resp.json()["detail"])


if __name__ == "__main__":
    unittest.main()
