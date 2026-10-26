"""账号级模型黑名单：人工禁用「某账号 × 某些模型」的组合。

与自动节流（model_cooldowns / model_caps）区分：那些表达「暂时不该用」，
这里表达使用者明确指定的「这个号就别碰这几个模型」。钉住：

  1. 名单归一（去空格 / 小写 / 去重 / 丢非字符串），坏数据回落空列表；
  2. 随凭证档与公共视图走（to_dict / public）；
  3. model_disabled() 只做精确匹配；ready(model) 被黑名单挡住，
     但**不影响该账号的其他模型**，也不影响别的账号；
  4. unavailable_reason(model) 说出「该模型已被人工禁用」——
     池子被抽干时的报错因此能逐个账号讲清原因；
  5. set_disabled_models 落盘、空数组清除、未知 uid 返回 None；
  6. 看板接线存在（accountRow 渲染 + editAccountDisabledModels 走 /accounts/set）。

Run with the current interpreter (python tests/run_all.py disabled_models).
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import _dashboard_source  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_account(uid, disabled=None, token="tok", **extra):
    data = {"uid": uid, "accessToken": token, "realm": "cn"}
    if disabled is not None:
        data["disabledModels"] = disabled
    data.update(extra)
    return wb_accounts.Account(data)


class FieldTests(unittest.TestCase):
    def test_default_is_empty(self):
        self.assertEqual(make_account("d-def").disabled_models, [])

    def test_normalised_on_load(self):
        a = make_account("d-norm", [" GLM-5.3 ", "glm-5.3", "gpt-6-astra", "", None, 7])
        self.assertEqual(a.disabled_models, ["glm-5.3", "gpt-6-astra"])

    def test_junk_falls_back_to_empty(self):
        for n, junk in enumerate((None, 123, "glm-5.3", {}, True)):
            self.assertEqual(make_account("d-junk-%d" % n, junk).disabled_models, [])

    def test_round_trips_through_dict_and_public(self):
        a = make_account("d-rt", ["glm-5.3"])
        self.assertEqual(a.to_dict()["disabledModels"], ["glm-5.3"])
        self.assertEqual(a.public()["disabledModels"], ["glm-5.3"])


class NormaliseTests(unittest.TestCase):
    def test_helper(self):
        f = wb_accounts.normalise_disabled_models
        self.assertEqual(f(["A", "a", " b ", ""]), ["a", "b"])
        self.assertEqual(f(None), [])
        self.assertEqual(f("glm-5.3"), [])
        self.assertEqual(f([1, 2]), [])


class ModelDisabledTests(unittest.TestCase):
    def test_exact_match_only(self):
        a = make_account("d-exact", ["glm-5.3"])
        self.assertTrue(a.model_disabled("glm-5.3"))
        self.assertTrue(a.model_disabled(" GLM-5.3 "))          # 归一后命中
        self.assertFalse(a.model_disabled("glm-5.3-flash"))     # 不做前缀
        self.assertFalse(a.model_disabled("glm-5.2"))
        self.assertFalse(a.model_disabled(""))
        self.assertFalse(a.model_disabled(None))

    def test_empty_list_never_disables(self):
        self.assertFalse(make_account("d-none").model_disabled("anything"))

    def test_ready_refuses_only_that_model(self):
        a = make_account("d-ready", ["glm-5.3"])
        self.assertFalse(a.ready(model="glm-5.3"))
        # 黑名单只挡那一个模型：兄弟模型照常可用
        self.assertTrue(a.ready(model="glm-5.2"))
        self.assertTrue(a.ready(model=None))

    def test_disabled_account_is_not_a_candidate_for_that_model(self):
        # 池子侧的实际后果：pick 不把它算作该模型的可选账号
        directory = tempfile.mkdtemp(prefix="deny-pick-")
        pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
        pool.accounts = [make_account("p-a", ["glm-5.3"]), make_account("p-b")]
        self.assertEqual(pool.count_ready("cn", model="glm-5.3"), 1)
        self.assertEqual(pool.count_ready("cn", model="glm-5.2"), 2)

    def test_unavailable_reason_mentions_the_ban(self):
        a = make_account("d-reason", ["glm-5.3"])
        self.assertEqual(a.unavailable_reason(model="glm-5.3"), "该模型已被人工禁用")
        self.assertEqual(a.unavailable_reason(model="glm-5.2"), "")


class SetDisabledModelsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="deny-set-")
        self.pool = wb_accounts.AccountPool(self.directory, log=lambda _m: None)
        self.pool.accounts = [make_account("s-a"), make_account("s-b")]

    def test_sets_and_clears(self):
        updated = self.pool.set_disabled_models("s-a", [" GLM-5.3 ", "glm-5.3"])
        self.assertEqual(updated["disabledModels"], ["glm-5.3"])
        self.assertEqual(self.pool.set_disabled_models("s-a", [])["disabledModels"], [])
        self.assertEqual(self.pool.get("s-a").disabled_models, [])

    def test_junk_becomes_empty_not_an_error(self):
        self.assertEqual(
            self.pool.set_disabled_models("s-a", "glm-5.3")["disabledModels"], [])
        self.assertEqual(
            self.pool.set_disabled_models("s-a", [123, None])["disabledModels"], [])

    def test_unknown_uid_returns_none(self):
        self.assertIsNone(self.pool.set_disabled_models("nope", ["x"]))

    def test_change_reaches_the_disk(self):
        self.pool.set_disabled_models("s-b", ["glm-5.3"])
        pool2 = wb_accounts.AccountPool(self.directory, log=lambda _m: None)
        self.assertEqual({a.uid: a for a in pool2.load()}["s-b"].disabled_models,
                         ["glm-5.3"])


class DashboardWiringTests(unittest.TestCase):
    def test_dashboard_has_denylist_editing(self):
        html = _dashboard_source.dashboard_html()
        self.assertIn("editAccountDisabledModels", html)
        self.assertIn("{uid: uid, disabledModels: denyList}", html)
        self.assertIn("+ 禁用模型", html)
        self.assertIn("禁用的模型: ", html)

    def test_dashboard_uses_a_checkbox_picker_not_a_prompt(self):
        """面板用「启用的模型」复选框列表：默认全选，取消勾选 = 禁用。"""
        html = _dashboard_source.dashboard_html()
        # 默认全选：勾选状态 = 不在禁用名单里
        self.assertIn("const picked = new Set(models.filter(m => !denied.has(m)));", html)
        # 取消勾选 -> 落进禁用名单
        self.assertIn("const denyList = models.filter(m => !picked.has(m));", html)
        # 取该账号自己区域的清单，而不是看板当前视图那一份
        self.assertIn("/v1/models?realm=' + encodeURIComponent(realm)", html)
        # 有全选 / 全不选
        self.assertIn("'#mdmAll'", html)
        self.assertIn("'#mdmNone'", html)

    def test_dashboard_reads_the_field_from_the_payload(self):
        html = _dashboard_source.dashboard_html()
        self.assertIn("a.disabledModels || []", html)


if __name__ == "__main__":
    unittest.main()
