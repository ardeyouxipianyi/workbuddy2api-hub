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

class DshHarnessChannelFingerprintTests(unittest.TestCase):
    """DSH（DeepSeek Harness）系统提示首句的渠道指纹（与 #382 / #506 同类）。

    上游对连续短语做大小写不敏感的渠道校验，命中即 400 / code 11128。逐句二分
    确认：整句触发，拆词或单独 ``DeepSeek Harness`` 都不触发；只插入一个 "the"
    改变短语结构后即不再命中。
    """

    FP = "You are an AI agent powered by DeepSeek Harness."
    FP2 = "You are a coding agent powered by the deepseek-v4.1-flash model."
    CLEAN = "You are a helpful AI assistant."

    def test_exact_phrase_rewritten(self):
        self.assertEqual(P.sanitize_text(self.FP), self.CLEAN)

    def test_case_insensitive(self):
        self.assertEqual(P.sanitize_text(self.FP.upper()), self.CLEAN)
        self.assertEqual(P.sanitize_text(self.FP.lower()), self.CLEAN)

    def test_coding_agent_sentence_rewritten(self):
        self.assertEqual(P.sanitize_text(self.FP2), self.CLEAN)

    def test_built_on_variant_rewritten(self):
        self.assertEqual(P.sanitize_text("You are an AI assistant built on DeepSeek."),
                         self.CLEAN)

    def test_embedded_in_system_prompt(self):
        text = self.FP + "\n\nYou are a coding agent."
        out = P.sanitize_text(text)
        self.assertEqual(out, self.CLEAN + "\n\nYou are a coding agent.")
        self.assertFalse(P.has_fingerprint(out))

    def test_idempotent(self):
        once = P.sanitize_text(self.FP)
        self.assertEqual(P.sanitize_text(once), once)

    def test_partial_phrases_untouched(self):
        # 只有连续整句是已确认的指纹；不构成自述句的文本不整句替换。
        for text in ("You are an AI agent.",
                     "You are free to use tools built by the user."):
            self.assertEqual(P.sanitize_text(text), text)
            self.assertFalse(P.has_fingerprint(text))

    def test_sanitize_messages_covers_system_role(self):
        messages = [
            {"role": "system", "content": self.FP},
            {"role": "user", "content": "hi"},
        ]
        out = P.sanitize_messages(messages)
        self.assertEqual(out[0]["content"], self.CLEAN)
        self.assertEqual(out[1]["content"], "hi")

    def test_identity_rewrite_covers_system_developer_and_assistant(self):
        # assistant 历史里的身份句同样命中上游校验（#574：同一句 400/400/400，
        # 放进 user 则 200/200/200）。user / tool 仍是用户数据，不能改写（#537）。
        messages = [
            {"role": "system", "content": self.FP},
            {"role": "developer", "content": self.FP2},
            {"role": "user", "content": self.FP},
            {"role": "assistant", "content": self.FP2},
            {"role": "tool", "content": self.FP},
        ]
        out = P.sanitize_messages(messages)
        self.assertEqual(out[0]["content"], self.CLEAN)
        self.assertEqual(out[1]["content"], self.CLEAN)
        self.assertEqual(out[2]["content"], self.FP)
        self.assertEqual(out[3]["content"], self.CLEAN)
        self.assertEqual(out[4]["content"], self.FP)


