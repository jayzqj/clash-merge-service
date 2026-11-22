import asyncio
from contextlib import ExitStack
import copy
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import runtime, web, processes, pipeline, fetcher
from app.config import Config, DEFAULTS
from app.purity import MihomoChecker


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {
            "CLASH_MERGE_DESKTOP": "1", "CLASH_MERGE_DATA_DIR": str(self.root),
            "CLASH_MERGE_CONFIG": str(self.root / "config.json"),
            "CLASH_MERGE_DESKTOP_TOKEN": "admin-test-token", "CLASH_MERGE_SUB_TOKEN": "subscription-test-token",
            "PORT": "18080"}))
        self.cfg = Config.load()
        self.stack.enter_context(patch.object(web, "CFG", self.cfg))
        self.stack.enter_context(patch.object(web, "STATE", pipeline.ServiceState()))
        self.client = TestClient(web.app, base_url="http://127.0.0.1:18080")
        self.addCleanup(self.client.close)

    def login(self):
        return self.client.get("/desktop/open?token=admin-test-token")

    def test_platform_defaults_are_isolated(self):
        self.assertEqual(self.cfg.output_path, str(self.root / "jay_filtered.yaml"))
        self.assertFalse(self.cfg.notify_enabled)
        self.assertEqual(self.cfg.subscriptions, [])
        self.assertEqual(self.cfg.data["mihomo_mixed_port"], 17890)
        with patch.dict(os.environ, {"CLASH_MERGE_DESKTOP": "0"}):
            linux = Config()
        self.assertEqual(linux.data["mihomo_workdir"], DEFAULTS["mihomo_workdir"])
        self.cfg.data["subscriptions"].append({"url": "example"})
        self.assertEqual(DEFAULTS["subscriptions"], [])

    def test_session_csrf_host_and_subscription_boundaries(self):
        self.assertEqual(self.client.get("/api/config").status_code, 401)
        self.assertEqual(self.client.get("/desktop/open?token=bad").status_code, 403)
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.client.get("/api/config").status_code, 200)
        self.assertEqual(self.client.post("/api/run").status_code, 403)
        self.assertEqual(self.client.get("/api/config", headers={"Host": "evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/config", headers={"Origin": "https://evil.example"}).status_code, 403)
        Path(self.cfg.output_path).write_text("proxies: []\n", encoding="utf-8")
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/subscription/bad/jay_filtered.yaml").status_code, 404)
        response = self.client.get("/subscription/subscription-test-token/jay_filtered.yaml")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, Path(self.cfg.output_path).read_bytes())

    def test_front_save_only_redacts_credentials(self):
        self.login()
        front = {"name": "test", "type": "socks5", "server": "127.0.0.1", "port": 7897, "password": "must-not-echo"}
        with patch.object(web, "_start_run") as start:
            response = self.client.put("/api/config", json={"run_now": False, "check_front": front, "check_front_enabled": True}, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(response.status_code, 200, response.text)
        start.assert_not_called()
        self.assertEqual(Config.load().check_front["password"], "must-not-echo")
        self.assertNotIn("must-not-echo", self.client.get("/api/config").text)
        response = self.client.put("/api/config", json={"run_now": False, "check_front": None, "check_front_enabled": False}, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(Config.load().check_front)

    def test_autorun_requires_session_and_calls_os_setting(self):
        headers = {"X-Requested-With": "XMLHttpRequest"}
        self.assertEqual(self.client.put("/api/desktop/autorun", json={"enabled": True}, headers=headers).status_code, 401)
        self.login()
        with patch.object(runtime, "set_autorun") as setter, patch.object(runtime, "autorun_enabled", return_value=True):
            response = self.client.put("/api/desktop/autorun", json={"enabled": True}, headers=headers)
            self.assertEqual(response.json(), {"ok": True, "enabled": True})
            setter.assert_called_once_with(True)

    def test_fetch_defaults_ignore_environment_proxy_on_desktop(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://example.invalid:9999"}), patch.object(fetcher.httpx, "AsyncClient") as factory:
            fetcher.make_client(cfg=self.cfg)
            self.assertIsNone(factory.call_args.kwargs["proxy"])
            self.cfg.data["fetch_proxy_url"] = "http://127.0.0.1:7897"
            fetcher.make_client(cfg=self.cfg)
            self.assertEqual(factory.call_args.kwargs["proxy"], "http://127.0.0.1:7897")
            with patch.dict(os.environ, {"CLASH_MERGE_DESKTOP": "0"}):
                fetcher.make_client()
            self.assertEqual(factory.call_args.kwargs["proxy"], "http://example.invalid:9999")

    def test_occupied_controller_never_attaches_to_existing_process(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            self.cfg.data["mihomo_controller_port"] = occupied.getsockname()[1]
            with patch("app.purity.shutil.which", return_value=sys.executable), patch("app.purity.start_process") as start:
                self.assertFalse(MihomoChecker(self.cfg).start([]))
                start.assert_not_called()


class ProcessTests(unittest.TestCase):
    def test_timeout_reaps_child(self):
        children = []
        original = processes.start_process
        def capture(*args, **kwargs):
            proc = original(*args, **kwargs)
            children.append(proc)
            return proc
        with patch.object(processes, "start_process", side_effect=capture):
            with self.assertRaises(subprocess.TimeoutExpired):
                processes.run_process([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.1)
        self.assertIsNotNone(children[0].poll())
        self.assertNotIn(children[0], processes._children)

    def test_failed_validation_preserves_last_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = Config(data=copy.deepcopy(DEFAULTS), path=root / "config.json")
            cfg.data["output_path"] = str(root / "published.yaml")
            target = Path(cfg.output_path)
            target.write_text("old file", encoding="utf-8")
            with patch.object(pipeline, "run_process", side_effect=subprocess.TimeoutExpired("mihomo", 120)):
                with self.assertRaises(fetcher.FetchError) as caught:
                    asyncio.run(pipeline._write_output(cfg, {"proxies": []}))
            self.assertEqual(caught.exception.step, "validate")
            self.assertEqual(target.read_text(), "old file")
            self.assertFalse((root / "output.tmp.yaml").exists())


if __name__ == "__main__":
    unittest.main()
