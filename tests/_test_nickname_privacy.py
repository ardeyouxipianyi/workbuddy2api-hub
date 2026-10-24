"""账号手机号隐私化（v2 方案）——出口脱敏的单元与源码级断言。

背景：账号池里 14/20 个账号拿手机号当昵称，面板上直接明文显示。脱敏集中在
服务端出口（`_json_panel` / `_json_cached_panel` / `add_log_entry` / `log()`），
不改前端一处。这份用例钉住三件容易出错的事：

  1. **不误伤**：26 位订单号（deal_name）、10 位时间戳、`total_tokens` 这类
     数字不能被切成号码。
  2. **不污染**：脱敏是 copy-on-write，绝不能原地改共享缓存对象
     （`remaining_usage()` 等直接返回缓存本体）。
  3. **顺序**：`add_log_entry` 必须先推断 level/tag 再掩码，否则含数字的
     ERROR 判定会被掩码吃掉；控制台 stderr 与看板 buffer 同源脱敏。

外加源码级断言，钉住 v2 的核心安全属性：/v1 的三个非流式生成路由**不许**出现
`_json_panel`——那是模型输出，一个字节都不能动。

数据目录来自 tests/_isolated_dirs.py，用例不碰仓库自己的 accounts/ 与 usage/。
"""
import io
import json
import os
import sys
import types
import unittest
from contextlib import ExitStack
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-privacy-")

import wb_proxy  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PHONE = "13812345678"
PHONE_MASKED = "138****5678"
# 这个号码同时含 "11101" 子串——add_log_entry 靠它判 ERROR（见 LogEgressTest）
PHONE_ERR = "13811101123"
PHONE_ERR_MASKED = "138****1123"
ORDER_26 = "9" * 26


class MaskPhoneTest(unittest.TestCase):
    """值级：只有「整串就是号码」才掩码。"""

    def test_a_full_phone_number_becomes_head_and_tail(self):
        self.assertEqual(wb_proxy._mask_phone(PHONE), PHONE_MASKED)

    def test_surrounding_whitespace_does_not_defeat_the_match(self):
        self.assertEqual(wb_proxy._mask_phone("  %s  " % PHONE), PHONE_MASKED)

    def test_masking_is_idempotent(self):
        once = wb_proxy._mask_phone(PHONE)
        self.assertEqual(wb_proxy._mask_phone(once), once)

    def test_non_phone_and_non_string_values_pass_through(self):
        for value in ("老王", "国际号", "", "1", "12812345678",
                      "1381234567", "138123456789", None, 13812345678,
                      b"13812345678", ["13812345678"]):
            self.assertIs(wb_proxy._mask_phone(value), value,
                          "不应改动 %r" % (value,))

    def test_a_plus_86_prefixed_number_is_left_alone(self):
        # 现有账号数据里昵称就是裸 11 位号码，没有 +86 形态。这里钉住当前行为：
        # 若将来真出现 +86 昵称，需要单独加规则（而不是指望现有的两条正则）。
        self.assertEqual(wb_proxy._mask_phone("+86" + PHONE), "+86" + PHONE)