class DshGuiParagraphTests(unittest.TestCase):
    """DSH 系统提示里 GUI 描述段的渠道指纹（#570）。

    A 句 + B 句连续出现即命中 11-128；实测改 A 句、改 B 句、或在两句之间插东西都会
    失配。#579 只调整 A 句尾的语序，实测仍 400；#574 进一步确认品牌词是必要条件，
    于是把散文里的 `DeepSeek Harness` 缩成 `DeepSeek`。两处都只动已确认的片段，
    连字符 / 下划线形式的路径与包名保持原样。
    """

    A = ('You are interacting with the user through the DeepSeek Harness Web GUI '
         'whose URL the runtime context names.')
    A_REWORDED = ('You are interacting with the user through the DeepSeek Web GUI '
                  'whose URL is named in the runtime context.')
    B = ('When the user refers to "this page", "this GUI", or "this app" without naming '
         'another target, they mean this GUI.')
    GUI = A + " " + B

    def test_gui_paragraph_in_system_reworded(self):
        out = P.sanitize_messages([{"role": "system", "content": self.GUI}])
        self.assertEqual(out[0]["content"], self.A_REWORDED + " " + self.B)

    def test_brand_word_reduced(self):
        # #579 只改语序，实测仍 400；#574 确认品牌词是必要条件，缩成
        # `DeepSeek` 后通过，句子其余部分不动。
        out = P.sanitize_text(self.GUI)
        self.assertIn("DeepSeek Web GUI", out)
        self.assertNotIn("DeepSeek Harness", out)
        self.assertNotIn("whose URL the runtime context names", out)

    def test_gui_paragraph_in_developer_reworded(self):
        out = P.sanitize_messages([{"role": "developer", "content": self.GUI}])
        self.assertEqual(out[0]["content"], self.A_REWORDED + " " + self.B)

    def test_user_and_tool_untouched(self):
        out = P.sanitize_messages([
            {"role": "user", "content": self.GUI},
            {"role": "tool", "content": self.GUI},
        ])
        self.assertEqual(out[0]["content"], self.GUI)
        self.assertEqual(out[1]["content"], self.GUI)

    def test_brand_word_reduced_outside_the_sentence(self):
        # 品牌词缩减不锚定句子：`DeepSeek Harness` 与 GUI 描述句组合才触发，
        # 但只缩一个词、语义不变，比按句子形状做特判更稳。
        for text, want in (("DeepSeek Harness", "DeepSeek"),
                           ("the DeepSeek Harness project", "the DeepSeek project"),
                           ("You are interacting with the user through the DeepSeek Harness CLI.",
                            "You are interacting with the user through the DeepSeek CLI.")):
            self.assertEqual(P.sanitize_text(text), want, text)

    def test_hyphenated_identifiers_untouched(self):
        # 路径 / 包名 / 仓库地址里的连字符形式必须原样保留。
        for text in (
            "github.com/deepseek-ai/deepseek-harness/tree/main",
            "import deepseek_harness as dsh",
            "/opt/deepseek-harness/app",
        ):
            self.assertEqual(P.sanitize_text(text), text, text)

    def test_case_insensitive_and_idempotent(self):
        out = P.sanitize_text(self.GUI.lower())
        self.assertEqual(
            out,
            self.GUI.lower().replace("whose url the runtime context names",
                                     "whose URL is named in the runtime context")
                          .replace("deepseek harness", "DeepSeek"))
        self.assertEqual(P.sanitize_text(out), out)

    def test_wire_body_carries_the_rewrite(self):
        # 走完整链路再断言一次：规则接上了还不够，得确认 build_upstream_body
        # 真的用它构造出站 body（漏接时单测全绿、线上照旧 400）。
        body = P.build_upstream_body({
            "model": "deepseek-v4.1-flash",
            "messages": [{"role": "system", "content": self.GUI},
                         {"role": "user", "content": "hi"}]})
        system = body["messages"][0]["content"]
        self.assertIn("DeepSeek Web GUI", system)
        self.assertNotIn("DeepSeek Harness", system)
        self.assertIn("whose URL is named in the runtime context", system)


