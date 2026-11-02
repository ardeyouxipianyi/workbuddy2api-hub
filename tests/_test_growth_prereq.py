"""first_buddy 前置链：auto 任务先完成并领奖（发放 Buddy），single 任务才能接取。

    python tests/_test_growth_prereq.py

背景（2026-10-09 实测）：上游给成长任务加了 task_type（auto/single）与前置校验。
first_buddy 是 auto 类（不接受接取），它是其他任务与猫猫旅行的前置；而且前置校验
认的是 Buddy 实例 —— 必须完成 first_buddy 并领取其奖励（reward_buddy=true）才发放。
旧流程把它当未接取任务跳过，于是 17 个任务全部 "prerequisite not met: first_buddy"，
旅行也 400 "no active buddy"。
"""
import io
import json
import os
import sys
import unittest
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import wb_tasks as T  # noqa: E402


class FakeAccount(object):
    def __init__(self):
        self.uid = "u-test"
        self.nickname = "tester"
        self.realm = "cn"
        self.proxy = ""
        self.access_token = "tok"
        self.credits = {"remain": 100}

    def headers(self, kind="chat"):
        return {}

    def fetch_credits(self):
        pass


def _task(code, **over):
    t = {"task_code": code, "status": "not_accepted", "current": 0, "target": 1,
         "name": code, "jump_url": "", "task_type": "single"}
    t.update(over)
    return t


class _Resp(object):
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(url, code, body):
    return urllib.error.HTTPError(url, code, "Bad Request", {},
                                  io.BytesIO(body.encode("utf-8")))


