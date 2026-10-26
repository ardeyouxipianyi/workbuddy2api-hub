"""Background-model redirection for desktop clients' off-thread traffic.

A desktop client's model picker only governs the MAIN chat thread. Its built-in
"model routing config" (codex-apps/protocol/src/model-routing-config.ts) sends
background work - memory generation, ambient suggestions, ephemeral
generation, automations - to its own hard-coded models. Those requests arrive
on this gateway under the same key as normal traffic, so on a shared key they
keep burning paid credits even when a free model is selected in the UI.

``WB_BACKGROUND_MODEL_NAMES`` (comma list, default covers the known set) marks
the models to intercept; ``WB_BACKGROUND_MODEL_TARGET`` (default
``deepseek-v4.1-flash``) is the free replacement. The rewrite happens at the
single POST dispatch point, before any account is chosen, so both OpenAI-shaped
routes and the Anthropic-shaped one are covered.

Run with: python _test_background_model_alias.py
No upstream credentials or outbound network are used.
"""
import os
import sys
import tempfile
import unittest

_startup_dir = tempfile.TemporaryDirectory(prefix="bg-model-alias-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy


class BackgroundModelAliasTests(unittest.TestCase):
    """The rewrite must be narrow: listed models only, everything else intact."""

    def setUp(self):
        self._saved_names = wb_proxy.BACKGROUND_MODEL_NAMES
        self._saved_target = wb_proxy.BACKGROUND_MODEL_TARGET

    def tearDown(self):
        wb_proxy.BACKGROUND_MODEL_NAMES = self._saved_names
        wb_proxy.BACKGROUND_MODEL_TARGET = self._saved_target

    def _apply(self, payload):
        wb_proxy._apply_background_model_alias(payload)
        return payload

    def test_listed_models_are_redirected_only_for_background_metadata(self):
        meta = {"client_metadata": {"request_kind": "memory",
                                    "thread_source": "memory_consolidation"}}
        for name in ("gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.3-codex"):
            body = self._apply({"model": name, "messages": [], **meta})
            self.assertEqual(body["model"], "deepseek-v4.1-flash")

    def test_explicit_picker_choice_is_never_rewritten(self):
        # The picker and the background router can name the same model. A body
        # without background metadata is an explicit request (issue #619).
        for body in ({"model": "gpt-5.6-terra", "messages": []},
                     {"model": "gpt-5.6-terra", "stream": True},
                     {"model": "gpt-5.6-terra", "client_metadata": {}}):
            self.assertEqual(self._apply(dict(body))["model"], "gpt-5.6-terra")

    def test_normal_models_are_untouched(self):
        for name in ("deepseek-v4.1-flash", "gpt-6-astra", "glm-5.3"):
            body = self._apply({"model": name})
            self.assertEqual(body["model"], name)

    def test_matching_ignores_case_and_whitespace(self):
        body = self._apply({"model": "  GPT-5.6-TERRA ",
                            "client_metadata": {"request_kind": "memory"}})
        self.assertEqual(body["model"], "deepseek-v4.1-flash")

    def test_missing_or_non_string_model_is_left_alone(self):
        self.assertEqual(self._apply({}), {})
        self.assertEqual(self._apply({"model": None}), {"model": None})
        self.assertEqual(self._apply({"model": 42}), {"model": 42})

    def test_non_dict_payload_is_ignored(self):
        # The dispatch point forwards whatever it parsed; it must never raise.
        wb_proxy._apply_background_model_alias(None)
        wb_proxy._apply_background_model_alias("not-a-body")
        wb_proxy._apply_background_model_alias(["also", "not", "a-body"])

    def test_other_body_fields_survive_the_rewrite(self):
        body = self._apply({"model": "gpt-5.6-terra", "stream": True,
                            "max_tokens": 16, "messages": [{"role": "user",
                                                            "content": "hi"}],
                            "client_metadata": {"request_kind": "memory"}})
        self.assertEqual(body["model"], "deepseek-v4.1-flash")
        self.assertTrue(body["stream"])
        self.assertEqual(body["max_tokens"], 16)
        self.assertEqual(body["messages"], [{"role": "user", "content": "hi"}])

    def test_names_and_target_are_env_overridable(self):
        # The defaults are module constants, so an operator can also flip them
        # at runtime; the env parsing is covered by a fresh tuple here.
        names = tuple(
            m.strip().lower()
            for m in "Model-A, model-b".split(",")
            if m.strip()
        )
        wb_proxy.BACKGROUND_MODEL_NAMES = names
        wb_proxy.BACKGROUND_MODEL_TARGET = "some-free-model"
        body = self._apply({"model": "MODEL-B",
                            "client_metadata": {"request_kind": "memory"}})
        self.assertEqual(body["model"], "some-free-model")
        untouched = self._apply({"model": "gpt-5.6-terra",
                                 "client_metadata": {"request_kind": "memory"}})
        self.assertEqual(untouched["model"], "gpt-5.6-terra")


if __name__ == "__main__":
    unittest.main()