class DshFileReferenceParagraphTests(unittest.TestCase):
    """DSH 系统提示里文件引用说明段的渠道指纹。

    `@deepseek-ai/dsh-file-reference`（官方插件，packages/context/file-reference）
    注入的 FILE_REFERENCE_PROMPT 里，这两句连续出现即命中 11-128，与 #570 的 GUI
    段同一套「A 句 + B 句连续出现」判据：

        A: Tokens prefixed with @ are paths the user explicitly referenced.
        B: Relative paths resolve from the workspace root; absolute paths identify
           files or directories on the host.

    2026-10-28 二分实测（稳定复现 3/3）：A 单独 200、B 单独 200、A+B 连续 400；
    改 A 措辞 / 改 B 措辞 / 两句之间插一句话 / 换序 / 合并成一句，全部 200；
    只把 B 的 `absolute` 换成 `full` 无效（说明不是单词级匹配）。
    这里只在 A 句补一个 `that` —— 一个词不多不少，语义完全不变。
    """

    A = ("Tokens prefixed with @ are paths the user explicitly referenced.")
    A_REWORDED = ("Tokens prefixed with @ are paths that the user explicitly "
                  "referenced.")
    B = ("Relative paths resolve from the workspace root; absolute paths identify "
         "files or directories on the host.")
    FILEREF = A + " " + B

    def test_file_reference_paragraph_in_system_rewritten(self):
        out = P.sanitize_messages([{"role": "system", "content": self.FILEREF}])
        self.assertEqual(out[0]["content"], self.A_REWORDED + " " + self.B)

    def test_file_reference_paragraph_in_developer_rewritten(self):
        out = P.sanitize_messages([{"role": "developer", "content": self.FILEREF}])
        self.assertEqual(out[0]["content"], self.A_REWORDED + " " + self.B)

    def test_file_reference_paragraph_in_assistant_rewritten(self):
        # assistant 历史同样被 #574 确认会触发，规则一并覆盖。
        out = P.sanitize_messages([{"role": "assistant", "content": self.FILEREF}])
        self.assertEqual(out[0]["content"], self.A_REWORDED + " " + self.B)

    def test_user_and_tool_untouched(self):
        # user / tool 里的同句属用户数据，按 #537 口径不改写。
        out = P.sanitize_messages([
            {"role": "user", "content": self.FILEREF},
            {"role": "tool", "content": self.FILEREF},
        ])
        self.assertEqual(out[0]["content"], self.FILEREF)
        self.assertEqual(out[1]["content"], self.FILEREF)

    def test_sentence_a_alone_also_rewritten(self):
        # 只补一个 that、语义不变，所以单独出现时改写同样安全；
        # 不必依赖 B 句同时在场（B 的变体不该让规则失效）。
        out = P.sanitize_text(self.A)
        self.assertEqual(out, self.A_REWORDED)

    def test_idempotent(self):
        once = P.sanitize_text(self.FILEREF)
        twice = P.sanitize_text(once)
        self.assertEqual(once, twice)
        self.assertEqual(twice.count("paths that the user"), 1)

    def test_case_insensitive(self):
        out = P.sanitize_text(self.FILEREF.lower())
        self.assertIn("paths that the user", out.lower())

    def test_unrelated_text_untouched(self):
        for text in (
            "The workspace root is /opt/workbuddy-hub.",
            "Relative paths resolve from the workspace root.",
            "Tokens prefixed with @ are file references.",
        ):
            self.assertEqual(P.sanitize_text(text), text, text)

    def test_wire_body_carries_the_rewrite(self):
        # 走完整链路再断言一次：规则接上了还不够，得确认 build_upstream_body
        # 真的用它构造出站 body（漏接时单测全绿、线上照旧 400）。
        body = P.build_upstream_body({
            "model": "deepseek-v4.1-flash",
            "messages": [{"role": "system", "content": self.FILEREF},
                         {"role": "user", "content": "hi"}]})
        system = body["messages"][0]["content"]
        self.assertIn("paths that the user explicitly referenced", system)
        self.assertNotIn("paths the user explicitly referenced", system)


