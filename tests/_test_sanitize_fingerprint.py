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


class DshFileReferenceParagraphTests(unittest.TestCase):
    """DSH 系统提示里「@ 文件引用说明段」的渠道指纹。

    DeepSeek Harness 的官方插件 `@deepseek-ai/dsh-file-reference`
    （packages/context/file-reference）会往系统提示里注入 FILE_REFERENCE_PROMPT。
    这一段被上游当成「非官方客户端」的特征，回 400 / code 11-128
    （displayTips：这次调用没通过渠道校验）。

    2026-10-28 在本机 CN 号池实测，用来定指纹边界（不是猜）：

        · 只发 A+B 两句 → 200
        · 整段五句连排 → 400，稳定复现
        · 整段里 A 句补一个 `that` → 200

    也就是说匹配单位是**整段连续文本**：截前两句测会误判成「已修复」，
    改 A 句一个词就能让它失配。修法因此取最小改动——只在 A 句补一个 `that`，
    一个词不多不少，B 句及其后所有句子原样保留，语义完全不变；负向前瞻
    (?!that ) 保证幂等。作用域与 GUI 段同口径：system / developer / assistant，
    user / tool 里的同句属用户数据不改写（#537）。
    """

    A = "Tokens prefixed with @ are paths the user explicitly referenced."
    A_FIXED = "Tokens prefixed with @ are paths that the user explicitly referenced."
    B = ("Relative paths resolve from the workspace root; absolute paths identify "
         "files or directories on the host.")
    TAIL = (" A trailing slash marks a directory: list it when its contents matter. "
            "Anything else is a file: use the read tool when its contents are needed, "
            "and do not claim to have inspected it before reading. "
            "@\"...\" quotes a path containing spaces.")
    PARA = A + " " + B + TAIL
    PARA_FIXED = A_FIXED + " " + B + TAIL

    def test_full_paragraph_rewritten(self):
        self.assertEqual(P.sanitize_text(self.PARA), self.PARA_FIXED)

    def test_only_sentence_a_changes(self):
        # B 句及其后所有句子必须逐字保留：改的是措辞，不是内容。
        out = P.sanitize_text(self.PARA)
        self.assertIn(self.B + self.TAIL, out)
        self.assertNotIn(" are paths the user ", out)

    def test_idempotent(self):
        once = P.sanitize_text(self.PARA)
        self.assertEqual(P.sanitize_text(once), once)
        self.assertFalse(P.has_fingerprint(once))

    def test_sentence_a_alone_rewritten(self):
        # A 句单独出现时改写同样安全（只补一个词），不必等 B 句在场；
        # 这样 DSH 换掉后半段措辞也不会让规则失效。
        self.assertEqual(P.sanitize_text(self.A), self.A_FIXED)

    def test_case_insensitive(self):
        self.assertEqual(P.sanitize_text(self.PARA.lower()), self.PARA_FIXED.lower())

    def test_embedded_in_system_prompt(self):
        text = "You are a coding agent.\n\n" + self.PARA + "\n\nMore rules follow."
        self.assertEqual(
            P.sanitize_text(text),
            "You are a coding agent.\n\n" + self.PARA_FIXED + "\n\nMore rules follow.")

    def test_scoped_to_system_developer_and_assistant(self):
        out = P.sanitize_messages([
            {"role": "system", "content": self.PARA},
            {"role": "developer", "content": self.PARA},
            {"role": "assistant", "content": self.PARA},
            {"role": "user", "content": self.PARA},
            {"role": "tool", "content": self.PARA},
        ])
        for i in range(3):
            self.assertEqual(out[i]["content"], self.PARA_FIXED, out[i]["role"])
        # user / tool 里的同句是用户数据，改写会让模型看到与磁盘不一致的内容。
        self.assertEqual(out[3]["content"], self.PARA)
        self.assertEqual(out[4]["content"], self.PARA)

    def test_unrelated_at_paths_untouched(self):
        for text in ("mail me at foo@bar.com", "npm i @deepseek-ai/dsh",
                     "see C:\\work\\@cache for details"):
            self.assertEqual(P.sanitize_text(text), text)