class MaskTextTest(unittest.TestCase):
    """文本级：带边界，避免把大数字切一段出来。"""

    def test_a_phone_inside_prose_is_replaced(self):
        self.assertEqual(wb_proxy._mask_text("账号 %s 已导入" % PHONE),
                         "账号 %s 已导入" % PHONE_MASKED)

    def test_a_26_digit_order_id_is_not_touched(self):
        text = "订单 %s 已创建" % ORDER_26
        self.assertEqual(wb_proxy._mask_text(text), text)

    def test_a_10_digit_timestamp_is_not_touched(self):
        for text in ("ts=1700000000", "created_at 1700000000 ok"):
            self.assertEqual(wb_proxy._mask_text(text), text)

    def test_a_digit_run_longer_than_a_phone_is_not_carved_up(self):
        # 前后紧邻数字 → 边界不成立，整串不动（这是防数据损坏的关键一条）
        for text in ("138123456780", "0138123456789", "113812345678"):
            self.assertEqual(wb_proxy._mask_text(text), text)

    def test_non_string_and_phone_free_text_pass_through(self):
        for value in (None, 123, "没有任何号码的一段话", ""):
            self.assertIs(wb_proxy._mask_text(value), value)

    def test_a_bare_11_digit_string_is_masked_at_text_level(self):
        # 文本级规则对「整串就是一个号码」也命中。这正是要求 _redact_account_names
        # 只用在面板出口、且靠实盘逐叶 diff 兜底的原因：真实载荷里不存在
        # 「非昵称字段、却是 11 位 1[3-9] 纯数字字符串」的值（见验证记录）。
        self.assertEqual(wb_proxy._mask_text(PHONE), PHONE_MASKED)

    def test_a_separated_phone_is_still_masked(self):
        # 人类书写习惯：3-4-4 分组间常见分隔符。首三位仍须连续。
        for text in ("138-1234-5678", "138 1234 5678", "138.1234.5678",
                     "138·1234·5678", "138\t1234\t5678", "1381234 5678"):
            self.assertEqual(wb_proxy._mask_text(text), PHONE_MASKED, text)
            self.assertEqual(wb_proxy._mask_text("账号 %s 已导入" % text),
                             "账号 %s 已导入" % PHONE_MASKED, text)
            # 掩码结果幂等：再次调用不再变化
            self.assertEqual(wb_proxy._mask_text(PHONE_MASKED), PHONE_MASKED, text)

    def test_separator_tolerance_does_not_injure_digit_ids(self):
        # 加了分隔符容忍后，「大数字不被切段」这条保证必须仍然成立：
        # 26 位连续订单号、10 位时间戳、12/13 位超长数字串一律原样。
        for text in ("订单 %s 已创建" % ORDER_26, "ts=1700000000",
                     "138123456789", "0138123456789", "113812345678"):
            self.assertEqual(wb_proxy._mask_text(text), text, text)

    def test_an_identifier_with_a_phone_shaped_run_is_left_alone(self):
        # #581：国内号 uid 里的一段 `179-97517476` 满足 3-4-4 形状，但左右紧邻
        # 十六进制字符，不是号码。掩码它会让面板拿到池子里不存在的 uid。
        uid = "6ef20bf4-76fa-4085-b179-97517476cc1e"
        self.assertEqual(wb_proxy._mask_text(uid), uid)
        # 同一个号码写在散文里仍然要掩码（中文是合法边界）。
        self.assertEqual(wb_proxy._mask_text("账号179-97517476已停用"),
                         "账号179****7476已停用")


