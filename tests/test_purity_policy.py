"""Filter by current or historical scores without bypassing availability rules."""
import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml
from fastapi.testclient import TestClient

from app import pipeline, web
from app.availability import load_availability, update_availability
from app.config import Config, DEFAULTS
from app.fetcher import FetchError
from app.merge import dedup_key, prefix_purity_names, sort_by_purity
from app.purity_history import apply_history, filter_by_purity


def node(index=0, **extra):
    return {"name": f"日本 {index}", "type": "ss", "server": f"node{index}.invalid",
            "port": 443, "cipher": "aes-128-gcm", "password": "test-secret", **extra}


def result(p, score=None, attempts=3):
    return {dedup_key(p): {"measured": score is not None, "score": score,
                          "attempts": attempts, "country": "JP"}}


class ConfigFixture:
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        data = copy.deepcopy(DEFAULTS)
        data.update(notify_enabled=False, output_path=str(self.root / "out.yaml"),
                    execution_start="00:00", execution_end="23:59",
                    subscriptions=[{"id": "test", "url": "https://example.invalid/sub"}])
        self.cfg = Config(data=data, path=self.root / "config.json")


class HistoryTests(ConfigFixture, unittest.TestCase):
    def test_failure_reuses_score_across_restart_and_rename_without_resetting_failures(self):
        p = node()
        apply_history(self.cfg, [p], result(p, 5))
        self.cfg.save()
        cfg = Config.load(self.cfg.path)
        renamed = dict(p, name="日本 renamed")
        for failures in (1, 2, 3):
            current = result(renamed)
            history = apply_history(cfg, [renamed], current)
            rec = history[dedup_key(p)]
            self.assertEqual(rec["score"], 5)
            self.assertFalse(rec["measured"])
            self.assertIsNone(current[dedup_key(p)]["score"])
            kept, _ = update_availability(cfg, load_availability(cfg), [renamed], history)
            self.assertEqual(len(kept), int(failures < 3))
            self.assertEqual(load_availability(cfg)[dedup_key(p)]["fails"], failures)
        apply_history(cfg, [p], result(p, 40))
        rec = apply_history(cfg, [p], result(p))[dedup_key(p)]
        self.assertEqual(rec["score"], 40)
        text = (self.root / "purity_history.json").read_text()
        self.assertNotIn("test-secret", text)
        self.assertNotIn(p["server"], text)

    def test_connection_changes_do_not_reuse_history(self):
        p = node()
        apply_history(self.cfg, [p], result(p, 0))
        changed = dict(p, password="changed-secret")
        rec = apply_history(self.cfg, [changed], result(changed))[dedup_key(changed)]
        self.assertIsNone(rec["score"])

    def test_bootstrap_from_published_scores_and_preserve_zero(self):
        p = node()
        Path(self.cfg.output_path).write_text(yaml.safe_dump({
            "proxies": [dict(p, name="0% 日本 old", **{"dialer-proxy": "front"})]}))
        rec = apply_history(self.cfg, [p], result(p, attempts=0))[dedup_key(p)]
        self.assertEqual(rec["score"], 0)
        self.assertFalse(rec["measured"])
        self.assertEqual(rec["attempts"], 0)

    def test_bad_cache_does_not_stop_current_measurements(self):
        p = node()
        (self.root / "purity_history.json").write_text("broken JSON")
        rec = apply_history(self.cfg, [p], result(p, 7))[dedup_key(p)]
        self.assertEqual(rec["score"], 7)
        self.assertTrue(rec["measured"])

    def test_filter_uses_history_including_boundary_and_zero(self):
        ps = [node(i) for i in range(5)]
        records = {}
        for p, score in zip(ps, [0, 15, 16, 5, None]):
            records.update(result(p, score))
        apply_history(self.cfg, ps, records)
        records.update(result(ps[3]))
        records = apply_history(self.cfg, ps, records)
        self.cfg.data.update(purity_filter_enabled=True, purity_threshold=15)
        kept, stats = filter_by_purity(self.cfg, ps, records)
        self.assertEqual(kept, [ps[0], ps[1], ps[3]])
        self.assertEqual(stats["purity_filter_excluded"], 2)
        self.cfg.data["purity_threshold"] = 0
        self.assertEqual(filter_by_purity(self.cfg, ps, records)[0], ps[:1])
        self.cfg.data["purity_filter_enabled"] = False
        kept, _ = filter_by_purity(self.cfg, ps, records)
        self.assertEqual(kept, ps)
        named, stats = prefix_purity_names(sort_by_purity(kept, records), records)
        self.assertEqual([p["name"].split()[0] for p in named[:4]], ["0%", "5%", "15%", "16%"])
        self.assertEqual(stats["purity_reused"], 1)
        self.assertEqual(stats["unmeasured_kept"], 1)


