# -*- coding: utf-8 -*-
"""WorkBuddy official web_search endpoint and tool integration.

    python tests/_test_workbuddy_search.py

Covers the request contract, flat response parsing, result-count clamping,
account selection/rotation, and the format handed back to the model. All
network calls are injected; the suite never touches a real account.
"""
import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import wb_proxy as proxy
import wb_webtools as W


FLAT = {
    "query": "OpenAI company",
    "type": "text2text",
    "provider": "1",
    "total_results": 2,
    "response_time_ms": 1146,
    "results": [
        {"title": "OpenAI", "url": "https://openai.com/",
         "snippet": "AI research and deployment company.", "site": "openai.com"},
        {"title": "About", "url": "https://openai.com/about/",
         "snippet": "About OpenAI.", "site": "openai.com"},
    ],
}


class FakeResponse(object):
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _limit=-1):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class FakeAccount(object):
    def __init__(self, uid="acct-a", realm="intl", product="workbuddy"):
        self.uid = uid
        self.realm = realm
        self.product = product
        self.access_token = "token"
        self.proxy = ""
        self.enabled = True
        self.successes = 0
        self.failures = []
        self.soft_rates = []
        self.header_calls = []

    def ready(self):
        return True

    def unavailable_reason(self):
        return ""

    def chat_base_url(self):
        return "https://www.workbuddy.ai"

    def headers(self, **kwargs):
        self.header_calls.append(kwargs)
        return {
            "Authorization": "Bearer " + self.access_token,
            "X-User-Id": self.uid,
            "Content-Type": "application/json",
            "Accept": "event-stream",
            "X-Agent-Intent": "craft",
        }

    def note_success(self, model=None):
        self.successes += 1

    def note_failure(self, message):
        self.failures.append(message)

    def note_unknown_failure(self, message):
        self.failures.append(message)

    def note_soft_rate(self, message):
        self.soft_rates.append(message)
        return 1.0

    def refresh(self):
        return False


class FakePool(object):
    def __init__(self, accounts):
        self.accounts = list(accounts)
        self.picked = 0

    def count_ready(self, realm=None):
        return sum(1 for a in self.accounts if a.realm == realm)

    def pick(self, **kwargs):
        self.picked += 1
        exclude = kwargs.get("exclude") or set()
        for account in self.accounts:
            if account.realm == kwargs.get("realm") and account.uid not in exclude:
                return account
        return None


