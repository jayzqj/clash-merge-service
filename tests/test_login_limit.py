"""Security review S2: failed admin logins are throttled per client IP."""
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


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class LoginLimiterUnitTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.limiter = web.LoginLimiter(max_failures=3, lock_seconds=60, clock=self.clock)

    def test_locks_after_consecutive_failures(self):
        self.assertFalse(self.limiter.fail("1.2.3.4"))
        self.assertFalse(self.limiter.fail("1.2.3.4"))
        self.assertEqual(self.limiter.locked("1.2.3.4"), 0)
        self.assertTrue(self.limiter.fail("1.2.3.4"))
        self.assertEqual(self.limiter.locked("1.2.3.4"), 60)
        self.assertEqual(self.limiter.locked("5.6.7.8"), 0)

    def test_lock_expires(self):
        for _ in range(3):
            self.limiter.fail("1.2.3.4")
        self.clock.now += 59.5
        self.assertEqual(self.limiter.locked("1.2.3.4"), 1)
        self.clock.now += 1
        self.assertEqual(self.limiter.locked("1.2.3.4"), 0)
        self.assertFalse(self.limiter.fail("1.2.3.4"))

    def test_success_resets_counter(self):
        self.limiter.fail("1.2.3.4")
        self.limiter.fail("1.2.3.4")
        self.limiter.reset("1.2.3.4")
        self.assertFalse(self.limiter.fail("1.2.3.4"))
        self.assertFalse(self.limiter.fail("1.2.3.4"))

    def test_tracking_is_bounded(self):
        with patch.object(web, "_LOGIN_TRACKED_MAX", 10):
            for i in range(50):
                self.limiter.fail(f"10.0.0.{i}")
            self.assertLessEqual(len(self.limiter._entries), 10)


class LoginLimitWebTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {"CLASH_MERGE_DESKTOP": "0"}))
        data = copy.deepcopy(DEFAULTS)
        data.update(notify_enabled=False)
        cfg = Config(data=data, path=root / "config.json")
        self.clock = FakeClock()
        self.stack.enter_context(patch.object(web, "CFG", cfg))
        self.stack.enter_context(patch.object(web, "STATE", pipeline.ServiceState()))
        self.stack.enter_context(patch.object(web, "_admin_password", "preview"))
        self.stack.enter_context(patch.object(
            web, "_LOGIN_LIMITER", web.LoginLimiter(max_failures=5, lock_seconds=900, clock=self.clock)))
        self.client = TestClient(web.app)
        self.addCleanup(self.client.close)

    def get(self, password, ip="203.0.113.7"):
        return self.client.get("/api/status", auth=(web.ADMIN_USER, password),
                               headers={"X-Real-IP": ip})

    def test_lockout_blocks_even_correct_password(self):
        with self.assertLogs("app.web", level="WARNING") as logs:
            for _ in range(5):
                self.assertEqual(self.get("wrong").status_code, 401)
        self.assertTrue(any("203.0.113.7" in line for line in logs.output))
        self.assertFalse(any("wrong" in line for line in logs.output))
        locked = self.get("preview")
        self.assertEqual(locked.status_code, 429)
        self.assertTrue(int(locked.headers["Retry-After"]) > 0)
        # Another client is unaffected.
        self.assertEqual(self.get("preview", ip="198.51.100.2").status_code, 200)
        self.clock.now += 901
        self.assertEqual(self.get("preview").status_code, 200)

    def test_success_clears_failures(self):
        for _ in range(4):
            self.get("wrong")
        self.assertEqual(self.get("preview").status_code, 200)
        for _ in range(4):
            self.assertEqual(self.get("wrong").status_code, 401)
        self.assertEqual(self.get("preview").status_code, 200)

    def test_missing_credentials_do_not_count(self):
        for _ in range(10):
            self.assertEqual(self.client.get("/api/status").status_code, 401)
        self.assertEqual(self.get("preview").status_code, 200)

    def test_forwarded_ip_trusted_only_from_loopback(self):
        class Req:
            def __init__(self, host, header):
                self.client = type("C", (), {"host": host})()
                self.headers = {"x-real-ip": header}

        self.assertEqual(web._client_ip(Req("127.0.0.1", "203.0.113.7")), "203.0.113.7")
        self.assertEqual(web._client_ip(Req("192.0.2.9", "203.0.113.7")), "192.0.2.9")
        self.assertEqual(web._client_ip(Req("127.0.0.1", "")), "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