class SettingsTests(ConfigFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg.save()
        stack = ExitStack()
        self.addCleanup(stack.close)
        for key, value in {"CFG": self.cfg, "STATE": pipeline.ServiceState(),
                           "_admin_password": "preview"}.items():
            stack.enter_context(patch.object(web, key, value))
        self.start = stack.enter_context(patch.object(web, "_start_run", return_value=True))
        stack.enter_context(patch.object(web, "reschedule"))
        self.client = TestClient(web.app)
        self.addCleanup(self.client.close)
        self.client.auth = (web.ADMIN_USER, "preview")
        self.client.headers["X-Requested-With"] = "XMLHttpRequest"

    def test_default_save_reload_disable_and_decimal_threshold(self):
        self.assertFalse(self.client.get("/api/config").json()["purity_filter_enabled"])
        for threshold in (0, 15, 15.5, 100):
            r = self.client.put("/api/config", json={
                "purity_filter_enabled": True, "purity_threshold": threshold})
            self.assertEqual(r.status_code, 200, r.text)
            saved = Config.load(self.cfg.path).data
            self.assertTrue(saved["purity_filter_enabled"])
            self.assertEqual(saved["purity_threshold"], threshold)
        r = self.client.put("/api/config", json={"purity_filter_enabled": False})
        self.assertEqual(r.status_code, 200)
        saved = self.client.get("/api/config").json()
        self.assertFalse(saved["purity_filter_enabled"])
        self.assertEqual(saved["purity_threshold"], 100)

    def test_invalid_settings_never_mutate_live_or_saved_config(self):
        original = copy.deepcopy(self.cfg.data)
        on_disk = self.cfg.path.read_bytes()
        for body in [{"purity_threshold": x} for x in (-1, 101, "15%", "", True)] + [
            {"purity_enabled": False, "purity_filter_enabled": True}]:
            r = self.client.put("/api/config", json=body)
            self.assertIn(r.status_code, (400, 422), r.text)
            self.assertEqual(self.cfg.data, original)
            self.assertEqual(self.cfg.path.read_bytes(), on_disk)
        self.start.assert_not_called()


class PipelineTests(ConfigFixture, unittest.IsolatedAsyncioTestCase):
    async def run_round(self, ps, records, during_check=None):
        async def write(cfg, doc):
            Path(cfg.output_path).write_text(yaml.safe_dump(doc), encoding="utf-8")

        def check(_):
            if during_check:
                during_check()
            return records

        ini = "[custom]\ncustom_proxy_group=🚀 手动选择`select`.*\nruleset=🚀 手动选择,[]FINAL\n"
        with patch.object(pipeline, "fetch_subscription", AsyncMock(return_value=copy.deepcopy(ps))), \
             patch.object(pipeline, "fetch_text", AsyncMock(return_value=ini)), \
             patch.object(pipeline, "MihomoChecker") as checker, \
             patch.object(pipeline, "_write_output", side_effect=write):
            checker.return_value.check_all.side_effect = check
            report = await pipeline.run_pipeline(self.cfg, pipeline.ServiceState())
        self.assertTrue(report["ok"], report)
        return report, yaml.safe_load(Path(self.cfg.output_path).read_text(encoding="utf-8"))

    async def test_full_round_reuses_then_filters_and_restores_on_success(self):
        ps = [node(i) for i in range(3)]
        initial = {k: v for p, score in zip(ps, [5, 15, 70]) for k, v in result(p, score).items()}
        await self.run_round(ps, initial)
        failed = {**initial, **result(ps[0])}
        report, doc = await self.run_round(ps, failed)
        self.assertEqual(report["purity_reused"], 1)
        self.assertEqual(report["purity_measured"], 2)
        self.assertTrue(doc["proxies"][0]["name"].startswith("5% "))
        self.cfg.data.update(purity_filter_enabled=True, purity_threshold=15)
        report, doc = await self.run_round(ps, failed)
        self.assertEqual(report["purity_reused"], 1)
        self.assertEqual([p["server"] for p in doc["proxies"]], [ps[0]["server"], ps[1]["server"]])
        report, doc = await self.run_round(ps, initial)
        self.assertEqual(len(doc["proxies"]), 2)

    async def test_deliberate_small_and_empty_output_are_published(self):
        ps = [node(i) for i in range(30)]
        records = {k: v for p in ps for k, v in result(p, 70).items()}
        await self.run_round(ps, records)
        self.cfg.data.update(purity_filter_enabled=True, purity_threshold=15,
                             use_remote_subconverter=True)
        records.update(result(ps[0], 5))
        report, doc = await self.run_round(ps, records)
        self.assertEqual(len(doc["proxies"]), 1)
        self.assertEqual(report["purity_filter_excluded"], 29)
        records.update(result(ps[0], 70))
        report, doc = await self.run_round(ps, records)
        self.assertEqual(doc["proxies"], [])
        self.assertEqual(report["purity_filter_excluded"], 30)
        names = {g["name"] for g in doc["proxy-groups"]} | {"DIRECT", "REJECT"}
        for group in doc["proxy-groups"]:
            self.assertTrue(set(group["proxies"]) <= names)

    async def test_failed_checks_use_history_until_availability_omits_them(self):
        p = node()
        await self.run_round([p], result(p, 5))
        self.cfg.data["purity_filter_enabled"] = True
        for failures in (1, 2, 3):
            report, doc = await self.run_round([p], result(p))
            self.assertEqual(len(doc["proxies"]), int(failures < 3))
        self.assertEqual(report["availability_omitted"], 1)

    async def test_missing_or_high_history_is_excluded_and_current_score_wins(self):
        ps = [node(i) for i in range(3)]
        await self.run_round(ps, {**result(ps[0], 70), **result(ps[1], 5), **result(ps[2])})
        self.cfg.data["purity_filter_enabled"] = True
        report, doc = await self.run_round(ps, {
            **result(ps[0]), **result(ps[1], 40), **result(ps[2])})
        self.assertEqual(doc["proxies"], [])
        self.assertEqual(report["purity_filter_excluded"], 3)

    async def test_mid_round_settings_save_waits_for_next_round(self):
        p = node()
        def change():
            self.cfg.data.update(purity_filter_enabled=True, purity_threshold=15)
        _, doc = await self.run_round([p], result(p, 70), during_check=change)
        self.assertEqual(len(doc["proxies"]), 1)
        _, doc = await self.run_round([p], result(p, 70))
        self.assertEqual(doc["proxies"], [])

    async def test_success_notification_uses_round_filter_and_actual_next_schedule(self):
        self.cfg.data.update(notify_enabled=True, wechat_webhook="https://example.invalid/webhook",
                             purity_filter_enabled=True, purity_threshold=15)
        def change_interval():
            self.cfg.data.update(interval_seconds=7200, purity_threshold=30)
        p = node()
        with patch.object(pipeline, "send_wechat", AsyncMock(return_value=True)) as send:
            report, _ = await self.run_round([p], result(p, 5), during_check=change_interval)
        message = send.await_args.args[1]
        self.assertIn("纯净度过滤：开启（≤15%）", message)
        self.assertIn("执行间隔：120 分钟", message)
        self.assertIn("执行时段：每天 00:00–23:59（北京时间）", message)
        self.assertIn(f"下次执行时间：{report['next_run']}（北京时间）", message)
        self.assertRegex(message, r"本轮用时：\d+(?:\.\d+)? 分钟")
        self.assertIn("后置节点：0 个\n", message)
        for removed in ("TCP", "UDP", "dialer-proxy", "发送方", " 秒"):
            self.assertNotIn(removed, message)
        persisted = json.loads((self.root / "state.json").read_text())
        self.assertEqual(persisted["next_run"], report["next_run"])
        self.assertEqual(persisted["last_run"]["interval_seconds"], 7200)

    async def test_early_failure_notification_includes_settings_and_next_schedule(self):
        self.cfg.data.update(subscriptions=[], notify_enabled=True,
                             wechat_webhook="https://example.invalid/webhook",
                             purity_filter_enabled=False, purity_threshold=12.5)
        state = pipeline.ServiceState()
        with patch.object(pipeline, "send_wechat", AsyncMock(return_value=True)) as send:
            report = await pipeline.run_pipeline(self.cfg, state)
        self.assertFalse(report["ok"])
        message = send.await_args.args[1]
        self.assertIn("失败步骤：fetch", message)
        self.assertIn("纯净度过滤：关闭（配置值：12.5%）", message)
        self.assertIn("执行间隔：60 分钟", message)
        self.assertIn(f"下次执行时间：{state.next_run}（北京时间）", message)
        self.assertRegex(message, r"本轮用时：\d+(?:\.\d+)? 分钟")
        self.assertNotIn("发送方", message)

    def test_guard_still_rejects_unexpected_empty_and_source_collapse(self):
        Path(self.cfg.output_path).write_text(yaml.safe_dump({"proxies": [node(i) for i in range(100)]}))
        for doc, report in [({"proxies": []}, {}), ({"proxies": [node()]}, {}),
                            ({"proxies": []}, {"purity_filter_enabled": True,
                             "purity_filter_excluded": 3, "purity_filter_kept": 0})]:
            with self.assertRaises(FetchError):
                pipeline._guard_output(self.cfg, doc, report)


if __name__ == "__main__":
    unittest.main()
