"""linux.do group: the ini defines groups and rules, the service fills the url-test members."""
import copy
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml
from fastapi.testclient import TestClient

from app import pipeline, web
from app.config import Config, DEFAULTS
from app.converter import build_proxy_groups, build_rules, parse_ini
from app.groups import fill_linuxdo_group
from app.merge import dedup_key


def node(index=0, **extra):
    return {"name": f"日本 {index}", "type": "ss", "server": f"node{index}.invalid",
            "port": 443, "cipher": "aes-128-gcm", "password": "test-secret", **extra}


def result(p, score=None, connected=True):
    return {dedup_key(p): {"connected": connected, "measured": score is not None, "score": score,
                          "attempts": 3, "country": "JP"}}


class ConfigFixture:
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "output").mkdir()
        data = copy.deepcopy(DEFAULTS)
        data.update(notify_enabled=False, output_path=str(self.root / "output" / "out.yaml"),
                    execution_start="00:00", execution_end="23:59",
                    subscriptions=[{"id": "test", "url": "https://example.invalid/sub"}])
        self.cfg = Config(data=data, path=self.root / "config.json")

INI = "\n".join([
    "[custom]",
    "ruleset=🐧 LinuxDo,[]DOMAIN-SUFFIX,linux.do",
    "ruleset=🐧 LinuxDo,[]DOMAIN,challenges.cloudflare.com",
    "ruleset=🚀 手动选择,[]FINAL",
    "custom_proxy_group=🚀 手动选择`select`.*",
    "custom_proxy_group=🐧 LinuxDo`select`[]🐧 LinuxDo 自动`[]🚀 手动选择`[]DIRECT",
    "custom_proxy_group=🐧 LinuxDo 自动`url-test`REJECT`https://linux.do/cdn-cgi/trace`300,,200",
    "",
])


def doc_with_groups():
    ini = parse_ini(INI)
    return {"proxy-groups": build_proxy_groups([], ini)}


def group(doc, name):
    return next(g for g in doc["proxy-groups"] if g["name"] == name)


class FillTests(unittest.TestCase):
    def setUp(self):
        self.proxies = [{"name": n, "type": t} for n, t in
                        [("a", "ss"), ("b", "vless"), ("c", "ss"), ("d", "ss"), ("e", "http")]]
        self.scores = {"a": 5, "b": 15, "c": 16, "d": None, "e": 3}

    def test_members_by_threshold_inclusive_and_outer_untouched(self):
        doc = doc_with_groups()
        outer_before = copy.deepcopy(group(doc, "🐧 LinuxDo"))
        stats = fill_linuxdo_group(doc, self.proxies, self.scores, 15)
        self.assertEqual(group(doc, "🐧 LinuxDo 自动")["proxies"], ["a", "b"])
        self.assertEqual(group(doc, "🐧 LinuxDo"), outer_before)
        self.assertEqual(stats, {"linuxdo_group": "🐧 LinuxDo 自动", "linuxdo_members": 2,
                                 "linuxdo_threshold": 15})
        fill_linuxdo_group(doc, self.proxies, self.scores, 15.5)
        self.assertEqual(group(doc, "🐧 LinuxDo 自动")["proxies"], ["a", "b"])
        fill_linuxdo_group(doc, self.proxies, self.scores, 100)
        self.assertEqual(group(doc, "🐧 LinuxDo 自动")["proxies"], ["a", "b", "c"])

    def test_no_qualifying_node_keeps_reject(self):
        doc = doc_with_groups()
        stats = fill_linuxdo_group(doc, self.proxies, self.scores, 0)
        self.assertEqual(group(doc, "🐧 LinuxDo 自动")["proxies"], ["REJECT"])
        self.assertEqual(stats["linuxdo_members"], 0)

    def test_missing_group_is_reported_and_nothing_changes(self):
        doc = {"proxy-groups": [{"name": "🐧 LinuxDo", "type": "select", "proxies": ["DIRECT"]}]}
        stats = fill_linuxdo_group(doc, self.proxies, self.scores, 15)
        self.assertEqual(stats["linuxdo_group"], "")
        self.assertEqual(stats["linuxdo_members"], 0)
        self.assertEqual(doc["proxy-groups"][0]["proxies"], ["DIRECT"])

    def test_ini_test_parameters_and_rule_order(self):
        inner = group(doc_with_groups(), "🐧 LinuxDo 自动")
        self.assertEqual(inner["type"], "url-test")
        self.assertEqual(inner["url"], "https://linux.do/cdn-cgi/trace")
        self.assertEqual((inner["interval"], inner["tolerance"]), (300, 200))
        self.assertNotIn("timeout", inner)  # mihomo default (docs/adr/0012)
        _, rules = build_rules(parse_ini(INI))
        self.assertEqual(rules[:2], ["DOMAIN-SUFFIX,linux.do,🐧 LinuxDo",
                                     "DOMAIN,challenges.cloudflare.com,🐧 LinuxDo"])


