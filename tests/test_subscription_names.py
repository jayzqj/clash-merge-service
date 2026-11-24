"""Subscription display names must persist without running the pipeline."""
import copy
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import Config, DEFAULTS
from app.pipeline import ServiceState
from app import web


class SubscriptionNameTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        data = copy.deepcopy(DEFAULTS)
        data["subscriptions"] = [
            {"id": "legacy", "url": "https://legacy.example.invalid/sub",
             "enabled": True}]
        self.cfg = Config(data=data, path=Path(temp.name) / "config.json")
        self.state = ServiceState(next_run="2030-01-01 12:00:00")
        for key, value in {"CFG": self.cfg, "STATE": self.state,
                           "_admin_password": "preview"}.items():
            p = patch.object(web, key, value)
            p.start()
            self.addCleanup(p.stop)
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.start = stack.enter_context(patch.object(web, "_start_run", return_value=True))
        self.reschedule = stack.enter_context(patch.object(web, "reschedule"))
        self.client = TestClient(web.app)
        self.addCleanup(self.client.close)
        self.client.auth = (web.ADMIN_USER, "preview")
        self.client.headers["X-Requested-With"] = "XMLHttpRequest"

    def test_create_optional_name_and_keep_legacy_subscription(self):
        for index, extra in enumerate(({}, {"name": "  主用机场  "})):
            response = self.client.post("/api/subscriptions", json={
                "url": f"https://example.invalid/sub/{index}", **extra})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["run_started"])
        stored = Config.load(self.cfg.path).subscriptions
        self.assertNotIn("name", stored[0])
        self.assertEqual([s["name"] for s in stored[1:]], ["", "主用机场"])
        self.assertEqual(self.client.get("/api/config").json()["subscriptions"], stored)

    def test_add_share_links_and_save_stability_settings_without_run(self):
        response = self.client.post('/api/subscriptions', json={'url': 'vless://fixture@192.0.2.1:443?security=none#sample'})
        self.assertEqual(response.status_code, 200)
        self.start.reset_mock()
        self.cfg.data['notify_enabled'] = False
        response = self.client.put('/api/config', json={'run_now':False, 'connectivity_rounds':5, 'excluded_protocols':['vless','vmess']})
        self.assertEqual(response.status_code, 200, response.text)
        self.start.assert_not_called()
        stored = Config.load(self.cfg.path)
        self.assertEqual(stored.data['connectivity_rounds'], 5)
        self.assertEqual(stored.data['excluded_protocols'], ['vless','vmess'])
        response = self.client.patch('/api/subscriptions/legacy', json={'url':'vmess://invalid-secret'})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('invalid-secret', response.text)

    def test_summary_does_not_serialize_large_node_lists(self):
        self.state.last_run = {'ok':True, 'proxies_out':12, 'connectivity_samples':[{'name':'large'}]*5000,
            'prefilter_excluded_names':['large']*5000, 'tested_chains':[{'node':'a','front':'b'}]*10,
            'sources_failed':[{'url':'private-link', 'id':'fixture', 'error':'timeout'}]}
        response = self.client.get('/api/status?summary=true')
        self.assertEqual(response.status_code, 200)
        self.assertLess(len(response.content), 2000)
        last = response.json()['last_run']
        self.assertNotIn('connectivity_samples',last)
        self.assertNotIn('prefilter_excluded_names',last)
        self.assertEqual(last['tested_chains_count'],10)
        self.assertNotIn('private-link',response.text)

    def test_rename_and_clear_without_changing_schedule_or_inputs(self):
        original = self.cfg.subscriptions[0].copy()
        for value in ("  <img src=x onerror=alert(1)>机场  ", " " * 3):
            response = self.client.patch("/api/subscriptions/legacy", json={"name": value})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["run_started"])
            self.assertEqual(response.json()["next_run"], self.state.next_run)
            stored = Config.load(self.cfg.path).subscriptions[0]
            self.assertEqual(stored, {**original, "name": value.strip()})
        self.start.assert_not_called()
        self.reschedule.assert_not_called()

    def test_name_length_validation_is_atomic(self):
        response = self.client.patch("/api/subscriptions/legacy", json={"name": "名" * 100})
        self.assertEqual(response.status_code, 200)
        before = self.cfg.path.read_bytes()
        for body in ({"name": "名" * 101, "enabled": False}, {"name": 123}):
            response = self.client.patch("/api/subscriptions/legacy", json=body)
            self.assertEqual(response.status_code, 400)
            self.assertTrue(response.json()["errors"])
            self.assertEqual(self.cfg.path.read_bytes(), before)
        response = self.client.post("/api/subscriptions", json={
            "url": "https://example.invalid/new", "name": "x" * 101})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.cfg.subscriptions), 1)

    def test_toggle_and_mixed_patch_still_trigger_pipeline(self):
        self.client.patch("/api/subscriptions/legacy", json={"name": "备用"})
        for body in ({"enabled": False}, {"enabled": True, "name": "主用"}):
            response = self.client.patch("/api/subscriptions/legacy", json=body)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["run_started"])
        self.assertEqual(self.start.call_count, 2)
        self.assertEqual(self.reschedule.call_count, 2)
        self.assertEqual(Config.load(self.cfg.path).subscriptions[0]["name"], "主用")

    def test_missing_subscription_and_csrf(self):
        response = self.client.patch("/api/subscriptions/missing", json={"name": "不存在"})
        self.assertEqual(response.status_code, 404)
        del self.client.headers["X-Requested-With"]
        response = self.client.patch("/api/subscriptions/legacy", json={"name": "拒绝"})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("name", self.cfg.subscriptions[0])
        self.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
