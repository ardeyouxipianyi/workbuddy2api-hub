"""`responses:` 日志要写出**实际生效**的推理强度，不只是客户端填的那个。

## 现场

`/v1/responses` 的 `responses:` 行原先只打 `effort=<请求里的 reasoning_effort>`。
客户端不填时那一格恒为 `None`，读日志的人会以为「这轮没开思考」，而网关其实
按模型默认档（`deepseek-*` 是 `high`）跑了——排查「为什么慢/为什么贵」时这个
误导代价很高（用户报的就是「日志里 effort=None 但明显在思考」）。

## 修法

同一行改成 `effort=<客户端值> -> upstream_effort=<实际值>`，两个值分别由
`client_effort_of()` 与 `upstream_effort_of()` 解析（后者与真正写进上游请求体
的那套解析是同一份：固定档模型 / 关闭思考 / 客户端值 / 模型默认）。

## 这里钉住的

  1. 日志格式里两个占位符都在（客户端值 → 实际值）；
  2. 解析结果与 `upstream_effort_of` 一致：不填 ≠ 无思考；
  3. 关闭思考（`none`）与显式档位都如实反映。

Run with the current interpreter (python tests/run_all.py responses_effort_log).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy as P

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ResponsesLogFormatTest(unittest.TestCase):
    def test_log_line_reports_both_levels(self):
        with open(os.path.join(ROOT, "wb_proxy.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("effort=%r -> upstream_effort=%r", src,
                      "responses: 行必须同时打出客户端值与实际值")
        # 两个占位符必须由对应的解析函数填充，不能又退回读原始字段
        idx = src.index("effort=%r -> upstream_effort=%r")
        tail = src[idx:idx + 700]
        self.assertIn("client_effort_of(chat_req)", tail)
        self.assertIn("upstream_effort_of(chat_req, model)", tail)


class EffortResolutionTest(unittest.TestCase):
    """解析口径：不填 ≠ 没有思考。"""

    MODEL = "deepseek-v4.1-flash"

    def test_omitted_effort_still_runs_at_model_default(self):
        self.assertIsNone(P.client_effort_of({}))
        actual = P.upstream_effort_of({}, self.MODEL)
        self.assertIsNotNone(actual, "不填时也必须能说出实际档位")
        self.assertEqual(actual, P.model_default_effort(self.MODEL))

    def test_explicit_effort_wins(self):
        self.assertEqual(P.upstream_effort_of({"reasoning_effort": "max"}, self.MODEL), "max")
        # camelCase 是另一个客户端拼法，同样要认
        self.assertEqual(P.upstream_effort_of({"reasoningEffort": "low"}, self.MODEL), "low")

    def test_disabled_reasoning_stays_none(self):
        # `none` = 明确关掉思考，不该被模型默认档覆盖
        self.assertEqual(P.upstream_effort_of({"reasoning_effort": "none"}, self.MODEL), "none")

    def test_unknown_model_without_effort_is_unknown(self):
        self.assertIsNone(P.upstream_effort_of({}, "some-model-with-no-default"))


if __name__ == "__main__":
    unittest.main()
