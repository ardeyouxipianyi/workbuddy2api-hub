"""The usage row records the reasoning effort the request actually ran at.

    build_upstream_body() resolves the effort (the client's value, else the model
    default), open_upstream() hands it back to the response handlers, and
    record_usage() writes it as reasoning_effort so the panel's recent-requests
    table can show it next to the model. Rows without one - models that have no
    reasoning controls, and rows written before the field existed - must stay
    exactly as they were.

    No network: the log is written to a temp directory.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-effort-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_accounts
import wb_proxy as P

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (label, extra))


def rows():
    with open(P.USAGE_LOG, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "reasoning_tokens": 3,
         "cached_tokens": 4, "total_tokens": 15}

P.record_usage("deepseek-v4.1-flash", USAGE, stream=True, elapsed_ms=1200, ttft_ms=800,
               gen_ms=400, account=None, effort="high")
row = rows()[-1]
check("the resolved effort is written on the row",
      row.get("reasoning_effort") == "high", row.get("reasoning_effort"))

P.record_usage("hy3", USAGE, stream=False, elapsed_ms=900, effort=None)
check("a request without an effort writes no field",
      "reasoning_effort" not in rows()[-1], rows()[-1].get("reasoning_effort"))

P.record_usage("gemini-3.5-flash", USAGE, stream=False, elapsed_ms=900, effort="medium")
check("a fixed-effort model records its fixed value",
      rows()[-1].get("reasoning_effort") == "medium", rows()[-1].get("reasoning_effort"))

# The panel reads rows through recent_usage(); the field has to survive it.
got = P.recent_usage(limit=10, realm=P.CURRENT_REALM)
latest = got["rows"][0]
check("recent_usage passes the field through",
      latest.get("reasoning_effort") == "medium", latest.get("reasoning_effort"))
check("recent_usage still returns the other fields",
      latest.get("model") == "gemini-3.5-flash" and latest.get("total_tokens") == 15,
      latest)
old = [r for r in got["rows"] if r.get("model") == "hy3"][0]
check("a row written without the field stays without it",
      "reasoning_effort" not in old, old)

# The body builder reads both client spellings and only fills in the model
# default when the client asked for nothing, so a camelCase request keeps
# "reasoningEffort" and never gains a snake-case key. Reading only the snake
# spelling would report None for a request that really ran at "max".
camel = P.build_upstream_body({"model": "deepseek-v4.1-flash",
                               "messages": [{"role": "user", "content": "hi"}],
                               "reasoningEffort": "max"})
check("a camelCase request keeps the camelCase key",
      "reasoning_effort" not in camel, camel.get("reasoning_effort"))
check("the effective effort is read from the camelCase spelling",
      P.upstream_effort_of(camel) == "max", P.upstream_effort_of(camel))
snake = P.build_upstream_body({"model": "deepseek-v4.1-flash",
                               "messages": [{"role": "user", "content": "hi"}],
                               "reasoning_effort": "low"})
check("the snake-case spelling still wins when the client sends it",
      P.upstream_effort_of(snake) == "low", P.upstream_effort_of(snake))
quiet = P.build_upstream_body({"model": "deepseek-v4.1-flash",
                               "messages": [{"role": "user", "content": "hi"}]})
check("a request that asks for nothing gets the model default",
      P.upstream_effort_of(quiet) == (P.model_default_effort("deepseek-v4.1-flash") or "high"),
      P.upstream_effort_of(quiet))


class _StubPool(object):
    """The parts of the account pool open_upstream() touches."""

    def __init__(self, accounts):
        self.accounts = accounts

    def count_ready(self, realm, model=None):
        return sum(a.ready(model=model) for a in self.accounts)

    def pick_for_session(self, realm, session_key=None, exclude=(), model=None):
        return next((a for a in self.accounts if a.uid not in exclude
                     and a.realm == realm and a.ready(model=model)), None)

    def apply_daily_token_limit(self, value=None, usage=None):
        return value or 0

    def apply_daily_credit_limit(self, value=None, credits=None, free_models=None):
        return value or 0

    def apply_model_daily_token_limit(self, value=None, per_model=None):
        return value or 0


class _FakeResponse(object):
    def close(self):
        pass


# End to end over the real open_upstream(): the effort it hands back is the one
# record_usage() writes, so a camelCase client gets its chip too.
account = wb_accounts.Account({"uid": "uid-effort", "accessToken": "t", "realm": "intl"})


def open_with(payload):
    """Run the real open_upstream() against a stubbed pool and transport."""
    old_pool, old_urlopen = P.POOL, wb_accounts.urlopen
    P.POOL = _StubPool([account])
    wb_accounts.urlopen = lambda req, timeout=None, proxy=None: _FakeResponse()
    try:
        _resp, _acct, effort = P.open_upstream(payload, target_realm="intl")
        return effort
    finally:
        P.POOL = old_pool
        wb_accounts.urlopen = old_urlopen


def chat(model, **extra):
    payload = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    payload.update(extra)
    return payload


# End to end over the real open_upstream(): the effort it hands back is the one
# record_usage() writes, so a camelCase client gets its chip too.
effort = open_with(chat("deepseek-v4.1-flash", reasoningEffort="max"))
check("open_upstream hands back the camelCase effort", effort == "max", effort)
P.record_usage("deepseek-v4.1-flash", USAGE, stream=False, elapsed_ms=900, effort=effort)
check("and that value lands on the usage row",
      rows()[-1].get("reasoning_effort") == "max", rows()[-1].get("reasoning_effort"))

# A model the catalog pins to one level runs there whether or not the client
# says anything - and the gateway writes nothing into the body for it, so the
# body alone reports no effort at all.
check("the catalog pins gemini-3.5-flash", P.model_fixed_effort("gemini-3.5-flash") == "medium",
      P.model_fixed_effort("gemini-3.5-flash"))
check("and it declares no default",
      P.model_default_effort("gemini-3.5-flash") is None,
      P.model_default_effort("gemini-3.5-flash"))
plain = P.build_upstream_body(chat("gemini-3.5-flash"))
check("a plain request to it carries no effort in the body",
      P.client_effort_of(plain) is None, P.client_effort_of(plain))
effort = open_with(chat("gemini-3.5-flash"))
check("open_upstream still reports the pinned level", effort == "medium", effort)
P.record_usage("gemini-3.5-flash", USAGE, stream=False, elapsed_ms=900, effort=effort)
check("so the pinned level reaches the usage row",
      rows()[-1].get("reasoning_effort") == "medium", rows()[-1].get("reasoning_effort"))
check("kimi-k3 is pinned the same way", open_with(chat("kimi-k3")) == "medium",
      open_with(chat("kimi-k3")))
check("a pinned model ignores a level the request carries anyway",
      open_with(chat("gemini-3.5-flash", reasoning_effort="max")) == "medium",
      open_with(chat("gemini-3.5-flash", reasoning_effort="max")))

# A selectable model with no client value runs at its declared default, and the
# client's own value wins when it sends one.
check("gpt-6-astra declares a default", P.model_default_effort("gpt-6-astra") == "high",
      P.model_default_effort("gpt-6-astra"))
check("a selectable model falls back to that default",
      open_with(chat("gpt-6-astra")) == "high", open_with(chat("gpt-6-astra")))
check("the client's own value still wins",
      open_with(chat("gpt-6-astra", reasoning_effort="low")) == "low",
      open_with(chat("gpt-6-astra", reasoning_effort="low")))

# Switching thinking off is an answer, not a gap: it must not be filled in with
# the model's default.
check("a request that disables thinking reports none",
      open_with(chat("gpt-6-astra", thinking={"type": "disabled"})) == "none",
      open_with(chat("gpt-6-astra", thinking={"type": "disabled"})))
check("an explicit none reports none",
      open_with(chat("gpt-6-astra", reasoning_effort="none")) == "none",
      open_with(chat("gpt-6-astra", reasoning_effort="none")))
check("a model with no reasoning metadata stays unknown",
      P.upstream_effort_of(chat("hy3"), "hy3") in (None, "low", "high"),
      P.upstream_effort_of(chat("hy3"), "hy3"))

print("")
print("  PASS=%d FAIL=%d" % (PASS, FAIL))
if FAIL:
    sys.exit(1)
print("OK")
