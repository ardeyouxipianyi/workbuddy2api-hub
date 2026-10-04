"""Miniprogram (mp) task channel (M3 C7).

Run with: python _test_mp_tasks.py
No upstream credentials or outbound network are used.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="mp-tasks-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_desktop
import wb_taskqueue
import wb_tasks


class FakeResponse(object):
    def __init__(self, payload=None):
        self.payload = json.dumps(payload if payload is not None
                                  else {"code": 0, "msg": "", "data": {}}).encode("utf-8")

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeAccount(object):
    def __init__(self, uid="uid-mp", realm="cn"):
        self.uid = uid
        self.nickname = "Nick"
        self.realm = realm
        self.enabled = True
        self.access_token = "tok"
        self.proxy = ""
        self.enterprise_id = ""

    def headers(self, purpose="chat"):
        return {"Authorization": "Bearer tok", "X-User-Id": self.uid}


def mp_task(code, status="accepted", current=0, target=1, **extra):
    item = {"task_code": code, "name": code, "status": status,
            "accept_status": status, "current": current, "target": target,
            "locked": False, "claimable": False, "claimed": False,
            "unforgeable": False}
    item.update(extra)
    return item


class MpEventTests(unittest.TestCase):
    def test_mp_fingerprint(self):
        fp = wb_desktop.mp_event_base(FakeAccount())
        self.assertEqual(fp["ideType"], "WorkBuddy_MP")
        self.assertEqual(fp["extName"], "workbuddy-mp")
        self.assertEqual(fp["platform"], "mini_program")
        self.assertEqual(fp["machineId"], wb_desktop.MP_MACHINE_ID)

    def test_report_mp_events_headers_and_body(self):
        captured = {}

        def fake_urlopen(req, timeout=30, proxy=""):
            captured["url"] = req.full_url
            captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse()

        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake_urlopen
        try:
            ok = wb_desktop.report_mp_events(
                FakeAccount(), [{"eventCode": "chat_request_send"}])
        finally:
            wb_accounts.urlopen = old
        self.assertTrue(ok)
        self.assertEqual(captured["url"], "https://www.codebuddy.cn/v2/report")
        self.assertEqual(captured["headers"].get("x-client-platform"), "mp-weixin")
        self.assertEqual(captured["headers"].get("x-client-product"), "workbuddy-mp")
        self.assertEqual(captured["body"][0]["eventCode"], "chat_request_send")
        self.assertEqual(captured["body"][0]["platform"], "mini_program")

    def test_mp_chat_event_variants(self):
        base = wb_desktop.mp_chat_event("conv-1")
        self.assertEqual(base["eventCode"], "chat_request_send")
        self.assertEqual(base["agentName"], "mp")
        self.assertEqual(base["conversationId"], "conv-1")
        self.assertNotIn("activityId", base)
        with_activity = wb_desktop.mp_chat_event("conv-1", activity_id="school_open_day_2026")
        self.assertEqual(with_activity["activityId"], "school_open_day_2026")
        with_model = wb_desktop.mp_chat_event("conv-1", model_id="glm-5.2",
                                              model_name="GLM-5.2")
        self.assertEqual(with_model["requestModelId"], "glm-5.2")
        self.assertEqual(with_model["requestModelName"], "GLM-5.2")

    def test_mp_expert_and_playbook_events(self):
        expert = wb_desktop.mp_expert_event("ex_abc", "专家")
        self.assertEqual(expert["eventCode"], "expert_actual_use")
        self.assertEqual(expert["extVersion"], "2.2.8")
        self.assertEqual(expert["source"], "mini_program")
        self.assertEqual(expert["type"], "send_message")
        playbook = wb_desktop.mp_playbook_events("case", "Case")
        self.assertEqual([e["eventCode"] for e in playbook],
                         ["playbook_cta_click", "playbook_prompt_send"])
        self.assertTrue(all(e["extVersion"] == "2.2.8" for e in playbook))


class MpApiHeaderTests(unittest.TestCase):
    def capture(self):
        captured = {}

        def fake_urlopen(req, timeout=30, proxy=""):
            captured["url"] = req.full_url
            captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
            captured["body"] = req.data
            return FakeResponse()

        return captured, fake_urlopen

    def test_fetch_tasks_mp_header(self):
        captured, fake = self.capture()
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            wb_tasks.fetch_growth_tasks(FakeAccount(), mp=True)
        finally:
            wb_accounts.urlopen = old
        self.assertEqual(captured["headers"].get("x-client-platform"), "miniprogram")

    def test_fetch_tasks_default_has_no_mp_header(self):
        captured, fake = self.capture()
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            wb_tasks.fetch_growth_tasks(FakeAccount())
        finally:
            wb_accounts.urlopen = old
        self.assertNotIn("x-client-platform", captured["headers"])

    def test_accept_and_claim_mp_headers(self):
        captured, fake = self.capture()
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake
        try:
            wb_tasks.accept_tasks(FakeAccount(), ["Sequential_Tasks_1"], mp=True)
            accept_headers = dict(captured["headers"])
            wb_tasks.claim_task(FakeAccount(), "Sequential_Tasks_1", mp=True)
            claim_headers = dict(captured["headers"])
        finally:
            wb_accounts.urlopen = old
        self.assertEqual(accept_headers.get("x-client-platform"), "miniprogram")
        self.assertEqual(claim_headers.get("x-client-platform"), "miniprogram")


class SequentialRunnerTests(unittest.TestCase):
    def setUp(self):
        self.account = FakeAccount()
        self.sleep_patch = mock.patch.object(wb_tasks.time, "sleep", return_value=None)
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def test_mp_task_not_offered_is_a_clean_skip(self):
        with mock.patch.object(wb_tasks, "fetch_growth_tasks", return_value=[]):
            ok, message, _credit = wb_tasks.run_sequential_task(
                self.account, "Sequential_Tasks_1")
        self.assertTrue(ok)
        self.assertIn("未下发", message)

    def test_claimed_and_locked_skip(self):
        with mock.patch.object(wb_tasks, "fetch_growth_tasks",
                               return_value=[mp_task("Sequential_Tasks_1", claimed=True)]):
            ok, message, _credit = wb_tasks.run_sequential_task(
                self.account, "Sequential_Tasks_1")
        self.assertTrue(ok)
        self.assertIn("已领取", message)
        with mock.patch.object(wb_tasks, "fetch_growth_tasks",
                               return_value=[mp_task("Sequential_Tasks_1", locked=True)]):
            ok, message, _credit = wb_tasks.run_sequential_task(
                self.account, "Sequential_Tasks_1")
        self.assertTrue(ok)
        self.assertIn("未解锁", message)

    def test_accept_failure_waits_for_the_next_unlock(self):
        with mock.patch.object(wb_tasks, "fetch_growth_tasks",
                               return_value=[mp_task("Sequential_Tasks_1",
                                                     status="not_accepted")]), \
                mock.patch.object(wb_tasks, "accept_tasks",
                                  return_value={"accepted": [], "failed": ["Sequential_Tasks_1"],
                                                "msg": "locked"}):
            ok, message, _credit = wb_tasks.run_sequential_task(
                self.account, "Sequential_Tasks_1")
        self.assertTrue(ok)
        self.assertIn("accept 未登记生效", message)

    def test_full_flow_reports_and_claims(self):
        calls = {"fetch": 0}

        def fetch(account, mp=False):
            calls["fetch"] += 1
            if calls["fetch"] == 1:
                return [mp_task("Sequential_Tasks_1", status="not_accepted",
                                current=0, target=1)]
            return [mp_task("Sequential_Tasks_1", status="completed",
                            current=1, target=1)]

        with mock.patch.object(wb_tasks, "fetch_growth_tasks", side_effect=fetch), \
                mock.patch.object(wb_tasks, "accept_tasks",
                                  return_value={"accepted": ["Sequential_Tasks_1"],
                                                "failed": []}), \
                mock.patch.object(wb_tasks, "_report_sequential_events",
                                  return_value=(True, "sent")), \
                mock.patch.object(wb_tasks, "claim_task",
                                  return_value={"ok": True, "credit": 100}):
            ok, message, credit = wb_tasks.run_sequential_task(
                self.account, "Sequential_Tasks_1")
        self.assertTrue(ok)
        self.assertEqual(credit, 100)
        self.assertIn("sent", message)

    def test_progress_not_reached_reports(self):
        with mock.patch.object(wb_tasks, "fetch_growth_tasks",
                               return_value=[mp_task("Sequential_Tasks_3",
                                                     current=0, target=5)]), \
                mock.patch.object(wb_tasks, "_report_sequential_events",
                                  return_value=(True, "sent")), \
                mock.patch.object(wb_tasks, "claim_task") as claim:
            ok, message, _credit = wb_tasks.run_sequential_task(
                self.account, "Sequential_Tasks_3")
        self.assertFalse(ok)
        self.assertIn("未点亮", message)
        claim.assert_not_called()

    def test_run_single_task_dispatches_sequential_codes(self):
        with mock.patch.object(wb_tasks, "run_sequential_task",
                               return_value=(True, "mp", 5)) as runner:
            result = wb_tasks.run_single_task(self.account, "Sequential_Tasks_2")
        self.assertEqual(result, (True, "mp", 5))
        runner.assert_called_once()


class QueueMpScanTests(unittest.TestCase):
    def test_scan_merges_mp_only_tasks(self):
        class FakePool(object):
            accounts = [FakeAccount()]

        queue = wb_taskqueue.TaskQueue(FakePool(), runner=lambda a, c: (True, "", 0))

        def fetch(account, mp=False):
            if mp:
                return [mp_task("Sequential_Tasks_1", current=0, target=1),
                        mp_task("chat_5", current=0, target=5)]
            return [mp_task("chat_5", current=0, target=5)]

        with mock.patch.object(wb_taskqueue.wb_tasks, "fetch_growth_tasks",
                               side_effect=fetch):
            result = queue.scan()
        codes = [t["task_code"] for t in result["accounts"][0]["growth"]]
        self.assertEqual(codes, ["chat_5", "Sequential_Tasks_1"])


if __name__ == "__main__":
    unittest.main()