class RedactAccountNamesTest(unittest.TestCase):
    """递归 + copy-on-write。"""

    def test_a_nickname_key_is_masked(self):
        obj = {"accounts": [{"uid": "a1b2c3d4", "nickname": PHONE}]}
        out = wb_proxy._redact_account_names(obj)
        self.assertEqual(out["accounts"][0]["nickname"], PHONE_MASKED)
        self.assertEqual(out["accounts"][0]["uid"], "a1b2c3d4")

    def test_a_nickname_with_an_embedded_number_is_masked(self):
        out = wb_proxy._redact_account_names({"nickname": "老王(%s)" % PHONE})
        self.assertEqual(out["nickname"], "老王(%s)" % PHONE_MASKED)

    def test_unrelated_fields_are_not_touched(self):
        obj = {
            "uid": "a1b2c3d4e5f6",
            "total_tokens": 15349000000,      # int：脱敏只碰 str
            "count": "15349000000",           # str 且不是号码形态 → 见下一条
            "deal_name": ORDER_26,
            "ts": "1700000000",
            "realm": "cn",
        }
        out = wb_proxy._redact_account_names(obj)
        self.assertEqual(out["uid"], "a1b2c3d4e5f6")
        self.assertEqual(out["total_tokens"], 15349000000)
        self.assertEqual(out["deal_name"], ORDER_26)
        self.assertEqual(out["ts"], "1700000000")
        self.assertEqual(out["realm"], "cn")
        # "15349000000" 是 11 位 1[3-9] 串，文本级会命中——如实钉住这一点，
        # 真实载荷里没有这样的字段（实盘 diff 验证），所以不会造成损坏。
        self.assertEqual(out["count"], "153****0000")

    def test_identity_fields_are_never_rewritten(self):
        # #581：uid 被改写成池子里不存在的值，面板再拿它调 /accounts 就 404
        # （「操作失败: no such account」）。标识符字段一律原样。
        uid = "6ef20bf4-76fa-4085-b179-97517476cc1e"
        obj = {"accounts": [{"uid": uid, "nickname": "老王",
                             "machineId": uid, "sessionId": uid,
                             "file": uid + ".json"}]}
        out = wb_proxy._redact_account_names(obj)
        account = out["accounts"][0]
        self.assertEqual(account["uid"], uid)
        self.assertEqual(account["machineId"], uid)
        self.assertEqual(account["sessionId"], uid)
        self.assertEqual(account["file"], uid + ".json")
        # 同一份载荷里的昵称仍然脱敏。
        self.assertEqual(out["accounts"][0]["nickname"], "老王")

    def test_a_phone_nickname_is_still_masked_next_to_identity_fields(self):
        uid = "6ef20bf4-76fa-4085-b179-97517476cc1e"
        out = wb_proxy._redact_account_names({"uid": uid, "nickname": PHONE})
        self.assertEqual(out["uid"], uid)
        self.assertEqual(out["nickname"], PHONE_MASKED)

    def test_logs_and_msg_channels_are_covered(self):
        # 昵称被拼进字符串、键名不是 nickname 的那些通道（v1 漏掉的一类）
        obj = {
            "logs": ["====== 正在为账号 [%s (u1)] 执行任务 ======" % PHONE],
            "msg": "%s: 打卡完成" % PHONE,
        }
        out = wb_proxy._redact_account_names(obj)
        self.assertNotIn(PHONE, out["logs"][0])
        self.assertIn(PHONE_MASKED, out["logs"][0])
        self.assertNotIn(PHONE, out["msg"])
        self.assertIn(PHONE_MASKED, out["msg"])

    def test_no_hit_returns_the_same_object(self):
        obj = {"uid": "u1", "n": 1, "s": "hello", "nested": {"a": [1, 2]}}
        self.assertIs(wb_proxy._redact_account_names(obj), obj)

    def test_a_hit_returns_a_copy_and_leaves_the_input_alone(self):
        original = {"nickname": PHONE, "nested": [{"nickname": PHONE}]}
        snapshot = json.dumps(original, sort_keys=True)
        out = wb_proxy._redact_account_names(original)
        self.assertIsNot(out, original)
        # 原对象（可能是共享缓存本体）必须一个字节都没变
        self.assertEqual(json.dumps(original, sort_keys=True), snapshot)
        self.assertEqual(out["nested"][0]["nickname"], PHONE_MASKED)

    def test_untouched_branches_keep_their_identity(self):
        shared = {"uid": "u1"}
        obj = {"a": shared, "nickname": PHONE}
        out = wb_proxy._redact_account_names(obj)
        self.assertIs(out["a"], shared)

    def test_redaction_is_idempotent(self):
        obj = {"nickname": PHONE, "logs": ["x %s y" % PHONE]}
        once = wb_proxy._redact_account_names(obj)
        twice = wb_proxy._redact_account_names(once)
        self.assertEqual(once, twice)


class LogEgressTest(unittest.TestCase):
    """看板 buffer 与控制台 stderr 同源脱敏，且顺序正确。"""

    def setUp(self):
        wb_proxy.clear_logs()

    def test_level_inference_runs_before_masking(self):
        # PHONE_ERR 含 "11101"（ERROR 判定用的数字子串）。若先掩码，"11101" 就没了，
        # 这一行会被降级成 INFO —— 所以顺序必须是「先推断、后掩码」。
        entry = wb_proxy.add_log_entry(PHONE_ERR)
        self.assertEqual(entry["level"], "ERROR")
        self.assertEqual(entry["msg"], PHONE_ERR_MASKED)
        self.assertNotIn(PHONE_ERR, entry["msg"])

    def test_the_buffer_holds_only_masked_text(self):
        wb_proxy.add_log_entry("[任务福利] 账号 [%s] 每日签到: OK" % PHONE,
                               tag="tasks")
        rows = wb_proxy.get_logs(limit=50)["logs"]
        self.assertTrue(rows)
        for row in rows:
            self.assertNotIn(PHONE, row["msg"])
        self.assertTrue(any(PHONE_MASKED in row["msg"] for row in rows))

    def test_stderr_is_masked_too(self):
        stream = io.StringIO()
        with mock.patch.object(sys, "stderr", stream):
            wb_proxy.log("[任务福利] 账号 [%s] 网页通道打卡: OK" % PHONE,
                         tag="tasks")
            # 写 stderr 由后台线程承担（#642 之前的写法会在请求线程里同步写）：
            # 等这一行真正落下去再断言，否则读到的是「还没写」的空串。
            wb_proxy.flush_log_channel(1.0)
        written = stream.getvalue()
        self.assertNotIn(PHONE, written)
        self.assertIn(PHONE_MASKED, written)
        # 同一次调用进 buffer 的也是掩码值
        self.assertTrue(any(PHONE_MASKED in r["msg"]
                            for r in wb_proxy.get_logs(limit=50)["logs"]))

    def test_a_long_order_id_in_a_log_line_is_not_carved_up(self):
        wb_proxy.add_log_entry("order %s created" % ORDER_26)
        rows = wb_proxy.get_logs(limit=50)["logs"]
        self.assertTrue(any(ORDER_26 in r["msg"] for r in rows))

    def test_export_masks_rows_written_before_masking_existed(self):
        """导出走 send_response/wfile，不经过 _json_panel —— 掩码得自己补。

        直接往 buffer 里塞一条明文行，模拟「落库打开之前就已经写进去」的历史数据。
        """
        wb_proxy.LOG_BUFFER.append({
            "id": 10 ** 9, "ts": "2026-10-01 00:00:00", "time": "00:00:00",
            "level": "INFO", "tag": "tasks",
            "msg": "historical row with %s in it" % PHONE,
        })
        handler = _ExportHandler()
        handler._get_logs_export()
        body = handler.body()
        self.assertNotIn(PHONE, body)
        self.assertIn(PHONE_MASKED, body)


