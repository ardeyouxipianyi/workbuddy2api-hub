"""POST /accounts/credits 的 uids 点名补积分（面板自动补快照用）。

自动补积分此前不带参数打 /accounts/credits —— 等于全池扫（上百个号要几分钟），
刷不到的号每 15 秒再触发一轮。现在面板只点名缺积分的号（uids），服务端：

1. 空列表什么都不刷，绝不退化成全池扫；
2. 只刷点名的号（去重、跳过不存在的）；
3. 多于一个号时照样吃单飞与拟人间隔，单个号不吃；
4. 带了 uids 但不是列表（字符串 / 对象 / null）→ 400，不能落到全池扫。

Run with: python tests/run_all.py credits_auto_uids
"""
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy


class FakeHandler(wb_proxy.Handler):
    """不连 socket：只记下发出的状态码与头，body 写进内存。"""

    def __init__(self, inm=None):  # noqa: D401 - 故意不走 BaseHTTPRequestHandler 的构造
        self.headers = {"If-None-Match": inm} if inm is not None else {}
        self.wfile = io.BytesIO()
        self.path = "/accounts/credits"
        self.status = None
        self.sent = {}

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, k, v):
        self.sent[k] = v

    def end_headers(self):
        pass


class FakeAcct(object):
    def __init__(self, uid):
        self.uid = uid
        self.realm = "intl"


class FakePool(object):
    def __init__(self, uids):
        self.accounts = [FakeAcct(u) for u in uids]

    def get(self, uid):
        for a in self.accounts:
            if a.uid == uid:
                return a
        return None


class CreditsUidsRoutingTests(unittest.TestCase):

    def setUp(self):
        self.pool = FakePool(["u1", "u2", "u3", "u4"])
        self.swept = []
        self.replies = []
        patches = [
            mock.patch.object(wb_proxy, "POOL", self.pool),
            mock.patch.object(wb_proxy, "_credits_sweep", self._sweep),
            mock.patch.object(wb_proxy, "account_views", lambda *a, **k: []),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.h = FakeHandler()
        self.h._json_panel = lambda code, obj: self.replies.append(obj)
        self.errors = []
        self.h._error = lambda code, msg, kind="server_error": self.errors.append((code, kind))
        self.addCleanup(wb_proxy._credits_sweep_end)

    def _sweep(self, targets, paced):
        live = [t.uid for t in targets if t is not None]
        self.swept.append((live, paced))
        return [{"uid": u, "ok": True} for u in live]

    def test_empty_list_refreshes_nothing(self):
        self.h._route_accounts_credits_fetch({"uids": []})
        self.assertEqual(self.swept, [([], False)], "空列表绝不能退化成全池扫")

    def test_only_listed_uids_deduped_missing_skipped(self):
        self.h._route_accounts_credits_fetch({"uids": ["u3", "u1", "u3", "ghost", ""]})
        self.assertEqual(len(self.swept), 1)
        live, paced = self.swept[0]
        self.assertEqual(live, ["u3", "u1"])
        self.assertTrue(paced, "多于一个号要吃拟人间隔")

    def test_single_uid_in_list_is_not_paced(self):
        self.h._route_accounts_credits_fetch({"uids": ["u2"]})
        self.assertEqual(self.swept, [(["u2"], False)])

    def test_multi_uids_respect_single_flight(self):
        self.assertTrue(wb_proxy._credits_sweep_begin())
        try:
            self.h._route_accounts_credits_fetch({"uids": ["u1", "u2"]})
        finally:
            wb_proxy._credits_sweep_end()
        self.assertEqual(self.swept, [], "已有全量刷在跑时不能再扫")
        self.assertTrue(self.replies and self.replies[-1].get("busy"))

    def test_non_list_uids_is_rejected_not_full_sweep(self):
        for bad in ("u1", {"u1": 1}, None, 3):
            self.errors.clear()
            self.h._route_accounts_credits_fetch({"uids": bad})
            self.assertEqual(self.errors, [(400, "invalid_request_error")], repr(bad))
        self.assertEqual(self.swept, [], "坏 uids 一个号都不能刷，更不能全池扫")
        self.assertEqual(self.replies, [])

    def test_no_params_still_full_pool(self):
        """手动「一键刷新积分」与旧调用方的语义不变。"""
        self.h._route_accounts_credits_fetch({})
        self.assertEqual(self.swept, [(["u1", "u2", "u3", "u4"], True)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