class GrowthPrereqTests(unittest.TestCase):
    def setUp(self):
        self._orig = (T.fetch_growth_tasks, T.accept_tasks, T.claim_task,
                      T.report_events, T.do_cat_travel)
        self.events = []
        self.state = {"fb": 0, "cc": "not_accepted"}

    def tearDown(self):
        (T.fetch_growth_tasks, T.accept_tasks, T.claim_task,
         T.report_events, T.do_cat_travel) = self._orig

    def _stub(self):
        def fetch(acc):
            fb_status = {0: "not_accepted", 1: "completed", 2: "claimed"}[self.state["fb"]]
            fb = _task("first_buddy", status=fb_status, current=min(self.state["fb"], 1),
                       target=1, task_type="auto")
            cc = _task("create_canvas", status=self.state["cc"])
            return [fb, cc]
        T.fetch_growth_tasks = fetch

        def report(acc, evs, base=None):
            code = "first_buddy" if evs and evs[0].get("eventCode") == "chat_request_send" else "other"
            self.events.append(("report", code))
            if code == "first_buddy":
                self.state["fb"] = 1
            return True
        T.report_events = report

        def claim(acc, code):
            self.events.append(("claim", code))
            if code == "first_buddy":
                self.state["fb"] = 2
            return {"ok": True, "credit": 300 if code == "first_buddy" else 100}
        T.claim_task = claim

        def accept(acc, codes, chunk=20):
            self.events.append(("accept", tuple(codes)))
            if "create_canvas" in codes:
                self.state["cc"] = "accepted"
            return {"ok": True, "accepted": list(codes), "failed": [], "msg": "",
                    "reasons": []}
        T.accept_tasks = accept
        T.do_cat_travel = lambda acc: {"ok": True, "msg": "ok"}

    def test_auto_task_completed_and_claimed_before_accept(self):
        self._stub()
        res = T.run_growth_tasks(FakeAccount(), gap=0)
        seq = self.events
        first_claim = next(i for i, e in enumerate(seq) if e[0] == "claim")
        first_accept = next(i for i, e in enumerate(seq) if e[0] == "accept")
        self.assertLess(first_claim, first_accept, seq)
        self.assertEqual(seq[first_claim], ("claim", "first_buddy"))
        self.assertEqual(seq[first_accept], ("accept", ("create_canvas",)))
        self.assertGreaterEqual(res["earned_credit"], 300)

    def test_manual_claim_leaves_completed_tasks_unclaimed(self):
        self._stub()
        res = T.run_growth_tasks(FakeAccount(), gap=0, claim=False)
        self.assertFalse([e for e in self.events if e[0] == "claim"], self.events)
        self.assertEqual(res["earned_credit"], 0)
        # first_buddy 已完成但没领奖，状态停在 completed，等用户手动领取。
        self.assertEqual(self.state["fb"], 1)

    def test_specs_updated(self):
        self.assertEqual(T.TASK_SPECS["first_buddy"]["kind"], "chat")
        self.assertEqual(T.TASK_SPECS["first_buddy"]["reward"], 300)
        self.assertTrue(T.TASK_SPECS["wb_wechat_oa_subscribe_task"]["unforgeable"])

    def test_travel_without_buddy_is_friendly(self):
        calls = []
        T.fetch_growth_tasks = lambda acc: []      # 没有 first_buddy，无法补齐前置

        def fake_urlopen(req, timeout=10, proxy=""):
            calls.append(req.full_url)
            if req.full_url.endswith("/travel/status"):
                return _Resp({"code": 0, "data": {"state": "idle", "buddy_id": 0}})
            if req.full_url.endswith("/travel/config"):
                return _Resp({"code": 0, "data": {"locations": [{"id": 1, "name": "咖啡馆"}]}})
            if req.full_url.endswith("/travel/depart"):
                raise _http_error(req.full_url, 400, '{"code":400,"msg":"no active buddy"}')
            return _Resp({"code": 0, "data": {}})
        orig = T._accounts.urlopen
        T._accounts.urlopen = fake_urlopen
        try:
            res = T.do_cat_travel(FakeAccount())
        finally:
            T._accounts.urlopen = orig
        self.assertFalse(res["ok"])
        self.assertIn("Buddy", res["msg"])
        self.assertEqual(sum(1 for u in calls if "/depart" in u), 1, calls)

    def test_travel_completes_first_buddy_and_retries(self):
        calls = []
        state = {"fb": "not_accepted", "departs": 0}

        def fetch(acc):
            return [_task("first_buddy", status=state["fb"],
                          current=1 if state["fb"] != "not_accepted" else 0,
                          target=1, task_type="auto")]
        T.fetch_growth_tasks = fetch

        def report(acc, evs, base=None):
            calls.append("report")
            state["fb"] = "completed"
            return True
        T.report_events = report

        def claim(acc, code):
            calls.append("claim:" + code)
            state["fb"] = "claimed"
            return {"ok": True, "credit": 300}
        T.claim_task = claim

        def fake_urlopen(req, timeout=10, proxy=""):
            calls.append(req.full_url)
            if req.full_url.endswith("/travel/status"):
                return _Resp({"code": 0, "data": {"state": "idle", "buddy_id": 0}})
            if req.full_url.endswith("/travel/config"):
                return _Resp({"code": 0, "data": {"locations": [{"id": 1, "name": "咖啡馆"}]}})
            if req.full_url.endswith("/travel/depart"):
                state["departs"] += 1
                if state["departs"] == 1:
                    raise _http_error(req.full_url, 400,
                                      '{"code":400,"msg":"no active buddy"}')
                return _Resp({"code": 0, "data": {"location": {"name": "咖啡馆"}}})
            return _Resp({"code": 0, "data": {}})
        orig = T._accounts.urlopen
        T._accounts.urlopen = fake_urlopen
        try:
            res = T.do_cat_travel(FakeAccount())
        finally:
            T._accounts.urlopen = orig
        self.assertTrue(res["ok"], res)
        self.assertIn("report", calls)
        self.assertIn("claim:first_buddy", calls)
        self.assertEqual(state["departs"], 2, calls)

    def test_travel_departs_directly_when_buddy_exists(self):
        calls = []
        # status 里 buddy_id 恒为 0（猫在家），不得因此误走补齐前置
        T.fetch_growth_tasks = lambda acc: (_ for _ in ()).throw(
            AssertionError("有 Buddy 时不该走补齐前置"))

        def fake_urlopen(req, timeout=10, proxy=""):
            calls.append(req.full_url)
            if req.full_url.endswith("/travel/status"):
                return _Resp({"code": 0, "data": {"state": "idle", "buddy_id": 0}})
            if req.full_url.endswith("/travel/config"):
                return _Resp({"code": 0, "data": {"locations": [{"id": 1, "name": "咖啡馆"}]}})
            if req.full_url.endswith("/travel/depart"):
                return _Resp({"code": 0, "data": {"location": {"name": "咖啡馆"}}})
            return _Resp({"code": 0, "data": {}})
        orig = T._accounts.urlopen
        T._accounts.urlopen = fake_urlopen
        try:
            res = T.do_cat_travel(FakeAccount())
        finally:
            T._accounts.urlopen = orig
        self.assertTrue(res["ok"], res)
        self.assertTrue(any("/depart" in u for u in calls), calls)

    def test_night_task_accepts_before_report(self):
        calls = []
        state = {"cur": 0}

        def fetch(acc):
            if state["cur"]:
                return [_task("black_cat", status="completed", current=3, target=3)]
            return [_task("black_cat", status="not_accepted", current=0, target=3)]
        T.fetch_growth_tasks = fetch

        def accept(acc, codes, chunk=20):
            calls.append(("accept", tuple(codes)))
            return {"ok": True, "accepted": list(codes), "failed": [], "msg": "",
                    "reasons": []}
        T.accept_tasks = accept

        def report(acc, evs, base=None):
            calls.append(("report",))
            state["cur"] = 3
            return True
        T.report_events = report

        def claim(acc, code):
            calls.append(("claim", code))
            return {"ok": True, "credit": 100}
        T.claim_task = claim

        orig_night = T.in_night_window
        T.in_night_window = lambda: True
        try:
            res = T.run_night_growth(FakeAccount())
        finally:
            T.in_night_window = orig_night
        kinds = [c[0] for c in calls]
        self.assertEqual(kinds[:2], ["accept", "report"], calls)
        self.assertTrue(res["ok"])

    def test_fetch_exposes_task_type(self):
        def fake_urlopen(req, timeout=15, proxy=""):
            return _Resp({"code": 0, "data": {"tasks": [
                {"task_code": "first_buddy", "title": "领取一只 Buddy",
                 "task_type": "auto", "accept_status": "not_accepted", "progress": None},
            ]}})
        orig = T._accounts.urlopen
        T._accounts.urlopen = fake_urlopen
        try:
            tasks = T.fetch_growth_tasks(FakeAccount())
        finally:
            T._accounts.urlopen = orig
        self.assertEqual(tasks[0]["task_type"], "auto")

    def test_valorant_uses_the_registered_expert(self):
        """固定专家的任务必须用登记的真实 id，不能落回通用专家池。"""
        events = []
        state = {"completed": False}

        def fetch(acc):
            status = "completed" if state["completed"] else "accepted"
            return [_task("WorkBuddy_Valorant", status=status,
                          current=1 if state["completed"] else 0, target=1)]

        def report(acc, evs, base=None):
            events.extend(evs)
            state["completed"] = True
            return True

        original = (T.fetch_growth_tasks, T.report_events, T.claim_task)
        T.fetch_growth_tasks = fetch
        T.report_events = report
        T.claim_task = lambda acc, code: {"ok": True, "credit": 100}
        try:
            earned = T._progress_one(
                FakeAccount(), _task("WorkBuddy_Valorant", status="accepted"), [], 0)
        finally:
            (T.fetch_growth_tasks, T.report_events, T.claim_task) = original

        self.assertEqual(events[0]["id"], "ex_xQHHD1u5Jkua")
        self.assertEqual(events[0]["name"], "无畏契约拼豆专家")
        self.assertEqual(earned, 100)


    def test_new_task_specs_registered(self):
        self.assertIn("workstation_expert", T.TASK_SPECS)
        self.assertIn("expert_use_maidehao", T.TASK_SPECS)
        self.assertIn("expert_use_douyinyin", T.TASK_SPECS)
        self.assertIn("expert_use_shangjia_xuanchuan", T.TASK_SPECS)
        self.assertIn("expert_use_bowangwang", T.TASK_SPECS)
        self.assertIn("Sequential_Tasks_7", T.TASK_SPECS)
        self.assertNotIn("RichMeow_Chat", T.DESKTOP_ONLY_TASKS)
        self.assertNotIn("Library_read", T.DESKTOP_ONLY_TASKS)

    def test_dynamic_expert_extraction_and_progress(self):
        task = {
            "task_code": "expert_use_custom",
            "name": "自定义电商专家",
            "jump_url": "https://www.workbuddy.cn/chat?deeplink=workbuddy%3A%2F%2Fexperts%3FexpertId%3Dex_custom999",
            "status": "accepted",
            "current": 0,
            "target": 1,
        }
        expert = T.extract_expert_from_task(task)
        self.assertEqual(expert, ("ex_custom999", "自定义电商专家"))

        events = []
        original = (T.fetch_growth_tasks, T.report_events, T.claim_task)
        T.fetch_growth_tasks = lambda acc: [dict(task, status="completed", current=1)]
        T.report_events = lambda acc, evs, base=None: (events.extend(evs), True)[1]
        T.claim_task = lambda acc, code: {"ok": True, "credit": 100}
        try:
            earned = T._progress_one(FakeAccount(), task, [], 0)
        finally:
            (T.fetch_growth_tasks, T.report_events, T.claim_task) = original
        self.assertEqual(earned, 100)
        self.assertEqual(events[0]["id"], "ex_custom999")

    def test_library_read_builds_web_element_click(self):
        ev = T.build_event(FakeAccount(), "library")
        self.assertEqual(ev["eventCode"], "web_element_click")
        self.assertEqual(ev["elementId"], "library_doc_intro_click")
        self.assertEqual(ev["pageURL"], T.LIB_DOC_URL)

    def test_richmeow_builds_desktop_chat_chain(self):
        evs = T.build_desktop_chat_events(FakeAccount())
        codes = [e["eventCode"] for e in evs]
        self.assertEqual(codes, [
            "agent_task_created", "chat_message_send", "chat_request_send",
            "chat_message_response", "chat_request_response", "agent_task_completed"
        ])

    def test_buddy_open_when_affordable(self):
        calls = []
        def fake_urlopen(req, timeout=10, proxy=""):
            calls.append(req.full_url)
            if "/buddy/quota" in req.full_url:
                return _Resp({"code": 0, "data": {"affordable": 2, "max_open_count": 1}})
            if "/buddy/open" in req.full_url:
                return _Resp({"code": 0, "data": {"buddy": "猫咪学霸"}})
            return _Resp({"code": 0, "data": {}})
        orig = T._accounts.urlopen
        T._accounts.urlopen = fake_urlopen
        try:
            res = T.do_buddy_open(FakeAccount())
        finally:
            T._accounts.urlopen = orig
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "open")
        self.assertEqual(res["opened"], 1)
        self.assertIn("/buddy/quota", calls[0])
        self.assertIn("/buddy/open", calls[1])

    def test_miniprogram_tasks_lifecycle(self):
        calls = []
        mp_tasks_state = [
            {"task_code": "Sequential_Tasks_1", "status": "not_accepted", "current": 0, "target": 1, "title": "小程序对话"}
        ]
        def fake_fetch(acc, mp=False):
            calls.append(("fetch", mp))
            return list(mp_tasks_state)
        def fake_accept(acc, codes, chunk=20, mp=False):
            calls.append(("accept", tuple(codes), mp))
            for t in mp_tasks_state:
                if t["task_code"] in codes:
                    t["status"] = "accepted"
            return {"ok": True, "accepted": list(codes)}
        def fake_report_mp(acc, evs, base=None):
            calls.append(("report_mp", len(evs)))
            for t in mp_tasks_state:
                t["status"] = "completed"
                t["current"] = 1
            return True
        def fake_claim(acc, code, mp=False):
            calls.append(("claim", code, mp))
            return {"ok": True, "credit": 100}

        orig = (T.fetch_growth_tasks, T.accept_tasks, T.report_mp_events, T.claim_task)
        T.fetch_growth_tasks = fake_fetch
        T.accept_tasks = fake_accept
        T.report_mp_events = fake_report_mp
        T.claim_task = fake_claim
        try:
            res = T.run_miniprogram_tasks(FakeAccount(), gap=0)
        finally:
            (T.fetch_growth_tasks, T.accept_tasks, T.report_mp_events, T.claim_task) = orig
        self.assertTrue(res["ok"])
        self.assertEqual(res["earned_credit"], 100)
        actions = [c[0] for c in calls]
        self.assertIn("accept", actions)
        self.assertIn("report_mp", actions)
        self.assertIn("claim", actions)


if __name__ == "__main__":
    unittest.main(verbosity=2)