class CodexIntroFingerprintTests(unittest.TestCase):
    """Codex 基础提示词首段（#743）。

    旧规则把中性句改写成 ``You are a coding agent running in the Codex CLI
    tool, ...``，而 ``SANITIZE_FEATURES`` 恰好在找 ``... running in the Codex
    CLI``，于是 sanitize 之后 has_fingerprint() 仍为 True，出站 body 继续
    11-128。这里钉住两件事：首段必须被中性化；改写必须是幂等的。
    """

    INTRO = ("You are a coding agent running in the Codex CLI, a terminal-based "
             "coding assistant.")
    INTRO_TOOL = ("You are a coding agent running in the Codex CLI tool, a "
                  "terminal-based coding assistant.")
    CLEAN = "You are a helpful AI assistant."

    def test_codex_intro_is_neutralized(self):
        for text in (self.INTRO, self.INTRO_TOOL):
            self.assertTrue(P.has_fingerprint(text))
            self.assertEqual(P.sanitize_text(text), self.CLEAN, text)
            self.assertFalse(P.has_fingerprint(P.sanitize_text(text)))

    def test_embedded_after_neutral_sentence_still_matches(self):
        # 真实 Codex base_instructions 的形态：中性首句 + 自述句 + 后续段落。
        text = ("You are a helpful AI assistant. " + self.INTRO +
                "\n\nYour capabilities:\n\n- Receive user prompts.")
        out = P.sanitize_text(text)
        self.assertFalse(P.has_fingerprint(out))
        self.assertIn("Your capabilities:", out)

    def test_neutral_sentence_is_untouched(self):
        self.assertEqual(P.sanitize_text(self.CLEAN), self.CLEAN)
        self.assertFalse(P.has_fingerprint(self.CLEAN))

    def test_idempotent(self):
        once = P.sanitize_text(self.INTRO)
        self.assertEqual(P.sanitize_text(once), once)


class LegacyRewriteFingerprintTests(unittest.TestCase):
    """The two rewrites that regressed into no-ops in v1.10.5 (issue #796)."""

    def test_main_branch_fingerprint_is_rewritten(self):
        source = "Main branch (you will usually use this for PRs): main"
        out = P.sanitize_text(source)
        self.assertEqual(
            out, "Default branch (you will usually use this for PRs): main")
        self.assertNotIn("Main branch (", out)

    def test_feedback_fingerprint_is_rewritten(self):
        source = ("To give feedback, users should report the issue at "
                  "https://github.com/anthropics/claude-code/issues")
        out = P.sanitize_text(source)
        self.assertEqual(
            out,
            "To provide feedback, users should report the issue at "
            "https://github.com/anthropics/claude-code/issues")
        self.assertNotIn("To give feedback,", out)

    def test_rewrites_are_not_noops(self):
        for old, new in P.SANITIZE_REWRITES:
            if old == "11-128":
                continue
            self.assertNotEqual(old, new, "rewrite is a no-op: %r" % old)


class ClaudeCodeIntroFingerprintTests(unittest.TestCase):
    """Claude Code 基础提示词首段（#733）。

    报告者把提示词模式改成「替换」后 400 消失，确认指纹在 system 文本里。
    旧规则把 ``Anthropic's official CLI for Claude`` 改写成 ``... CLI tool
    for Claude``，反而把 ``You are Claude Code`` 保留下来；这里按 #743 处理
    Codex 自述句的同一套办法整句中性化，并钉住 Anthropic 转换后的出站链路。
    """

    INTRO = "You are Claude Code, Anthropic's official CLI tool for Claude."
    INTRO_NO_TOOL = "You are Claude Code, Anthropic's official CLI for Claude."
    INTRO_CODE = "You are Claude Code, Anthropic\u2019s official CLI tool for Claude Code."
    CLEAN = "You are a helpful AI assistant."

    def test_intro_variants_neutralized(self):
        for text in (self.INTRO, self.INTRO_NO_TOOL, self.INTRO_CODE,
                     self.INTRO.lower(), self.INTRO.upper()):
            self.assertTrue(P.has_fingerprint(text), text)
            out = P.sanitize_text(text)
            self.assertEqual(out, self.CLEAN, text)
            self.assertFalse(P.has_fingerprint(out), text)

    def test_embedded_in_system_prompt_preserves_surrounding_text(self):
        text = "Before.\n" + self.INTRO + "\nAfter."
        out = P.sanitize_text(text)
        self.assertEqual(out, "Before.\n" + self.CLEAN + "\nAfter.")
        self.assertFalse(P.has_fingerprint(out))

    def test_idempotent(self):
        once = P.sanitize_text(self.INTRO)
        self.assertEqual(P.sanitize_text(once), once)

    def test_scoped_to_system_developer_and_assistant(self):
        out = P.sanitize_messages([
            {"role": "system", "content": self.INTRO},
            {"role": "developer", "content": self.INTRO},
            {"role": "assistant", "content": self.INTRO},
            {"role": "user", "content": self.INTRO},
            {"role": "tool", "content": self.INTRO},
        ])
        for i in range(3):
            self.assertEqual(out[i]["content"], self.CLEAN, out[i]["role"])
        self.assertEqual(out[3]["content"], self.INTRO)
        self.assertEqual(out[4]["content"], self.INTRO)

    def test_other_claude_code_mentions_untouched(self):
        for text in ("Claude Code is a CLI.", "Use Claude Code for this task."):
            self.assertEqual(P.sanitize_text(text), text)

    def test_anthropic_messages_path_carries_the_rewrite(self):
        chat = P.messages_to_chat({
            "model": "deepseek-v4.1-flash",
            "system": [{"type": "text", "text": self.INTRO}],
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 128,
        })
        body = P.build_upstream_body(chat)
        self.assertEqual(body["messages"][0]["content"], self.CLEAN)
        self.assertEqual(body["messages"][1]["content"], "hi")


if __name__ == "__main__":
    unittest.main()

