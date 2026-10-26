"""账号备注：面板标记「批量」/「自用」，随凭证档持久化。

备注是纯展示字段：不参与任何调度判断，落盘、导出/导入照常携带，看板在账号
行内显示并可直接编辑（有备注的账号在表格里置顶——那是前端排序，由
tests/_test_account_weight.js 钉住）。这里钉住字段本身：

  1. 默认空、坏数据回落空串、超长截断；
  2. 随凭证档与公共视图走（to_dict / public / 导入导出）；
  3. set_note 落盘、空串清除、未知 uid 返回 None；
  4. 看板接线存在（accountRow 渲染备注行 + editAccountNote 走 /accounts/set）。

Run with the current interpreter (python tests/run_all.py note).
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import _dashboard_source  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_account(uid, note=None, **extra):
    data = {"uid": uid, "accessToken": "token-%s" % uid, "realm": "cn"}
    if note is not None:
        data["note"] = note
    data.update(extra)
    return wb_accounts.Account(data)


class NoteFieldTests(unittest.TestCase):
    def test_default_note_is_empty(self):
        self.assertEqual(make_account("n-def").note, "")

    def test_note_is_stripped_and_clamped(self):
        self.assertEqual(make_account("n-pad", note="  批量  ").note, "批量")
        long = "x" * 500
        self.assertEqual(len(make_account("n-long", note=long).note), 200)

    def test_junk_falls_back_to_empty(self):
        for junk in (None, 123, [], {}, True):
            self.assertEqual(make_account("n-junk", note=junk).note, "")

    def test_to_dict_and_public_carry_note(self):
        account = make_account("n-view", note="自用")
        self.assertEqual(account.to_dict()["note"], "自用")
        self.assertEqual(account.public()["note"], "自用")
        self.assertEqual(make_account("n-view2").public()["note"], "")

    def test_note_round_trips_through_the_credential_file(self):
        with tempfile.TemporaryDirectory(prefix="note-persist-") as directory:
            make_account("n-disk", note="批量01").save(directory)
            pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
            loaded = {a.uid: a for a in pool.load()}
            self.assertEqual(loaded["n-disk"].note, "批量01")


class SetNoteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="note-set-")
        self.pool = wb_accounts.AccountPool(self.directory, log=lambda _m: None)
        self.pool.accounts = [make_account("s-a"), make_account("s-b", note="旧备注")]

    def test_sets_and_clears(self):
        updated = self.pool.set_note("s-a", "  批量  ")
        self.assertEqual(updated["note"], "批量")
        self.assertEqual(self.pool.set_note("s-a", "   ")["note"], "")
        self.assertEqual(self.pool.get("s-a").note, "")

    def test_overwrites_the_old_note(self):
        self.assertEqual(self.pool.set_note("s-b", "自用")["note"], "自用")
        self.assertEqual(self.pool.get("s-b").note, "自用")

    def test_unknown_uid_returns_none(self):
        self.assertIsNone(self.pool.set_note("nope", "x"))

    def test_note_change_reaches_the_disk(self):
        self.pool.set_note("s-b", "批量02")
        with tempfile.TemporaryDirectory(prefix="note-disk-") as directory:
            pass
        # 重新从原目录读，确认落盘
        pool2 = wb_accounts.AccountPool(self.directory, log=lambda _m: None)
        self.assertEqual({a.uid: a for a in pool2.load()}["s-b"].note, "批量02")


class DashboardWiringTests(unittest.TestCase):
    """看板接线的结构断言（渲染细节由 JS 套件钉）。"""

    def test_dashboard_has_note_editing(self):
        html = _dashboard_source.dashboard_html()
        self.assertIn("editAccountNote", html)
        self.assertIn("{uid: uid, note: next}", html)
        self.assertIn("+ 备注", html)

    def test_dashboard_sorts_note_accounts_first(self):
        html = _dashboard_source.dashboard_html()
        self.assertIn("const nx = x[0].note ? 0 : 1, ny = y[0].note ? 0 : 1;", html)


if __name__ == "__main__":
    unittest.main()