class PipelineLinuxDoTests(ConfigFixture, unittest.IsolatedAsyncioTestCase):
    async def run_with(self, coro_factory, ps, records):
        async def write(cfg, doc):
            Path(cfg.output_path).write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")

        with patch.object(pipeline, "fetch_subscription", AsyncMock(return_value=copy.deepcopy(ps))), \
             patch.object(pipeline, "fetch_text", AsyncMock(return_value=INI)), \
             patch.object(pipeline, "MihomoChecker") as checker, \
             patch.object(pipeline, "_write_output", side_effect=write):
            checker.return_value.check_all.side_effect = lambda _: records
            report = await coro_factory()
        self.assertTrue(report["ok"], report)
        return report, yaml.safe_load(Path(self.cfg.output_path).read_text(encoding="utf-8"))

    def servers(self, doc):
        by_name = {p["name"]: p["server"] for p in doc["proxies"]}
        return [by_name[n] for n in group(doc, "🐧 LinuxDo 自动")["proxies"] if n in by_name]

    async def test_full_round_and_reapply_use_current_threshold(self):
        ps = [node(i) for i in range(4)]
        records = {k: v for p, s in zip(ps, [5, 15, 40, None]) for k, v in result(p, s).items()}
        state = pipeline.ServiceState()
        report, doc = await self.run_with(lambda: pipeline.run_pipeline(self.cfg, state), ps, records)
        self.assertEqual(self.servers(doc), [ps[0]["server"], ps[1]["server"]])
        self.assertEqual(report["linuxdo_members"], 2)
        self.assertEqual(doc["rules"][0], "DOMAIN-SUFFIX,linux.do,🐧 LinuxDo")
        self.assertEqual(group(doc, "🐧 LinuxDo")["proxies"], ["🐧 LinuxDo 自动", "🚀 手动选择", "DIRECT"])

        self.cfg.data["linuxdo_purity_threshold"] = 5
        report, doc = await self.run_with(lambda: pipeline.reapply_rules(self.cfg, state), ps, records)
        self.assertEqual(self.servers(doc), [ps[0]["server"]])
        self.assertEqual(report["linuxdo_threshold"], 5)

        self.cfg.data["linuxdo_purity_threshold"] = 0
        report, doc = await self.run_with(lambda: pipeline.reapply_rules(self.cfg, state), ps, records)
        self.assertEqual(group(doc, "🐧 LinuxDo 自动")["proxies"], ["REJECT"])


class LinuxDoSettingsTests(ConfigFixture, unittest.TestCase):
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

    def test_default_save_and_reload(self):
        self.assertEqual(self.client.get("/api/config").json()["linuxdo_purity_threshold"], 15)
        for value in (0, 12.5, 100):
            r = self.client.put("/api/config", json={"linuxdo_purity_threshold": value})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(Config.load(self.cfg.path).data["linuxdo_purity_threshold"], value)

    def test_invalid_values_rejected_without_mutation(self):
        original = copy.deepcopy(self.cfg.data)
        for value in (-1, 101, "15", True):
            r = self.client.put("/api/config", json={"linuxdo_purity_threshold": value})
            self.assertIn(r.status_code, (400, 422), r.text)
            self.assertEqual(self.cfg.data, original)
        self.start.assert_not_called()
