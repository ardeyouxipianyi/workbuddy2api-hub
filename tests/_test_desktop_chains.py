"""Desktop event chains (M3 C2) and their wb_tasks wiring.

Run with: python _test_desktop_chains.py
No upstream credentials or outbound network are used.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="desktop-chains-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_desktop
import wb_tasks


class FakeResponse(object):
    def __init__(self, payload=None):
        self.payload = json.dumps(payload if payload is not None
                                  else {"code": 0, "msg": "", "data": None}).encode("utf-8")

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def make_account(realm="cn", uid="uid-cn"):
    return wb_accounts.Account({"uid": uid, "accessToken": "tok",
                                "realm": realm, "nickname": "Nick"})


class EventSequenceTests(unittest.TestCase):
    def test_derive_id_is_stable_and_36_hex(self):
        account = make_account()
        one = wb_desktop.derive_desktop_id(account, "machine")
        two = wb_desktop.derive_desktop_id(account, "machine")
        other = wb_desktop.derive_desktop_id(account, "session")
        self.assertEqual(one, two)
        self.assertNotEqual(one, other)
        self.assertEqual(len(one), 36)
        int(one, 16)

    def test_fingerprint_fields(self):
        fp = wb_desktop.desktop_fingerprint(make_account())
        self.assertEqual(fp["extName"], "workbuddy-desktop")
        self.assertEqual(fp["ideType"], "WorkBuddy")
        self.assertEqual(len(fp["machineId"]), 36)
        self.assertEqual(fp["userId"], "uid-cn")

    def test_chat_sequence_codes(self):
        events = wb_desktop.chat_sequence("conv", "req", "msg", "fast-model", "fast-model")
        self.assertEqual([e["eventCode"] for e in events], [
            "agent_task_created", "chat_message_send", "chat_request_send",
            "chat_message_response", "chat_message_status", "chat_request_response"])
        self.assertEqual(events[0]["conversationId"], "conv")
        self.assertEqual(events[2]["codebuddy.conversation_request_id"], "req")

    def test_buddy_sequence_codes(self):
        events = wb_desktop.buddy_app_sequence()
        self.assertEqual([e["eventCode"] for e in events], [
            "buddyapp_discover_click", "buddyapp_show", "buddyapp_enter_click",
            "buddyapp_auth_confirm_click", "buddyapp_bindaccount_skip_click"])
        self.assertTrue(all(e["buddyId"] == "cb_y5Dy46tPQGGWtueMxXbe" for e in events))

    def test_template_sequence_is_chat_chain_plus_two(self):
        events = wb_desktop.template_use_sequence("c", "r", "3", "竞品分析")
        self.assertEqual(len(events), 8)
        self.assertEqual(events[-2]["eventCode"], "agent_task_created_with_template")
        self.assertEqual(events[-1]["eventCode"], "template_used")

    def test_playbook_sequence_is_chat_chain_plus_three(self):
        events = wb_desktop.playbook_prompt_sequence("c", "r", "case", "Case")
        self.assertEqual(len(events), 9)
        self.assertEqual(events[-1]["eventCode"], "playbook_prompt_send")
        self.assertEqual(events[-1]["conversationId"], "c")

    def test_canvas_sequence_is_chat_chain_plus_two(self):
        events = wb_desktop.design_canvas_sequence("c", "r")
        self.assertEqual(len(events), 8)
        self.assertEqual(events[-2]["eventCode"], "wbx_design_canvas_task_create")
        self.assertEqual(events[-1]["eventCode"], "wbx_design_canvas_open")

    def test_single_event_payloads(self):
        automation = wb_desktop.automation_create_event()
        self.assertEqual(automation["eventCode"], "automated_task_create_suc")
        self.assertEqual(automation["mode"], "LOCAL")
        skin = wb_desktop.appearance_skin_event()
        self.assertEqual(skin["eventCode"], "appearance_skin_apply")
        self.assertEqual(skin["id"], "theme-tkmw7j")


class ReportTests(unittest.TestCase):
    def test_report_desktop_events_merges_fingerprint(self):
        captured = {}

        def fake_urlopen(req, timeout=30, proxy=""):
            captured["url"] = req.full_url
            captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse()

        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake_urlopen
        try:
            ok = wb_desktop.report_desktop_events(
                make_account(), [{"eventCode": "x", "custom": 1}])
        finally:
            wb_accounts.urlopen = old
        self.assertTrue(ok)
        self.assertEqual(captured["url"], "https://copilot.tencent.com/v2/report")
        self.assertEqual(len(captured["body"]), 1)
        event = captured["body"][0]
        self.assertEqual(event["eventCode"], "x")
        self.assertEqual(event["custom"], 1)
        self.assertEqual(event["extName"], "workbuddy-desktop")
        self.assertEqual(captured["headers"].get("x-product"), "SaaS")

    def test_report_web_event_uses_the_web_origin(self):
        captured = {}

        def fake_urlopen(req, timeout=30, proxy=""):
            captured["url"] = req.full_url
            captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse()

        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake_urlopen
        try:
            ok = wb_desktop.report_web_event(
                make_account(), "web_element_click",
                "https://www.workbuddy.cn/space/d/doc", "library_doc_intro_click",
                "WorkBuddy资料库介绍")
        finally:
            wb_accounts.urlopen = old
        self.assertTrue(ok)
        self.assertEqual(captured["url"], "https://www.workbuddy.cn/v2/report")
        self.assertEqual(captured["headers"].get("x-client-platform"), "web")
        self.assertEqual(captured["body"][0]["elementId"], "library_doc_intro_click")

    def test_set_appearance_theme_body(self):
        captured = {}

        def fake_urlopen(req, timeout=30, proxy=""):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse()

        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake_urlopen
        try:
            ok = wb_desktop.set_appearance_theme(make_account(), "theme-tkmw7j")
        finally:
            wb_accounts.urlopen = old
        self.assertTrue(ok)
        self.assertEqual(captured["url"],
                         "https://copilot.tencent.com/v2/user-asset/appearance/set")
        self.assertEqual(captured["body"],
                         {"kind": "theme", "resource_key": "theme-tkmw7j"})


class TaskWiringTests(unittest.TestCase):
    def test_desktop_only_list_no_longer_blocks_the_ported_tasks(self):
        for code in ("RichMeow_Chat", "Library_read", "Buddy_App", "Buddy_App_QQ"):
            self.assertNotIn(code, wb_tasks.DESKTOP_ONLY_TASKS)
            self.assertIn(code, wb_tasks.DESKTOP_ACTIONS)

    def test_buddy_runner_sends_the_five_event_chain(self):
        sent = []
        with mock.patch.object(wb_desktop, "report_desktop_events",
                               side_effect=lambda account, events: sent.append(events) or True):
            ok, detail = wb_tasks.run_desktop_buddy_app(make_account(), 1)
        self.assertTrue(ok)
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(sent[0]), 5)

    def test_template_runner_sends_five_groups(self):
        sent = []
        with mock.patch.object(wb_desktop, "report_desktop_events",
                               side_effect=lambda account, events: sent.append(events) or True):
            ok, _detail = wb_tasks.run_desktop_template(make_account(), 5)
        self.assertTrue(ok)
        self.assertEqual(len(sent), 5)
        self.assertTrue(all(len(events) == 8 for events in sent))
        self.assertEqual([events[-2]["id"] for events in sent], ["1", "2", "3", "4", "5"])

    def test_library_runner_uses_the_web_report(self):
        calls = []
        with mock.patch.object(wb_desktop, "report_web_event",
                               side_effect=lambda *args, **kwargs: calls.append(args) or True):
            ok, _detail = wb_tasks.run_desktop_library(make_account(), 1)
        self.assertTrue(ok)
        self.assertEqual(calls[0][1], "web_element_click")
        self.assertIn("library_doc_intro_click", calls[0])

    def test_richmeow_runner_sends_one_chat_chain(self):
        sent = []
        with mock.patch.object(wb_desktop, "report_desktop_events",
                               side_effect=lambda account, events: sent.append(events) or True):
            ok, _detail = wb_tasks.run_desktop_richmeow(make_account(), 1)
        self.assertTrue(ok)
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(sent[0]), 6)


if __name__ == "__main__":
    unittest.main()