class _CapturingHandler(object):
    """只换掉最底层的 `_json`，把它的入参截下来（真正的出口就是这里）。

    `_json_panel` / `_json_cached_panel` 用真实的——自己再写一份 stub 等于把
    被测的脱敏逻辑整条绕过去，测了个寂寞。
    """

    _json_panel = wb_proxy.Handler._json_panel

    def __init__(self):
        self.captured = None

    def _json(self, code, obj):
        self.captured = (code, obj)


class _ExportHandler(object):
    """导出接口的最小外壳：它只用到 _authorized / send_* / wfile。

    `_get_logs_export` 直接写 wfile，不走 `_json_panel`，所以这里不 stub 脱敏，
    测的就是真实实现里有没有补那道掩码。
    """

    _get_logs_export = wb_proxy.Handler._get_logs_export

    def __init__(self):
        self.status = None
        self.headers = []
        self.path = "/logs/export"
        self._chunks = []
        self.wfile = self

    def _authorized(self):
        return True

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.headers.append((key, value))

    def end_headers(self):
        pass

    def write(self, data):
        self._chunks.append(data)

    def body(self):
        return b"".join(self._chunks).decode("utf-8")


class _FakeAccount(object):
    def __init__(self, uid, nickname):
        self.uid = uid
        self.nickname = nickname
        self.realm = "cn"
        self.enabled = True

    def checkin(self, trigger=None):
        return {"ok": True, "msg": "checkin ok"}


class PostRoutePayloadTest(unittest.TestCase):
    """POST 任务路由的真实载荷必须已被掩码。

    这些通道（批次日志、summary_msg、results[].nickname）只在 POST 响应里出现，
    而跑真任务会改账号状态，不能用实盘去驱动；所以在 handler 级用桩把上游任务
    函数换掉，只验证「出参形态」——也就是泄露面。
    """

    def _run(self, name, payload, accounts, patches):
        handler = _CapturingHandler()
        pool = types.SimpleNamespace(
            accounts=accounts,
            get=lambda u: next((a for a in accounts if a.uid == u), None),
            list_public=lambda realm=None: [{"uid": a.uid, "nickname": a.nickname}
                                            for a in accounts])
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(wb_proxy, "POOL", pool))
            stack.enter_context(mock.patch.object(wb_proxy, "invalidate_tasks_cache",
                                                  lambda: None))
            for target, attr, value in patches:
                stack.enter_context(mock.patch.object(target, attr, value))
            getattr(wb_proxy.Handler, name)(handler, payload)
        self.assertIsNotNone(handler.captured, "%s 没有产出响应" % name)
        return handler.captured[1]

    def test_tasks_run_logs_are_masked(self):
        import wb_tasks
        acct = _FakeAccount("u" * 16, PHONE)
        payload = self._run(
            "_route_tasks_run", {"uid": acct.uid}, [acct],
            [(wb_tasks, "run_growth_tasks",
              lambda acc, gap=1.0, claim=True: {"earned_credit": 5, "logs": ["step ok"]})])
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(PHONE, blob)
        self.assertIn(PHONE_MASKED, blob)

    def test_tasks_travel_summary_and_results_are_masked(self):
        import wb_tasks
        acct = _FakeAccount("u" * 16, PHONE)
        payload = self._run(
            "_route_tasks_travel", {"uid": acct.uid}, [acct],
            [(wb_tasks, "do_cat_travel",
              lambda acc: {"action": "travel", "msg": "got 3", "credit": 3})])
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(PHONE, blob)
        self.assertIn(PHONE_MASKED, blob)
        self.assertIn(PHONE_MASKED, payload["msg"])
        self.assertEqual(payload["results"][0]["nickname"], PHONE_MASKED)

    def test_checkin_results_and_account_views_are_masked(self):
        acct = _FakeAccount("u" * 16, PHONE)
        payload = self._run(
            "_route_accounts_checkin", {"uid": acct.uid}, [acct], [])
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(PHONE, blob)
        self.assertEqual(payload["results"][0]["nickname"], PHONE_MASKED)


