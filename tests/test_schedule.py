"""Execution windows apply to scheduled and manually triggered full rounds."""
import asyncio
import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from app import pipeline, schedule, web
from app.config import Config, DEFAULTS
from app.notify import success_text, failure_text


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=schedule.CST)


class Fixture:
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cfg = Config(data=copy.deepcopy(DEFAULTS), path=Path(temp.name)/"config.json")
        self.cfg.data["notify_enabled"] = False
        self.cfg.save()


class WindowTests(Fixture, unittest.TestCase):
    def test_defaults_and_minute_boundaries(self):
        for value, allowed in [("07:59:59",False),("08:00:00",True),
                               ("23:59:59",True),("00:00:00",False)]:
            self.assertEqual(schedule.in_execution_window(self.cfg, at("2026-11-18T"+value)),allowed)
        self.assertEqual(Config.load(self.cfg.path).data["execution_start"], "08:00")

    def test_intervals_snap_to_next_opening(self):
        self.cfg.data.update(execution_start="07:00",execution_end="23:00",interval_seconds=7200)
        for now, expected in [("2026-11-18T06:50:00","2026-11-18 07:00:00"),
                              ("2026-11-18T08:00:00","2026-11-18 10:00:00"),
                              ("2026-11-18T22:00:00","2026-11-19 07:00:00"),
                              ("2026-11-18T23:01:00","2026-11-19 07:00:00")]:
            self.assertEqual(schedule.next_execution(self.cfg,at(now)),expected)

    def test_overnight_and_end_boundary(self):
        self.cfg.data.update(execution_start="22:00",execution_end="07:00")
        for value,allowed in [("21:59:59",False),("22:00:00",True),("00:00:00",True),
                              ("07:00:59",True),("07:01:00",False)]:
            self.assertEqual(schedule.in_execution_window(self.cfg,at("2026-11-18T"+value)),allowed)
        self.assertEqual(schedule.next_execution(self.cfg,at("2026-11-18T06:30:00")),"2026-11-18 22:00:00")
        self.assertEqual(schedule.next_execution(self.cfg,at("2026-11-18T23:30:00")),"2026-11-19 00:30:00")

    def test_timezone_is_beijing_not_host_timezone(self):
        utc = datetime(2026,11,18,0,0,tzinfo=timezone.utc)
        self.assertTrue(schedule.in_execution_window(self.cfg,utc))
        self.assertEqual(schedule.next_execution(self.cfg,utc),"2026-11-18 09:00:00")

    def test_startup_clamps_saved_or_overdue_schedule_and_recovers_invalid_time(self):
        path = self.cfg.path.parent / "state.json"
        with patch.object(schedule,"now_cst",return_value=at("2026-11-18T01:00:00")):
            for value in ["2026-11-18 02:00:00","2026-11-17 22:00:00","bad",None]:
                path.write_text(json.dumps({"next_run":value}))
                self.assertEqual(pipeline.load_state(self.cfg).next_run,"2026-11-18 08:00:00")

    def test_reschedule_and_notifications_share_window(self):
        state=pipeline.ServiceState()
        with patch.object(schedule,"now_cst",return_value=at("2026-11-18T23:30:00")), \
             patch.object(pipeline,"_wake_event"):
            pipeline.reschedule(state,self.cfg)
        self.assertEqual(state.next_run,"2026-11-19 08:00:00")
        report={"execution_start":"07:00","execution_end":"23:00","next_run":state.next_run}
        for message in [success_text(report),failure_text("fetch","test",report)]:
            self.assertIn("执行时段：每天 07:00–23:00（北京时间）",message)


class ApiTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.state=pipeline.ServiceState(next_run="2026-11-18 01:00:00")
        for key,value in {"CFG":self.cfg,"STATE":self.state,"_admin_password":"preview"}.items():
            self.stack.enter_context(patch.object(web,key,value))
        self.stack.enter_context(patch.object(schedule,"now_cst",return_value=at("2026-11-18T01:00:00")))
        self.stack.enter_context(patch.object(pipeline,"_wake_event"))
        self.spawn=self.stack.enter_context(patch.object(web,"_spawn"))
        self.client=TestClient(web.app);self.addCleanup(self.client.close)
        self.client.auth=(web.ADMIN_USER,"preview")
        self.client.headers["X-Requested-With"]="XMLHttpRequest"

    def test_save_outside_window_persists_but_does_not_start(self):
        r=self.client.put("/api/config",json={"execution_start":"07:00","execution_end":"23:00"})
        self.assertEqual(r.status_code,200,r.text)
        self.assertFalse(r.json()["run_started"])
        self.assertTrue(r.json()["outside_execution_window"])
        self.assertEqual(r.json()["next_run"],"2026-11-18 07:00:00")
        self.assertEqual(Config.load(self.cfg.path).data["execution_end"],"23:00")
        self.assertEqual(self.client.get("/api/config").json()["execution_start"],"07:00")
        self.spawn.assert_not_called()

    def test_manual_trigger_cannot_bypass_window(self):
        r=self.client.post("/api/run").json()
        self.assertFalse(r["ok"])
        self.assertTrue(r["outside_execution_window"])
        self.assertEqual(r["next_run"],"2026-11-18 08:00:00")
        self.spawn.assert_not_called()

    def test_manual_trigger_inside_window_starts(self):
        self.spawn.side_effect=lambda coro:coro.close()
        with patch.object(schedule,"now_cst",return_value=at("2026-11-18T08:00:00")):
            self.assertTrue(self.client.post("/api/run").json()["ok"])
        self.spawn.assert_called_once()

    def test_invalid_window_rejected_without_mutation(self):
        before=self.cfg.path.read_bytes()
        for body in [{"execution_start":v} for v in ["", "7:00", "24:00", "08:60", "08:00:00",123]] + [
            {"execution_start":"08:00","execution_end":"08:00"}]:
            r=self.client.put("/api/config",json=body)
            self.assertIn(r.status_code,(400,422),r.text)
            self.assertEqual(self.cfg.path.read_bytes(),before)
        self.spawn.assert_not_called()


class RuntimeTests(Fixture,unittest.IsolatedAsyncioTestCase):
    async def test_pipeline_entry_outside_window_does_no_work_or_notification(self):
        state=pipeline.ServiceState(last_run={"ok":True})
        with patch.object(schedule,"now_cst",return_value=at("2026-11-18T03:00:00")), \
             patch.object(pipeline,"fetch_subscription",AsyncMock()) as fetch, \
             patch.object(pipeline,"send_wechat",AsyncMock()) as send:
            result=await pipeline.run_pipeline(self.cfg,state)
        self.assertTrue(result["outside_execution_window"])
        self.assertEqual(state.runs_started,0)
        self.assertEqual(state.last_run,{"ok":True})
        self.assertFalse(state.lock.locked())
        fetch.assert_not_called();send.assert_not_called()

    async def test_scheduler_does_not_run_overdue_task_outside_window(self):
        state=pipeline.ServiceState(next_run="2026-11-17 23:00:00")
        with patch.object(schedule,"now_cst",return_value=at("2026-11-18T02:00:00")), \
             patch.object(pipeline,"datetime") as clock, \
             patch.object(pipeline,"run_pipeline",AsyncMock()) as run, \
             patch.object(pipeline.asyncio,"wait_for",AsyncMock()) as wait:
            clock.now.return_value=at("2026-11-18T02:00:00")
            clock.strptime=datetime.strptime
            async def stop(awaitable,timeout):
                awaitable.close()
                self.assertEqual(timeout,6*3600)
                raise asyncio.CancelledError()
            wait.side_effect=stop
            with self.assertRaises(asyncio.CancelledError):
                await pipeline.scheduler_loop(self.cfg,state)
        self.assertEqual(state.next_run,"2026-11-18 08:00:00")
        run.assert_not_called()

    async def test_running_round_finishes_and_uses_new_window_for_next_run(self):
        self.cfg.data["subscriptions"]=[{"url":"https://example.invalid/sub"}]
        state=pipeline.ServiceState()
        moment=[at("2026-11-18T22:59:00")]
        async def finish_fetch(*args):
            moment[0]=at("2026-11-18T23:10:00")
            self.cfg.data.update(execution_start="07:00",execution_end="23:00")
            raise RuntimeError("fixture fetch failure after end time")
        with patch.object(schedule,"now_cst",side_effect=lambda:moment[0]), \
             patch.object(pipeline,"fetch_subscription",side_effect=finish_fetch):
            report=await pipeline.run_pipeline(self.cfg,state)
        self.assertEqual(state.runs_started,1)
        self.assertEqual(report["failed_step"],"fetch")
        self.assertEqual(report["execution_start"],"07:00")
        self.assertEqual(report["execution_end"],"23:00")
        self.assertEqual(report["next_run"],"2026-11-19 07:00:00")


if __name__=="__main__":
    unittest.main()