class WorkBuddySearchTests(unittest.TestCase):
    def test_flat_payload_and_wrapped_payload(self):
        parsed = W.parse_workbuddy_search_payload(FLAT, "ignored")
        self.assertEqual(parsed["provider"], "1")
        self.assertEqual(parsed["response_time_ms"], 1146)
        self.assertEqual(parsed["results"][0]["url"], "https://openai.com/")
        wrapped = {"code": 0, "data": FLAT}
        self.assertEqual(W.parse_workbuddy_search_payload(wrapped, "x")["total_results"], 2)

    def test_business_error_is_not_treated_as_empty_success(self):
        with self.assertRaises(RuntimeError) as raised:
            W.parse_workbuddy_search_payload({"code": 15003, "msg": "unsupported"}, "x")
        self.assertIn("15003", str(raised.exception))
        with self.assertRaises(ValueError):
            W.parse_workbuddy_search_payload(["not", "an", "object"], "x")

    def test_result_count_clamps(self):
        self.assertEqual(W.clamp_web_result_count(None), 10)
        self.assertEqual(W.clamp_web_result_count(0), 10)
        self.assertEqual(W.clamp_web_result_count(-1), 10)
        self.assertEqual(W.clamp_web_result_count(1), 1)
        self.assertEqual(W.clamp_web_result_count(99), W.MAX_RESULTS)

    def test_search_formats_sources_for_the_model(self):
        text = W.search("OpenAI company", 2, runner=lambda q, n: FLAT)
        self.assertIn("Search results for: OpenAI company", text)
        self.assertIn("https://openai.com/", text)
        self.assertEqual(W.sources_from_result(text)[0]["title"], "OpenAI")

    def test_search_error_does_not_fall_back_to_local_engines(self):
        def fail(_q, _n):
            raise RuntimeError("boom")
        with mock.patch.object(W, "_search_fanout", side_effect=AssertionError("fallback ran")):
            text = W.search("OpenAI", 5, runner=fail)
        self.assertIn("WorkBuddy official web search failed", text)
        self.assertIn("boom", text)

    def test_execute_accepts_result_count_aliases(self):
        seen = []
        out = W.execute("web_search", {"query": "OpenAI", "max_results": 20},
                        search_runner=lambda q, n: seen.append((q, n)) or FLAT)
        self.assertEqual(seen, [("OpenAI", 10)])
        self.assertIn("Search results", out)

    def test_request_contract_and_header_overrides(self):
        account = FakeAccount()
        request = proxy.build_workbuddy_search_request(
            account, "OpenAI", 5, "session-1", "turn-1")
        self.assertEqual(request.full_url, "https://www.workbuddy.ai/agenttool/v1/search")
        self.assertEqual(request.data.decode("utf-8"),
                         '{"query":"OpenAI","type":"text2text","max_results":5}')
        headers = dict(request.header_items())
        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(headers["Content-type"], "application/json;charset=UTF-8")
        self.assertEqual(headers["X-agent-intent"], "craft")
        self.assertEqual(headers["X-agent-runtime"], "other")
        self.assertTrue(account.header_calls[0]["conversation_id"])

    def test_run_uses_preferred_account_and_parses_flat_response(self):
        account = FakeAccount()
        pool = FakePool([account])
        with mock.patch.object(proxy, "POOL", pool), \
                mock.patch.object(proxy, "pinned_account", lambda: None), \
                mock.patch.object(proxy, "apply_daily_guards", lambda: None), \
                mock.patch.object(proxy.wb_accounts, "urlopen",
                                  lambda req, timeout=30, proxy="": FakeResponse(FLAT)):
            parsed = proxy.run_workbuddy_search(
                "OpenAI company", 2, realm="intl", session_key="s",
                preferred_account=account)
        self.assertEqual(parsed["results"][0]["title"], "OpenAI")
        self.assertEqual(account.successes, 1)
        self.assertEqual(pool.picked, 0)

    def test_follow_up_wires_the_official_runner(self):
        seen = []
        holder = {
            "realm": "intl",
            "base_messages": [{"role": "user", "content": "find OpenAI"}],
            "base_body": {"model": "m", "messages": [], "tools": [W.web_search_tool_def()]},
        }
        calls = [{"name": "web_search", "arguments": '{"query":"OpenAI"}'}]
        with mock.patch.object(proxy, "run_workbuddy_search",
                               lambda query, count, **kwargs: seen.append((query, count)) or FLAT), \
                mock.patch.object(proxy, "open_upstream",
                                  lambda body, session_key=None, target_realm=None: object()):
            proxy.follow_up_with_tool_results(
                calls, holder, "m", "sess", 0.0, preferred_account=FakeAccount())
        self.assertEqual(seen, [("OpenAI", 10)])
        tool_messages = [m for m in holder["convo_messages"] if m.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertIn("https://openai.com/", tool_messages[0]["content"])
        self.assertEqual(len(holder.get("web_sources") or []), 2)

    def test_run_rotates_on_429_but_not_on_400(self):
        first = FakeAccount("acct-a")
        second = FakeAccount("acct-b")
        pool = FakePool([first, second])
        calls = {"n": 0}

        def rate_limited(req, timeout=30, proxy=""):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests", {},
                io.BytesIO(b'{"code":6004,"msg":"rate limited"}'))

        with mock.patch.object(proxy, "POOL", pool), \
                mock.patch.object(proxy, "pinned_account", lambda: None), \
                mock.patch.object(proxy, "apply_daily_guards", lambda: None), \
                mock.patch.object(proxy.time, "sleep", lambda *_: None), \
                mock.patch.object(proxy.wb_accounts, "urlopen", rate_limited):
            with self.assertRaises(RuntimeError):
                proxy.run_workbuddy_search(
                    "OpenAI", 2, realm="intl", preferred_account=first)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(pool.picked, 1)
        self.assertEqual(len(first.soft_rates), 1)

        calls["n"] = 0

        def bad_request(req, timeout=30, proxy=""):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"code":15003}'))
        with mock.patch.object(proxy, "POOL", pool), \
                mock.patch.object(proxy, "pinned_account", lambda: None), \
                mock.patch.object(proxy, "apply_daily_guards", lambda: None), \
                mock.patch.object(proxy.wb_accounts, "urlopen", bad_request):
            with self.assertRaises(RuntimeError):
                proxy.run_workbuddy_search(
                    "OpenAI", 2, realm="intl", preferred_account=first)
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