class SourceLevelTest(unittest.TestCase):
    """把「哪个出口该用哪个 hook」钉在源码上，防止将来被人改回去。"""

    def setUp(self):
        with open(os.path.join(REPO, "wb_proxy.py"), encoding="utf-8") as fh:
            self.src = fh.read()

    def _body(self, name):
        start = self.src.index("    def %s(" % name)
        nxt = self.src.find("\n    def ", start + 1)
        return self.src[start:nxt if nxt != -1 else len(self.src)]

    def test_the_llm_generation_routes_never_use_the_panel_hook(self):
        # v2 的核心安全属性：模型输出一个字节都不许动。
        for name in ("_chat_nonstream_response",
                     "_responses_nonstream_response",
                     "_messages_nonstream_response"):
            body = self._body(name)
            self.assertIn("self._json(", body, name)
            self.assertNotIn("_json_panel", body, name)

    def test_account_bearing_panel_handlers_use_the_panel_hook(self):
        for name in ("_get_accounts", "_get_v1_usage", "_get_usage_recent",
                     "_get_usage_analytics", "_get_usage_remaining",
                     "_get_activity_history", "_get_account_credits_grants",
                     "_get_account_credits_detail", "_get_accounts_login_poll",
                     "_get_tasks", "_get_scheduler",
                     "_route_tasks_run", "_route_tasks_streak_bonus",
                     "_route_tasks_travel", "_route_accounts_checkin",
                     "_route_accounts_daily_chat",
                     "_route_accounts_daily_chat_web",
                     "_route_accounts_sync_profile",
                     "_route_accounts_import_desktop",
                     "_route_accounts_import", "_route_accounts_product",
                     # 上游 v1.7.7 的「一键测试」：结果里带 account_views()，
                     # 合并时曾用裸 _json 直出，现在必须走脱敏出口。
                     "_route_accounts_test_all"):
            body = self._body(name)
            # 缓存型端点用 _json_cached_panel，其余用 _json_panel；两者都要拒收
            # 没有脱敏的 _json( / _json_cached(。
            self.assertTrue("_json_panel" in body or "_json_cached_panel" in body,
                            name)
            self.assertNotIn("self._json(200", body, name)
            self.assertNotIn("self._json_cached(", body, name)

    def test_the_export_document_is_redacted_once_for_both_paths(self):
        body = self._body("_get_accounts_export")
        self.assertIn("_redact_account_names(doc)", body)

    def test_the_import_desktop_console_uses_the_text_level_mask(self):
        # CRG 复审点：控制台 stdout 也是一个出口。必须用文本级 _mask_text，
        # 否则「老王(13812345678)」这种昵称内嵌号码会明文打到控制台。
        i = self.src.index("if args.import_desktop:")
        seg = self.src[i:i + 700]
        self.assertIn("_mask_text(account.nickname)", seg)
        self.assertNotIn("_mask_phone(account.nickname)", seg)

    def test_the_panel_hook_is_defined_but_json_stays_redaction_free(self):
        self.assertIn("def _json_panel(self, code, obj):", self.src)
        json_body = self._body("_json")
        json_body = json_body.split("\n    def ")[0]
        self.assertNotIn("_redact_account_names", json_body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
