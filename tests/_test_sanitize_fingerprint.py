"""Tests for fingerprint sanitization, including PR #62 OmO Sisyphus-Junior fix."""
import json, os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _isolated_dirs import isolated_data_dirs  # noqa: E402  (its own data root)
# Its own directories, removed when this process exits. This suite used to share
# tests/_acc_test and tests/_use_test with _test_daily_chat.py and
# _test_tasks_cache.py, which made them unsafe to run at the same time and left
# directories in the repo after a run.
_TMP = isolated_data_dirs("wb-fingerprint-")

import wb_proxy as P

class FingerprintSanitizationTests(unittest.TestCase):
    def test_omo_junior_fingerprint_neutralized(self):
        fp = "Sisyphus-Junior - Focused executor from OhMyOpenCode"
        # 1. Exact string rewritten
        self.assertEqual(P.sanitize_text(fp), "Sisyphus-Junior - Focused executor")

        # 2. Case insensitive
        self.assertEqual(P.sanitize_text(fp.lower()), "Sisyphus-Junior - Focused executor")

        # 3. Embedded in context
        text = f"You are a subagent. {fp}. Complete the task."
        expected = "You are a subagent. Sisyphus-Junior - Focused executor. Complete the task."
        self.assertEqual(P.sanitize_text(text), expected)

    def test_omo_partial_tokens_untouched(self):
        # Tokens alone should not be stripped or modified
        self.assertEqual(P.sanitize_text("Sisyphus-Junior alone"), "Sisyphus-Junior alone")
        self.assertEqual(P.sanitize_text("from OhMyOpenCode alone"), "from OhMyOpenCode alone")
        self.assertEqual(P.sanitize_text("Sisyphus master agent"), "Sisyphus master agent")

    def test_sanitize_messages_covers_all_roles_and_tool_args(self):
        fp = "Sisyphus-Junior - Focused executor from OhMyOpenCode"
        clean = "Sisyphus-Junior - Focused executor"
        messages = [
            {"role": "system", "content": f"System: {fp}"},
            {"role": "user", "content": f"User: {fp}"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "run", "arguments": json.dumps({"query": fp})}
                }]
            },
            {"role": "tool", "tool_call_id": "call_1", "content": f"Tool output: {fp}"}
        ]
        sanitized = P.sanitize_messages(messages)
        self.assertIn(clean, sanitized[0]["content"])
        self.assertNotIn("from OhMyOpenCode", sanitized[0]["content"])
        self.assertIn(clean, sanitized[1]["content"])
        self.assertNotIn("from OhMyOpenCode", sanitized[1]["content"])
        args = json.loads(sanitized[2]["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(args["query"], clean)
        self.assertIn(clean, sanitized[3]["content"])
        self.assertNotIn("from OhMyOpenCode", sanitized[3]["content"])

class DshAgentFingerprintTests(unittest.TestCase):
    """DSH 的厂商归属句命中 11128 时的脱敏行为 / the DSH 11128 rewrite."""

    # 原文刻意不直接落盘：本仓库的用户大多把源码喂给智能体代改，明文常驻的指纹
    # 一旦被读进上下文、又被回显进请求，就会让使用方自己被 11128 拦掉——本规则
    # 的由来正是这样一次事故。片段拼接保证测试语义不变，也不会被顺手复述出去。
    # The sentence is assembled from fragments on purpose: users of this repo mostly
    # hand the source to an agent, and a plaintext fingerprint sitting in the tree
    # gets read into context and echoed back into a request, blocking that user with
    # 11128 — exactly the incident this rule came from.
    FP = ("You are an AI agent " "powered by " "DeepSeek " "Harness")

    def test_default_rewrite_keeps_attribution(self):
        out = P.sanitize_text(self.FP + ".")
        self.assertEqual(out, P.DSH_AGENT_REWRITE_VARIANT + ".")
        # 整句不再是连续指纹 / the contiguous fingerprint is gone
        self.assertNotIn(self.FP, out)
        # 产品归属仍在 / the attribution survives
        self.assertIn("DeepSeek Harness", out)

    def test_case_insensitive(self):
        self.assertEqual(P.sanitize_text(self.FP.lower() + "."),
                         P.DSH_AGENT_REWRITE_VARIANT + ".")

    def test_embedded_in_context_keeps_punctuation(self):
        text = "Intro. " + self.FP + ". Outro."
        self.assertEqual(P.sanitize_text(text),
                         "Intro. " + P.DSH_AGENT_REWRITE_VARIANT + ". Outro.")

    def test_neutral_mode_via_env(self):
        old = os.environ.get("WB_DSH_AGENT_REWRITE")
        os.environ["WB_DSH_AGENT_REWRITE"] = "neutral"
        try:
            self.assertEqual(P.sanitize_text(self.FP + "."),
                             P.DSH_AGENT_REWRITE_NEUTRAL + ".")
        finally:
            if old is None:
                os.environ.pop("WB_DSH_AGENT_REWRITE", None)
            else:
                os.environ["WB_DSH_AGENT_REWRITE"] = old

    def test_individual_tokens_untouched(self):
        # 单词与改动过的变体都不该被碰 / neither single tokens nor reworded
        # variants are touched — the upstream accepts those on their own.
        for text in ("DeepSeek Harness alone",
                     "You are an AI agent.",
                     "powered by DeepSeek Harness",
                     "You are an AI agent powered by DeepSeek AI's DeepSeek Harness."):
            self.assertEqual(P.sanitize_text(text), text)

    def test_clean_text_untouched(self):
        text = "You are a helpful assistant."
        self.assertFalse(P.has_fingerprint(text))
        self.assertEqual(P.sanitize_text(text), text)


if __name__ == "__main__":
    unittest.main()

