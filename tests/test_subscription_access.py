"""Output-path confinement and the token-protected Linux subscription endpoint."""
import asyncio
import copy
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import notify, pipeline, runtime, web
from app.config import Config, DEFAULTS
from app.fetcher import FetchError

TOKEN = "A" * 20 + "b-_" + "9" * 20


class LinuxFixture:
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {
            "CLASH_MERGE_DESKTOP": "0", "CLASH_MERGE_SUB_TOKEN": TOKEN,
            "CLASH_MERGE_PUBLIC_BASE": "https://sub.example.org/"}))
        data = copy.deepcopy(DEFAULTS)
        data.update(notify_enabled=False)
        self.cfg = Config(data=data, path=self.root / "config.json")


class OutputPathTests(LinuxFixture, unittest.TestCase):
    def test_default_is_inside_output_dir(self):
        self.assertEqual(Path(self.cfg.output_path), self.root / "output" / "jay_filtered.yaml")
        self.assertEqual(self.cfg.output_path_errors(), [])

    def test_loaded_config_without_output_path_gets_default(self):
        self.cfg.path.write_text('{"subscriptions": []}', encoding="utf-8")
        loaded = Config.load(self.cfg.path)
        self.assertEqual(Path(loaded.output_path), self.root / "output" / "jay_filtered.yaml")

    def test_rejects_paths_outside_output_dir(self):
        out = self.root / "output"
        bad = [
            "", "relative.yaml", str(self.root / "config.json"), str(self.root / "x.yaml"),
            str(out / ".." / "x.yaml"), str(out / "sub" / "x.yaml"), str(out / "x.yml"),
            str(out / "output.tmp.yaml"), str(out / ".hidden.yaml"), str(out / "a b.yaml"),
            "/etc/cron.d/x.yaml", "/root/.ssh/authorized_keys",
        ]
        for value in bad:
            with self.subTest(value=value):
                self.cfg.data["output_path"] = value
                self.assertTrue(self.cfg.output_path_errors())
                self.assertTrue(any("output_path" in e for e in self.cfg.validate()))

    def test_rejects_symlinked_target(self):
        out = self.root / "output"
        out.mkdir()
        target = out / "jay_filtered.yaml"
        try:
            target.symlink_to(self.root / "config.json")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.cfg.data["output_path"] = str(target)
        self.assertTrue(self.cfg.output_path_errors())

    def test_write_refuses_bad_path_before_touching_disk(self):
        victim = self.root / "victim.yaml"
        victim.write_text("keep", encoding="utf-8")
        self.cfg.data["output_path"] = str(victim)
        with self.assertRaises(FetchError) as caught:
            asyncio.run(pipeline._write_output(self.cfg, {"proxies": []}))
        self.assertEqual(caught.exception.step, "write")
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep")
        self.assertFalse((self.root / "output.tmp.yaml").exists())


class TokenTests(unittest.TestCase):
    def test_token_created_once_with_private_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            first = runtime.ensure_subscription_token(directory)
            self.assertTrue(runtime.valid_token(first))
            self.assertEqual(runtime.ensure_subscription_token(directory), first)
            if os.name != "nt":
                mode = (Path(directory) / runtime.TOKEN_FILE).stat().st_mode & 0o777
                self.assertEqual(mode, 0o600)

    def test_invalid_token_file_refuses_to_start(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / runtime.TOKEN_FILE).write_text("short", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                runtime.ensure_subscription_token(directory)

    def test_subscription_url_from_public_base(self):
        with patch.dict(os.environ, {"CLASH_MERGE_DESKTOP": "0", "CLASH_MERGE_SUB_TOKEN": TOKEN,
                                     "CLASH_MERGE_PUBLIC_BASE": "https://sub.example.org/"}):
            self.assertEqual(runtime.subscription_url(),
                             f"https://sub.example.org/subscription/{TOKEN}/jay_filtered.yaml")
        for base in ("", "http://sub.example.org", "https://sub.example.org/path",
                     "https://user@sub.example.org"):
            with self.subTest(base=base), patch.dict(os.environ, {
                    "CLASH_MERGE_DESKTOP": "0", "CLASH_MERGE_SUB_TOKEN": TOKEN,
                    "CLASH_MERGE_PUBLIC_BASE": base}):
                self.assertEqual(runtime.subscription_url(), "")

    def test_notification_contains_full_url(self):
        url = f"https://sub.example.org/subscription/{TOKEN}/jay_filtered.yaml"
        self.assertIn(url, notify.success_text({"subscription_url": url}))
        self.assertIn("CLASH_MERGE_PUBLIC_BASE", notify.success_text({"subscription_url": ""}))
        self.assertNotIn("xiuyuan", notify.success_text({}))


class LinuxWebTests(LinuxFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.stack.enter_context(patch.object(web, "CFG", self.cfg))
        self.stack.enter_context(patch.object(web, "STATE", pipeline.ServiceState()))
        self.stack.enter_context(patch.object(web, "_admin_password", "preview"))
        self.stack.enter_context(patch.object(web, "_start_run", return_value=False))
        self.stack.enter_context(patch.object(web, "reschedule"))
        self.client = TestClient(web.app)
        self.addCleanup(self.client.close)

    def admin(self):
        self.client.auth = (web.ADMIN_USER, "preview")
        self.client.headers["X-Requested-With"] = "XMLHttpRequest"

    def test_subscription_requires_exact_token(self):
        out = Path(self.cfg.output_path)
        out.parent.mkdir(parents=True)
        out.write_text("proxies: []\n", encoding="utf-8")
        ok = self.client.get(f"/subscription/{TOKEN}/jay_filtered.yaml")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.content, out.read_bytes())
        self.assertEqual(ok.headers["cache-control"], "no-store")
        for bad in ("x", TOKEN[:-1], TOKEN + "x", TOKEN.lower(), "%E4%B8%AD" + TOKEN):
            with self.subTest(bad=bad):
                self.assertEqual(self.client.get(f"/subscription/{bad}/jay_filtered.yaml").status_code, 404)
        self.assertEqual(self.client.get("/jay_filtered.yaml").status_code, 404)

    def test_subscription_404_without_server_token(self):
        out = Path(self.cfg.output_path)
        out.parent.mkdir(parents=True)
        out.write_text("proxies: []\n", encoding="utf-8")
        with patch.dict(os.environ, {"CLASH_MERGE_SUB_TOKEN": ""}):
            self.assertEqual(self.client.get("/subscription//jay_filtered.yaml").status_code, 404)
            self.assertEqual(self.client.get(f"/subscription/{TOKEN}/jay_filtered.yaml").status_code, 404)

    def test_subscription_not_served_from_bad_output_path(self):
        victim = self.root / "config.json"
        victim.write_text("{}", encoding="utf-8")
        self.cfg.data["output_path"] = str(victim)
        self.assertEqual(self.client.get(f"/subscription/{TOKEN}/jay_filtered.yaml").status_code, 404)

    def test_api_output_path_is_read_only(self):
        self.admin()
        view = self.client.get("/api/config").json()
        self.assertEqual(view["output_path"], self.cfg.output_path)
        self.assertEqual(view["subscription_url"],
                         f"https://sub.example.org/subscription/{TOKEN}/jay_filtered.yaml")
        before = copy.deepcopy(self.cfg.data)
        r = self.client.put("/api/config", json={"output_path": "/etc/cron.d/evil.yaml", "run_now": False})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.cfg.data, before)
        self.assertFalse(self.cfg.path.exists())
        r = self.client.put("/api/config", json={"output_path": self.cfg.output_path, "run_now": False})
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