class AssistantHistoryFingerprintTests(unittest.TestCase):
    """#574：assistant 历史里的身份句 / 品牌 + GUI 组合。

    同一句放在 user 里 200/200/200，放在 assistant 里 400/400/400；只把
    `DeepSeek Harness` 缩成 `DeepSeek` 就能让真实 129KB 请求通过。
    """

    IDENTITY = "You are an AI agent powered by DeepSeek Harness."
    CLEAN = "You are a helpful AI assistant."
    GUI = ('You are interacting with the user through the DeepSeek Harness Web GUI '
           'whose URL the runtime context names. When the user refers to "this page", '
           '"this GUI", or "this app", they mean this GUI.')

    def test_assistant_identity_sentence_rewritten(self):
        out = P.sanitize_messages([{"role": "assistant", "content": self.IDENTITY}])
        self.assertEqual(out[0]["content"], self.CLEAN)

    def test_assistant_gui_paragraph_rewritten(self):
        out = P.sanitize_messages([{"role": "assistant", "content": self.GUI}])
        self.assertNotIn("DeepSeek Harness", out[0]["content"])
        self.assertIn("DeepSeek Web GUI", out[0]["content"])

    def test_user_and_tool_history_untouched(self):
        out = P.sanitize_messages([
            {"role": "user", "content": self.IDENTITY},
            {"role": "tool", "content": self.GUI},
        ])
        self.assertEqual(out[0]["content"], self.IDENTITY)
        self.assertEqual(out[1]["content"], self.GUI)

    def test_hyphenated_paths_in_assistant_untouched(self):
        for text in ("github.com/deepseek-ai/deepseek-harness/tree/main",
                     "import deepseek_harness as dsh",
                     "/opt/deepseek-harness/app"):
            out = P.sanitize_messages([{"role": "assistant", "content": text}])
            self.assertEqual(out[0]["content"], text, text)

    def test_wire_body_carries_assistant_rewrite(self):
        body = P.build_upstream_body({
            "model": "deepseek-v4.1-flash",
            "messages": [
                {"role": "assistant", "content": self.IDENTITY},
                {"role": "user", "content": self.IDENTITY},
            ]})
        by_role = {}
        for m in body["messages"]:
            by_role.setdefault(m.get("role"), m.get("content"))
        self.assertEqual(by_role["assistant"], self.CLEAN)
        self.assertEqual(by_role["user"], self.IDENTITY)


class IdentityVerbVariantsTests(unittest.TestCase):
    """同类自述句的其它动词搭配（built by / created by / …）与省略名词的形态。

    上游只按「自我介绍句式 + 品牌」判，不区分动词；1.7.18 只覆盖 powered by /
    built on 两种，补全动词表后这些变体同样应被中性化，而分词修饰宾语的句子
    （"tools built by …"）不能被误伤。
    """

    CLEAN = "You are a helpful AI assistant."

    def test_built_by_variant_rewritten(self):
        # 专名 + 逗号 + built by 的形态（客户端默认身份句常见写法）。
        self.assertEqual(
            P.sanitize_text("You are Example Agent, built by Example Corp. Be direct."),
            self.CLEAN + " Be direct.")

    def test_created_developed_made_trained_variants(self):
        for sentence in (
            "You are a coding assistant created by Example Corp.",
            "You are an AI agent developed by Example Corp.",
            "You are a chat model made by Example Corp.",
            "You are an assistant trained by Example Corp.",
        ):
            self.assertEqual(P.sanitize_text(sentence), self.CLEAN, sentence)

    def test_bare_powered_by_without_noun(self):
        self.assertEqual(
            P.sanitize_text("You are powered by the model named glm-5.3-flash."),
            self.CLEAN)

    def test_participle_modifying_object_is_untouched(self):
        for sentence in (
            "You are free to use tools built by the user.",
            "You are a coding agent pairing with the user inside their codebase.",
        ):
            self.assertEqual(P.sanitize_text(sentence), sentence, sentence)

    def test_variants_scoped_to_system_and_developer(self):
        fp = "You are Example Agent, built by Example Corp."
        out = P.sanitize_messages([
            {"role": "system", "content": fp},
            {"role": "developer", "content": fp},
            {"role": "user", "content": fp},
        ])
        self.assertEqual(out[0]["content"], self.CLEAN)
        self.assertEqual(out[1]["content"], self.CLEAN)
        self.assertEqual(out[2]["content"], fp)


if __name__ == "__main__":
    unittest.main()

