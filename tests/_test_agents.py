"""One-click agent integration: hermetic unit tests (no network access).

Covers the text-level editors in wb_agents.py (YAML / TOML / dotenv / JSON
deep-merge) and the full integrate()/restore() round trip against a temporary
home directory, plus the client registry itself.

    python tests/_test_agents.py
"""

import json
import os
import re
import shutil
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_agents as A

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


# ---------------------------------------------------------------------------
# YAML editor
# ---------------------------------------------------------------------------

print("[1] yaml_upsert_inline creates a nested chain from empty text")

text = A.yaml_upsert_inline("", "llm-pi-ai.providers.wb-proxy",
                            {"baseURL": "http://x/v1", "api": "openai-completions"})
check("provider block was created",
      'wb-proxy: {"baseURL": "http://x/v1", "api": "openai-completions"}' in text,
      text)
check("parent keys created at depth 0 and 2",
      "llm-pi-ai:" in text and "\n  providers:" in text, text)
check("leaf is indented under providers",
      '\n    wb-proxy: {"baseURL"' in text, text)

print()
print("[2] yaml_upsert_inline keeps unrelated keys and writes the new provider")

before = (
    "# hand-written comment\n"
    "agent-default-model:\n"
    "  provider: other\n"
    "  model: old-model\n"
    "\n"
    "llm-pi-ai:\n"
    "  timeout: 30\n"
    "  providers:\n"
    "    deepseek:\n"
    "      displayName: DeepSeek\n"
    "      api: openai-completions\n"
)
after = A.yaml_upsert_inline(
    before, "llm-pi-ai.providers.wb-proxy", {"apiKeyEnv": "WB_PROXY_API_KEY"})
check("comment line preserved", after.startswith("# hand-written comment"), after)
check("other provider block preserved",
      "    deepseek:\n      displayName: DeepSeek\n      api: openai-completions"
      in after, after)
check("sibling key under llm-pi-ai preserved", "  timeout: 30" in after, after)
check("default model block preserved",
      "  provider: other\n  model: old-model" in after, after)
check("wb-proxy block written", "wb-proxy:" in after
      and '"apiKeyEnv": "WB_PROXY_API_KEY"' in after, after)

print()
print("[3] repeated yaml_upsert_inline does not duplicate the block")

twice = A.yaml_upsert_inline(after, "llm-pi-ai.providers.wb-proxy",
                             {"apiKeyEnv": "WB_PROXY_API_KEY", "v": 2})
check("only one wb-proxy key", twice.count("wb-proxy:") == 1, twice)
check("value was replaced in place", '"v": 2' in twice, twice)
check("old value no longer present",
      '"apiKeyEnv": "WB_PROXY_API_KEY"}' not in twice.replace('"v": 2', ""),
      twice)

print()
print("[4] agent-default-model upsert creates and then replaces")

t = A.yaml_upsert_inline("", "agent-default-model",
                         {"provider": "wb-proxy", "model": "m1"})
check("default model created",
      'agent-default-model: {"provider": "wb-proxy", "model": "m1"}' in t, t)
t2 = A.yaml_upsert_inline(t, "agent-default-model",
                          {"provider": "wb-proxy", "model": "m2"})
check("default model replaced", '"model": "m2"' in t2, t2)
check("still a single occurrence", t2.count("agent-default-model:") == 1, t2)

print()
print("[5] yaml_refs_upsert: empty file, keep other keys, replace, reject refs: {}")

r = A.yaml_refs_upsert("", "WB_PROXY_API_KEY", "k-1")
check("refs block created on empty text",
      r == "refs:\n  WB_PROXY_API_KEY: k-1\n", repr(r))

r2 = A.yaml_refs_upsert("refs:\n  OTHER: keep-me\n", "WB_PROXY_API_KEY", "k-2")
check("other ref preserved", "OTHER: keep-me" in r2, repr(r2))
check("new ref added inside refs", "WB_PROXY_API_KEY: k-2" in r2, repr(r2))
check("inserted under the refs block",
      r2.index("OTHER") < r2.index("WB_PROXY_API_KEY"), repr(r2))

r3 = A.yaml_refs_upsert(r2, "WB_PROXY_API_KEY", "k-3")
check("existing ref replaced", "WB_PROXY_API_KEY: k-3" in r3, repr(r3))
check("no duplicate ref lines", r3.count("WB_PROXY_API_KEY:") == 1, repr(r3))

try:
    A.yaml_refs_upsert("refs: {}\n", "WB_PROXY_API_KEY", "k-x")
    raised = ""
except A.AgentConfigError as exc:
    raised = str(exc)
check("refs: {} inline mapping raises AgentConfigError",
      "cannot upsert into `refs:" in raised, repr(raised))

print()
print("[6] yaml_upsert_inline through an inline flow parent expands the block")

t = A.yaml_upsert_inline("providers: {a: 1}\n", "providers.wb-proxy", {"x": 1})
check("inline flow parent expanded",
      "providers:\n  a: 1\n" in t and 'wb-proxy: {"x": 1}' in t, repr(t))

# ---------------------------------------------------------------------------
# TOML editor
# ---------------------------------------------------------------------------

print()
print("[7] toml on empty text: top keys before the provider table")

t = A.toml_upsert_top_key("", "model_provider", "wb-proxy")
check("top key created on empty text",
      t == 'model_provider = "wb-proxy"\n', repr(t))
t = A.toml_upsert_top_key(t, "model", "m1")
t = A.toml_upsert_table(t, "model_providers.wb-proxy",
                        {"name": "WB Proxy", "base_url": "http://x/v1",
                         "wire_api": "responses"})
check("top keys stay before the table",
      t.index("model_provider") < t.index("[model_providers.wb-proxy]"), repr(t))
check("table body written", 'wire_api = "responses"' in t, repr(t))

print()
print("[8] toml top key inserts before the first [table] and replaces existing")

t = A.toml_upsert_table("", "some_other", {"a": 1})
t = A.toml_upsert_top_key(t, "model_provider", "old")
t = A.toml_upsert_top_key(t, "model_provider", "wb-proxy")
check("existing top key replaced in place",
      t.count('model_provider = "wb-proxy"') == 1
      and '"old"' not in t, repr(t))
check("top key lives before the first table",
      t.index("model_provider") < t.index("[some_other]"), repr(t))
check("existing table untouched", "[some_other]\na = 1" in t, repr(t))

print()
print("[9] repeated toml application is idempotent")

t1 = ""
t1 = A.toml_upsert_top_key(t1, "model_provider", "wb-proxy")
t1 = A.toml_upsert_top_key(t1, "model", "m1")
t1 = A.toml_upsert_table(t1, "model_providers.wb-proxy",
                         {"name": "WB Proxy", "base_url": "http://x/v1",
                          "wire_api": "responses", "experimental_bearer_token": "k"})
t2 = t1
t2 = A.toml_upsert_top_key(t2, "model_provider", "wb-proxy")
t2 = A.toml_upsert_top_key(t2, "model", "m1")
t2 = A.toml_upsert_table(t2, "model_providers.wb-proxy",
                         {"name": "WB Proxy", "base_url": "http://x/v1",
                          "wire_api": "responses", "experimental_bearer_token": "k"})
check("second application changes nothing", t1 == t2, (t1, t2))

t3 = A.toml_upsert_table("", "model_providers.wb-proxy",
                         {"name": "WB", "env_key": "OPENAI_API_KEY"})
t3 = A.toml_drop_table_keys(t3, "model_providers.wb-proxy", ("env_key",))
check("env_key is removed when the inline token takes over",
      "env_key" not in t3 and 'name = "WB"' in t3, repr(t3))
check("dropping an absent key is a no-op",
      A.toml_drop_table_keys(t3, "model_providers.wb-proxy", ("env_key",)) == t3)

print()
print("[10] toml table upsert keeps other keys in the same table")

t = A.toml_upsert_table("", "model_providers.deepseek", {"name": "DS", "x": 1})
t = A.toml_upsert_table(t, "model_providers.wb-proxy", {"name": "WB"})
check("second table appended", "[model_providers.wb-proxy]" in t, repr(t))
check("first table preserved", "[model_providers.deepseek]\nname = \"DS\"\nx = 1"
      in t, repr(t))
t = A.toml_upsert_table(t, "model_providers.deepseek", {"name": "DS2"})
check("single key replaced, sibling kept",
      'name = "DS2"' in t and "x = 1" in t, repr(t))

# ---------------------------------------------------------------------------
# dotenv editor
# ---------------------------------------------------------------------------

print()
print("[11] env_upsert: create, replace, preserve comments")

e = A.env_upsert("", {"WB_PROXY_API_KEY": "k-1"})
check("key created on empty text", e == "WB_PROXY_API_KEY=k-1\n", repr(e))
e = A.env_upsert("# comment\nWB_PROXY_API_KEY=old\nOTHER=keep\n",
                 {"WB_PROXY_API_KEY": "k-2"})
check("existing key replaced", "WB_PROXY_API_KEY=k-2" in e, repr(e))
check("comment and unrelated key preserved",
      "# comment" in e and "OTHER=keep" in e, repr(e))
check("no duplicate key lines", e.count("WB_PROXY_API_KEY=") == 1, repr(e))

# ---------------------------------------------------------------------------
# JSON deep merge
# ---------------------------------------------------------------------------

print()
print("[12] deep_merge keeps siblings, overwrites nested leaves")

merged = A.deep_merge(
    {"env": {"A": 1, "B": 2}, "keep": True},
    {"env": {"A": 9}, "added": [1, 2]})
check("untouched leaf survives", merged["env"]["B"] == 2, merged)
check("patched leaf replaced", merged["env"]["A"] == 9, merged)
check("untouched top key survives", merged["keep"] is True, merged)
check("new key added", merged["added"] == [1, 2], merged)
base = {"env": {"A": 1}}
A.deep_merge(base, {"env": {"A": 2}})
check("inputs are not mutated", base["env"]["A"] == 1, base)

# ---------------------------------------------------------------------------
# Client registry
# ---------------------------------------------------------------------------

print()
print("[13] client registry integrity")

check("all six expected clients registered",
      sorted(A.CLIENTS) == ["claude-code", "codex", "crush", "dsh", "opencode",
                            "zcode"],
      sorted(A.CLIENTS))
ids = [c["id"] for c in A.CLIENTS.values()]
check("registry ids are unique", len(set(ids)) == len(ids), ids)
ok_meta = True
for cid, client in A.CLIENTS.items():
    ok_meta = ok_meta and client["id"] == cid and client.get("label") \
        and client.get("protocol") and callable(client.get("apply")) \
        and callable(client.get("probe")) and callable(client.get("files"))
check("every client has id/label/protocol and callable apply/probe/files",
      ok_meta, ids)
for env_var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
    os.environ.pop(env_var, None)
home_paths = tempfile.mkdtemp(prefix="wb-agents-reg-")
ok_paths = True
seen = []
for cid, client in A.CLIENTS.items():
    for name, path in client["files"](home_paths).items():
        ok_paths = ok_paths and os.path.isabs(path) and home_paths in path
        seen.append((cid, name, path))
check("files(home) returns absolute paths under the injected home",
      ok_paths, seen)

# ---------------------------------------------------------------------------
# integrate / restore round trips
# ---------------------------------------------------------------------------

print()
print("[14] claude-code: pre-existing settings.json survives apply and restores byte-exact")

home = tempfile.mkdtemp(prefix="wb-agents-home-")
accounts = tempfile.mkdtemp(prefix="wb-agents-acct-")
settings_path = os.path.join(home, ".claude", "settings.json")
original_bytes = ('{\n  "theme": "dark",\n  "permissions": {"allow": ["Bash(git:*)"]},\n'
                  '  "env": {"KEEP_ME": "1"}\n}\n').encode("utf-8")
os.makedirs(os.path.dirname(settings_path))
with open(settings_path, "wb") as fh:
    fh.write(original_bytes)

result = A.integrate(accounts, "claude-code", "http://127.0.0.1:8800/v1/",
                     "sk-test-123", model="model-a", home=home)
check("integrate reports the client", result["client"] == "claude-code", result)
with open(settings_path, encoding="utf-8") as fh:
    applied = json.load(fh)
check("unrelated top-level keys preserved",
      applied.get("theme") == "dark"
      and applied.get("permissions") == {"allow": ["Bash(git:*)"]}, applied)
check("unrelated env key preserved", applied["env"].get("KEEP_ME") == "1",
      applied["env"])
check("ANTHROPIC_BASE_URL points at the bare gateway root (trailing /v1 stripped)",
      applied["env"].get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:8800",
      applied["env"])
check("ANTHROPIC_AUTH_TOKEN written",
      applied["env"].get("ANTHROPIC_AUTH_TOKEN") == "sk-test-123", applied["env"])
check("model env vars written",
      applied["env"].get("ANTHROPIC_MODEL") == "model-a"
      and applied["env"].get("ANTHROPIC_DEFAULT_SONNET_MODEL") == "model-a",
      applied["env"])
state = A._load_state(accounts)
check("integration-state.json records the client", "claude-code" in state, state)
check("state records the backup path and sha",
      bool(state["claude-code"]["files"][0].get("backup"))
      and bool(state["claude-code"]["files"][0].get("sha256")),
      state["claude-code"])

over = A.overview(accounts, home=home)
check("overview marks claude-code applied without external change",
      over["claude-code"]["applied"] is not None
      and over["claude-code"]["applied"]["external_change"] is False,
      over["claude-code"])

with open(settings_path, "a", encoding="utf-8") as fh:
    fh.write("\n// user edit\n")
over2 = A.overview(accounts, home=home)
check("a hand edit after apply reads as external_change",
      over2["claude-code"]["applied"]["external_change"] is True,
      over2["claude-code"])

restored = A.restore(accounts, "claude-code", home=home)
check("restore reports the restored file",
      restored["restored"][0]["action"] == "restored", restored)
with open(settings_path, "rb") as fh:
    after_restore = fh.read()
check("restore is byte-exact", after_restore == original_bytes,
      (after_restore, original_bytes))
check("state record cleared after restore",
      "claude-code" not in A._load_state(accounts), A._load_state(accounts))

print()
print("[15] files created by apply are deleted on restore")

home2 = tempfile.mkdtemp(prefix="wb-agents-home2-")
acct2 = tempfile.mkdtemp(prefix="wb-agents-acct2-")
A.integrate(acct2, "claude-code", "http://gw.local", "k", home=home2)
created_path = os.path.join(home2, ".claude", "settings.json")
check("apply created the file", os.path.exists(created_path))
A.restore(acct2, "claude-code", home=home2)
check("restore deleted the file it created", not os.path.exists(created_path))

print()
print("[16] repeated applies still restore to the very first original")

home3 = tempfile.mkdtemp(prefix="wb-agents-home3-")
acct3 = tempfile.mkdtemp(prefix="wb-agents-acct3-")
p3 = os.path.join(home3, ".claude", "settings.json")
orig3 = b'{"orig": true}\n'
os.makedirs(os.path.dirname(p3))
with open(p3, "wb") as fh:
    fh.write(orig3)
A.integrate(acct3, "claude-code", "http://one", "k1", model="m1", home=home3)
A.integrate(acct3, "claude-code", "http://two", "k2", model="m2", home=home3)
with open(p3, encoding="utf-8") as fh:
    applied3 = json.load(fh)
check("second apply rewrote the config",
      applied3["env"]["ANTHROPIC_BASE_URL"] == "http://two", applied3["env"])
A.restore(acct3, "claude-code", home=home3)
with open(p3, "rb") as fh:
    check("restore returned the first pre-integration bytes",
          fh.read() == orig3)

print()
print("[17] restore without a recorded integration raises")

home4 = tempfile.mkdtemp(prefix="wb-agents-home4-")
acct4 = tempfile.mkdtemp(prefix="wb-agents-acct4-")
try:
    A.restore(acct4, "claude-code", home=home4)
    raised17 = ""
except A.AgentConfigError as exc:
    raised17 = str(exc)
check("restore raises AgentConfigError for an unconfigured client",
      "no recorded integration" in raised17, repr(raised17))

print()
print("[18] unknown client id and bad base_url raise AgentConfigError")

try:
    A.integrate(acct4, "not-a-client", "http://x", "k", home=home4)
    raised18a = ""
except A.AgentConfigError as exc:
    raised18a = str(exc)
check("unknown client id raises", "unknown client" in raised18a, repr(raised18a))
try:
    A.integrate(acct4, "claude-code", "ftp://x", "k", home=home4)
    raised18b = ""
except A.AgentConfigError as exc:
    raised18b = str(exc)
check("non-http base_url raises", "http://" in raised18b, repr(raised18b))

print()
print("[19] every client's apply + restore round trip in one temporary home")

home5 = tempfile.mkdtemp(prefix="wb-agents-home5-")
acct5 = tempfile.mkdtemp(prefix="wb-agents-acct5-")
base_url = "http://127.0.0.1:9000"
models = [{"id": "m-big", "context_window": 128000, "max_output": 8192},
          {"id": "m-small"}]
all_ok = True
notes = {}
for cid in sorted(A.CLIENTS):
    try:
        paths = A.CLIENTS[cid]["files"](home5)
        res = A.integrate(acct5, cid, base_url, "key-" + cid,
                          model="m-big", models=models, home=home5)
        missing = [p for p in paths.values() if not os.path.exists(p)]
        all_ok = all_ok and not missing and res["client"] == cid
        notes[cid] = "missing=%s" % missing if missing else "ok"
        A.restore(acct5, cid, home=home5)
        leftovers = [p for p in paths.values() if os.path.exists(p)]
        all_ok = all_ok and not leftovers
        if leftovers:
            notes[cid] = "leftovers=%s" % leftovers
    except Exception as exc:  # noqa: BLE001 - report every client
        all_ok = False
        notes[cid] = "raised: %r" % exc
check("all six clients applied and restored cleanly", all_ok, notes)
check("state is empty after restoring everything",
      A._load_state(acct5) == {}, A._load_state(acct5))

print()
print("[20] written client configs are shaped correctly")

home6 = tempfile.mkdtemp(prefix="wb-agents-home6-")
acct6 = tempfile.mkdtemp(prefix="wb-agents-acct6-")
A.integrate(acct6, "codex", "http://gw:1", "k-codex", model="m-big",
            models=models, home=home6)
codex_cfg = os.path.join(home6, ".codex", "config.toml")
with open(codex_cfg, encoding="utf-8") as fh:
    codex_text = fh.read()
check("codex config selects the wb-proxy provider",
      'model_provider = "wb-proxy"' in codex_text, codex_text)
check("codex provider table written",
      "[model_providers.wb-proxy]" in codex_text
      and 'base_url = "http://gw:1/v1"' in codex_text, codex_text)
with open(os.path.join(home6, ".codex", "auth.json"), encoding="utf-8") as fh:
    codex_auth = json.load(fh)
check("codex auth.json holds the key",
      codex_auth.get("OPENAI_API_KEY") == "k-codex", codex_auth)

A.integrate(acct6, "dsh", "http://gw:1", "k-dsh", model="m-big",
            models=models, home=home6)
dsh_settings = os.path.join(home6, ".dsh", "settings.yaml")
with open(dsh_settings, encoding="utf-8") as fh:
    dsh_text = fh.read()
check("dsh settings.yaml has the wb-proxy provider under llm-pi-ai.providers",
      "llm-pi-ai:" in dsh_text and "providers:" in dsh_text
      and "wb-proxy:" in dsh_text, dsh_text)
check("dsh baseURL ends with /v1", "http://gw:1/v1" in dsh_text, dsh_text)
check("dsh agent-default-model written",
      'agent-default-model: {"provider": "wb-proxy", "model": "m-big"}'
      in dsh_text, dsh_text)
with open(os.path.join(home6, ".dsh", ".credentials.yaml"),
          encoding="utf-8") as fh:
    cred_text = fh.read()
check("dsh credentials refs written",
      cred_text == "refs:\n  WB_PROXY_API_KEY: k-dsh\n", repr(cred_text))

A.integrate(acct6, "opencode", "http://gw:1", "k-oc", model="m-big",
            models=models, home=home6)
oc_path = os.path.join(home6, ".config", "opencode", "opencode.json")
with open(oc_path, encoding="utf-8") as fh:
    oc = json.load(fh)
check("opencode provider registered",
      oc["provider"]["wb-proxy"]["options"]["baseURL"] == "http://gw:1/v1",
      oc)
check("opencode default model written", oc.get("model") == "wb-proxy/m-big", oc)
check("opencode models map written",
      sorted(oc["provider"]["wb-proxy"]["models"]) == ["m-big", "m-small"], oc)
check("opencode model entry carries context/output limits",
      oc["provider"]["wb-proxy"]["models"]["m-big"].get("limit")
      == {"context": 128000, "output": 8192},
      oc["provider"]["wb-proxy"]["models"])
check("opencode model without specs stays an empty object",
      oc["provider"]["wb-proxy"]["models"]["m-small"] == {},
      oc["provider"]["wb-proxy"]["models"])

A.integrate(acct6, "crush", "http://gw:1", "k-cr", model="m-big",
            models=models, home=home6)
crush_path = os.path.join(home6, ".config", "crush", "crush.json")
with open(crush_path, encoding="utf-8") as fh:
    cr = json.load(fh)
check("crush provider written with base_url and key",
      cr["providers"]["wb-proxy"]["base_url"] == "http://gw:1/v1"
      and cr["providers"]["wb-proxy"]["api_key"] == "k-cr", cr)
check("crush model entries carry context_window",
      cr["providers"]["wb-proxy"]["models"][0]["context_window"] == 128000,
      cr["providers"]["wb-proxy"]["models"])

A.integrate(acct6, "zcode", "http://gw:1", "k-zc", model="m-big",
            models=models, home=home6)
zc_path = os.path.join(home6, ".zcode", "v2", "provider_config.json")
with open(zc_path, encoding="utf-8") as fh:
    zc = json.load(fh)
zc_rules = zc["config"]["providerConfigRules"]["providerRules"]
check("zcode rule carries the gateway URL and the key",
      zc_rules[0]["providerId"] == "wb-proxy"
      and zc_rules[0]["config"]["api"]["baseUrl"] == "http://gw:1/v1"
      and zc_rules[0]["config"]["access"]["apiKey"] == "k-zc", zc_rules)
check("zcode model ids carried into personalModelIds/modelOrder",
      zc_rules[0]["config"]["personalModelIds"] == ["m-big", "m-small"]
      and zc_rules[0]["config"]["modelOrder"] == ["m-big", "m-small"],
      zc_rules[0])
check("zcode providerOrder lists the provider",
      zc["config"]["providerOrder"] == ["wb-proxy"], zc["config"])
check("zcode schemaVersion kept", zc.get("schemaVersion") == 1, zc)

for path in (codex_cfg, os.path.join(home6, ".codex", "auth.json"),
             dsh_settings, os.path.join(home6, ".dsh", ".credentials.yaml"),
             oc_path, crush_path, zc_path):
    pass

print()
print("[20b] zcode: other providers, their order and model rules survive re-apply")

home15 = tempfile.mkdtemp(prefix="wb-agents-home15-")
acct15 = tempfile.mkdtemp(prefix="wb-agents-acct15-")
zc_dir = os.path.join(home15, ".zcode", "v2")
os.makedirs(zc_dir)
zc_other_rule = {
    "providerId": "other-provider",
    "providerName": "Other",
    "config": {"group": "standard-personal",
               "access": {"type": "api-key", "apiKey": "k-other"},
               "api": {"type": "anthropic-messages",
                       "baseUrl": "https://other.example/v1"}},
}
zc_original = {
    "schemaVersion": 1,
    "config": {
        "providerOrder": ["other-provider"],
        "providerConfigRules": {"providerRules": [zc_other_rule]},
        "modelConfigRules": {"keep": {"me": True}},
    },
}
zc_original_bytes = json.dumps(zc_original).encode("utf-8")
with open(os.path.join(zc_dir, "provider_config.json"), "wb") as fh:
    fh.write(zc_original_bytes)

A.integrate(acct15, "zcode", "http://gw:1", "k-first", model="m-big",
            models=models, home=home15)
A.integrate(acct15, "zcode", "http://gw:2", "k-second", model="m-small",
            models=[{"id": "m-small"}], home=home15)
with open(os.path.join(zc_dir, "provider_config.json"), encoding="utf-8") as fh:
    zc2 = json.load(fh)
rules2 = zc2["config"]["providerConfigRules"]["providerRules"]
check("re-apply updates the one wb-proxy rule instead of stacking new ones",
      len(rules2) == 2
      and [r["providerId"] for r in rules2] == ["other-provider", "wb-proxy"],
      rules2)
check("re-apply refreshes key, base URL and model list",
      rules2[1]["config"]["access"]["apiKey"] == "k-second"
      and rules2[1]["config"]["api"]["baseUrl"] == "http://gw:2/v1"
      and rules2[1]["config"]["personalModelIds"] == ["m-small"], rules2[1])
check("the other provider rule survives verbatim",
      rules2[0] == zc_other_rule, rules2[0])
check("providerOrder appends ours without touching the existing entry",
      zc2["config"]["providerOrder"] == ["other-provider", "wb-proxy"],
      zc2["config"])
check("unrelated config keys survive",
      zc2["config"]["modelConfigRules"] == {"keep": {"me": True}},
      zc2["config"])

A.restore(acct15, "zcode", home=home15)
with open(os.path.join(zc_dir, "provider_config.json"), "rb") as fh:
    check("restore returns the pre-integration bytes",
          fh.read() == zc_original_bytes)

print()
print("[21] oversized config file is refused")

home7 = tempfile.mkdtemp(prefix="wb-agents-home7-")
acct7 = tempfile.mkdtemp(prefix="wb-agents-acct7-")
big_path = os.path.join(home7, ".claude", "settings.json")
os.makedirs(os.path.dirname(big_path))
with open(big_path, "wb") as fh:
    fh.write(b" " * (A.MAX_CONFIG_BYTES + 1))
try:
    A.integrate(acct7, "claude-code", "http://x", "k", home=home7)
    raised21 = ""
except A.AgentConfigError as exc:
    raised21 = str(exc)
check("file over %d bytes is refused" % A.MAX_CONFIG_BYTES,
      "refusing to edit" in raised21, repr(raised21[:120]))

print()
print("[22] applied.at is a human-readable ISO/date string and overview reflects it")

home8 = tempfile.mkdtemp(prefix="wb-agents-home8-")
acct8 = tempfile.mkdtemp(prefix="wb-agents-acct8-")
res22 = A.integrate(acct8, "claude-code", "http://127.0.0.1:8788/v1", "test-key",
                    model="m1", home=home8)
applied_at = res22.get("applied_at")
check("applied_at is a string", isinstance(applied_at, str) and len(applied_at) >= 19, applied_at)
ov22 = A.overview(acct8, home=home8)
claude_ov = ov22.get("claude-code") or {}
check("overview applied.at is a string",
      isinstance((claude_ov.get("applied") or {}).get("at"), str),
      claude_ov.get("applied"))

print()
print("[23] transactional rollback on write failure leaves no orphan state")

home9 = tempfile.mkdtemp(prefix="wb-agents-home9-")
acct9 = tempfile.mkdtemp(prefix="wb-agents-acct9-")
# For DSH, if credentials path is unwritable, settings.yaml must not be left behind
# and no integration state must be saved.
dsh_creds = os.path.join(home9, ".dsh", ".credentials.yaml")
os.makedirs(os.path.dirname(dsh_creds), exist_ok=True)
# Make a directory where .credentials.yaml should be, causing write to fail
os.makedirs(dsh_creds, exist_ok=True)
try:
    A.integrate(acct9, "dsh", "http://127.0.0.1:8788/v1", "key", home=home9)
    threw23 = False
except A.AgentConfigError:
    threw23 = True
check("integrate raised on partial write failure", threw23)
st23 = A._load_state(acct9)
check("no state record was saved", "dsh" not in st23, st23)
dsh_settings = os.path.join(home9, ".dsh", "settings.yaml")
check("first file was rolled back / deleted", not os.path.exists(dsh_settings))

print()
print("[24] created configs are removed together with the directories we made")

home11 = tempfile.mkdtemp(prefix="wb-agents-home11-")
acct11 = tempfile.mkdtemp(prefix="wb-agents-acct11-")
A.integrate(acct11, "crush", "http://127.0.0.1:8788/v1", "key", home=home11)
crush_dir = os.path.join(home11, ".config", "crush")
crush_cfg = os.path.join(crush_dir, "crush.json")
check("apply created the client config", os.path.exists(crush_cfg))
check("apply created the parent directories", os.path.isdir(crush_dir))
A.restore(acct11, "crush", home=home11)
check("restore deleted the created file", not os.path.exists(crush_cfg))
check("restore pruned the empty directories it created", not os.path.exists(crush_dir))
check("the injected home itself is never removed", os.path.isdir(home11))

print()
print("[25] an empty leftover directory never makes a client read as configured")

home12 = tempfile.mkdtemp(prefix="wb-agents-home12-")
acct12 = tempfile.mkdtemp(prefix="wb-agents-acct12-")
os.makedirs(os.path.join(home12, ".config", "crush"), exist_ok=True)
ov12 = A.overview(acct12, home=home12)
crush_ov = ov12.get("crush") or {}
check("a config-less client is not reported as configured",
      crush_ov.get("configured") is False, crush_ov.get("configured"))
check("a config-less client is not reported as applied",
      crush_ov.get("applied") is None, crush_ov.get("applied"))

print()
print("[26] wb_proxy handlers: client_id compatibility & models fallback")

import wb_proxy

home10 = tempfile.mkdtemp(prefix="wb-agents-home10-")
acct10 = tempfile.mkdtemp(prefix="wb-agents-acct10-")

class MockHandler:
    def __init__(self, accounts_dir, home_dir):
        self.accounts_dir = accounts_dir
        self.home_dir = home_dir
        self.response = None

    def _agents_resolve_key(self, payload, warnings):
        return "mock-key-123"

    def _agents_models(self):
        return [{"id": "model-fallback-1", "context_window": 128000},
                {"id": "model-fallback-2", "context_window": 32000}]

    def _json(self, code, obj):
        self.response = (code, obj)
        return (code, obj)

    def _error(self, code, msg, typ=""):
        self.response = (code, {"error": msg, "type": typ})
        return (code, {"error": msg})

# Patch integrate and restore targets to use our isolated home10
orig_integrate = A.integrate
orig_restore = A.restore
A.integrate = lambda acct, cid, base_url, key, model=None, models=None, home=None: \
    orig_integrate(acct10, cid, base_url, key, model=model, models=models, home=home10)
A.restore = lambda acct, cid, home=None: \
    orig_restore(acct10, cid, home=home10)

try:
    handler = MockHandler(acct10, home10)
    # Test 1: Frontend payload format (sends client_id, omits models)
    apply_payload_frontend = {
        "client_id": "opencode",
        "base_url": "http://127.0.0.1:8788/v1",
        "key_id": "",
        "model": "model-fallback-1",
    }
    code, res = wb_proxy.Handler._handle_agents_apply(handler, apply_payload_frontend)
    check("apply with client_id returns HTTP 200", code == 200, (code, res))
    # Verify OpenCode received the fallback models catalog
    opencode_cfg = os.path.join(home10, ".config", "opencode", "opencode.json")
    with open(opencode_cfg, "r", encoding="utf-8") as fh:
        cfg_data = json.load(fh)
    wb_prov = (cfg_data.get("provider") or {}).get("wb-proxy") or {}
    check("opencode models populated from catalog fallback",
          "model-fallback-1" in (wb_prov.get("models") or {}), wb_prov)

    # Test 2: Frontend restore format (sends client_id)
    restore_payload_frontend = {"client_id": "opencode"}
    code_r, res_r = wb_proxy.Handler._handle_agents_restore(handler, restore_payload_frontend)
    check("restore with client_id returns HTTP 200", code_r == 200, (code_r, res_r))
    check("restore cleaned up the opencode config file", not os.path.exists(opencode_cfg))

    # Test 3: Backend payload format (sends client) also returns 200
    apply_payload_backend = {
        "client": "claude-code",
        "base_url": "http://127.0.0.1:8788",
        "key_id": "",
        "model": "claude-test",
    }
    code_b, res_b = wb_proxy.Handler._handle_agents_apply(handler, apply_payload_backend)
    check("apply with client returns HTTP 200", code_b == 200, (code_b, res_b))
finally:
    A.integrate = orig_integrate
    A.restore = orig_restore

print()
print("[27] rollback restores a pre-existing file from its backup (NameError regression)")

# 覆盖回滚分支的两半：文件原本存在（先备份 → 失败时从备份回滚）与原本不
# 存在（失败时删除）。失败必须发生在 **Phase 2 写入** 里——占位成目录会让
# _read_text 先失败、根本走不到写入；这里改用「.credentials.yaml 是可读的
# 普通文件，但它的 .tmp 路径被目录占位」，让第二个文件的写入才失败。
# 修复前回滚分支里有一个未定义的 backup_dir(...)，NameError 被 except 吞掉：
# settings.yaml 停在半配置状态、state 也没写——面板显示「未配置」，restore()
# 又因为查不到记录而拒绝执行，用户两头都回不去。
home13 = tempfile.mkdtemp(prefix="wb-agents-home13-")
acct13 = tempfile.mkdtemp(prefix="wb-agents-acct13-")
dsh_settings13 = os.path.join(home13, ".dsh", "settings.yaml")
os.makedirs(os.path.dirname(dsh_settings13), exist_ok=True)
orig13 = b"agent-default-model:\n  provider: other\n  model: keep-me\n"
with open(dsh_settings13, "wb") as fh:
    fh.write(orig13)
dsh_creds13 = os.path.join(home13, ".dsh", ".credentials.yaml")
with open(dsh_creds13, "wb") as fh:
    fh.write(b"")                        # 可读（构建阶段能通过）
os.makedirs(dsh_creds13 + ".tmp", exist_ok=True)   # 写入阶段必失败
raised13 = ""
try:
    A.integrate(acct13, "dsh", "http://127.0.0.1:8788/v1", "key", home=home13)
except A.AgentConfigError as exc:
    raised13 = str(exc)
check("integrate raised from the write phase (rollback path was reached)",
      "failed to write dsh files" in raised13, repr(raised13))
with open(dsh_settings13, "rb") as fh:
    after13 = fh.read()
check("pre-existing settings.yaml was rolled back byte-exact",
      after13 == orig13, (after13, orig13))
check("no state record was saved (the panel must not read as half-configured)",
      "dsh" not in A._load_state(acct13), A._load_state(acct13))
ov13 = A.overview(acct13, home=home13)
check("overview reports dsh as unconfigured", ov13["dsh"]["configured"] is False,
      ov13["dsh"])
refused13 = ""
try:
    A.restore(acct13, "dsh", home=home13)
except A.AgentConfigError as exc:
    refused13 = str(exc)
check("restore still refuses (nothing was recorded to restore)",
      "no recorded integration" in refused13, repr(refused13))

# 另一半：同样的写失败，但 settings.yaml 原本不存在 → 回滚把它删掉
home14 = tempfile.mkdtemp(prefix="wb-agents-home14-")
acct14 = tempfile.mkdtemp(prefix="wb-agents-acct14-")
dsh_creds14 = os.path.join(home14, ".dsh", ".credentials.yaml")
os.makedirs(os.path.dirname(dsh_creds14), exist_ok=True)
with open(dsh_creds14, "wb") as fh:
    fh.write(b"")
os.makedirs(dsh_creds14 + ".tmp", exist_ok=True)
raised14 = ""
try:
    A.integrate(acct14, "dsh", "http://127.0.0.1:8788/v1", "key", home=home14)
except A.AgentConfigError as exc:
    raised14 = str(exc)
check("same write failure with no pre-existing settings.yaml also raises",
      "failed to write dsh files" in raised14, repr(raised14))
check("the created settings.yaml was removed again",
      not os.path.exists(os.path.join(home14, ".dsh", "settings.yaml")))
check("no state record for the second home either",
      "dsh" not in A._load_state(acct14), A._load_state(acct14))

print()
print("[28] 服务端 / 远程看板不提供一键配置（issue #246）")

check("loopback v4 is allowed",
      wb_proxy.agents_client_allowed(("127.0.0.1", 51234)) is True)
check("loopback v6 is allowed",
      wb_proxy.agents_client_allowed(("::1", 51234, 0, 0)) is True)
check("an IPv4-mapped loopback peer is allowed",
      wb_proxy.agents_client_allowed(("::ffff:127.0.0.1", 51234, 0, 0)) is True)
check("a LAN peer is refused",
      wb_proxy.agents_client_allowed(("192.168.1.20", 51234)) is False)
check("a docker bridge peer is refused",
      wb_proxy.agents_client_allowed(("172.17.0.1", 51234)) is False)
check("a missing peer address is refused",
      wb_proxy.agents_client_allowed(None) is False)

saved_form = wb_proxy._SERVER_DEPLOYMENT
try:
    wb_proxy._SERVER_DEPLOYMENT = True
    check("a container / OpenWrt marker wins over a loopback peer",
          wb_proxy.agents_client_allowed(("127.0.0.1", 51234)) is False)
finally:
    wb_proxy._SERVER_DEPLOYMENT = saved_form


class RemotePanel:
    """A panel opened from another machine: the answer must be enabled:false."""

    client_address = ("192.168.1.20", 40000)
    _agents_client_allowed = wb_proxy.Handler._agents_client_allowed

    def _json(self, code, obj):
        self.response = (code, obj)
        return (code, obj)


remote = RemotePanel()
code_r, body_r = wb_proxy.Handler._get_agents(remote)
check("a remote panel gets enabled:false from GET /agents",
      code_r == 200 and body_r.get("enabled") is False, body_r)
check("the remote answer carries no client list and no model list",
      "clients" not in body_r and "models" not in body_r, body_r)

proxy_src = open(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "wb_proxy.py"), encoding="utf-8").read()
check("wb_proxy no longer imports wb_agents at module level",
      not re.search(r"(?m)^import wb_agents\s*$", proxy_src))
check("wb_agents is imported lazily inside agents_module()",
      "def agents_module()" in proxy_src and "        import wb_agents" in proxy_src)

print()
print("[29] credential files land owner-only (issue #345)")

# 本模块写出的每个文件都带网关 API Key（明文内联，或由同目录凭据文件解析的
# env 引用），所以 apply 不能让它们 group/world 可读：DSH 遇到 0644 的凭据文件
# 直接拒绝启动，而且用户手动 chmod 600 之后再次配置不能被改回宽松。
home16 = tempfile.mkdtemp(prefix="wb-agents-cred-")
acct16 = tempfile.mkdtemp(prefix="wb-agents-cred-acct-")


def mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


A.integrate(acct16, "dsh", "http://127.0.0.1:8788/v1", "key", home=home16)
cred16 = os.path.join(home16, ".dsh", ".credentials.yaml")
settings16 = os.path.join(home16, ".dsh", "settings.yaml")

if os.name == "posix":
    check("a fresh DSH credentials file is 0600",
          mode_of(cred16) == 0o600, "%04o" % mode_of(cred16))
    check("its sibling settings.yaml is 0600 too",
          mode_of(settings16) == 0o600, "%04o" % mode_of(settings16))

    A.integrate(acct16, "dsh", "http://127.0.0.1:8788/v1", "key", home=home16)
    check("re-applying leaves an already 0600 credentials file alone",
          mode_of(cred16) == 0o600, "%04o" % mode_of(cred16))

    os.chmod(cred16, 0o644)
    A.integrate(acct16, "dsh", "http://127.0.0.1:8788/v1", "key", home=home16)
    check("a 0644 file left by an older build is tightened to 0600",
          mode_of(cred16) == 0o600, "%04o" % mode_of(cred16))

    os.chmod(cred16, 0o400)
    A.integrate(acct16, "dsh", "http://127.0.0.1:8788/v1", "key", home=home16)
    check("a stricter 0400 the user chose is kept, never widened",
          mode_of(cred16) == 0o400, "%04o" % mode_of(cred16))
    os.chmod(cred16, 0o600)

    A.integrate(acct16, "codex", "http://127.0.0.1:8788/v1", "key", home=home16)
    check("codex auth.json is 0600",
          mode_of(os.path.join(home16, ".codex", "auth.json")) == 0o600)
    A.integrate(acct16, "opencode", "http://127.0.0.1:8788/v1", "key",
                home=home16)
    check("opencode's config with an inline apiKey is 0600",
          mode_of(os.path.join(home16, ".config", "opencode",
                               "opencode.json")) == 0o600)

    # 备份保存的是原文件字节（同样可能含 Key），也必须 owner-only。
    crush_dir = os.path.join(home16, ".config", "crush")
    os.makedirs(crush_dir, exist_ok=True)
    with open(os.path.join(crush_dir, "crush.json"), "w",
              encoding="utf-8") as fh:
        fh.write("{}\n")
    A.integrate(acct16, "crush", "http://127.0.0.1:8788/v1", "key",
                home=home16)
    bdir16 = os.path.join(acct16, A.BACKUP_DIRNAME, "crush")
    backups16 = [os.path.join(bdir16, n) for n in os.listdir(bdir16)]
    check("the backup of the original file is 0600 as well",
          bool(backups16) and all(mode_of(p) == 0o600 for p in backups16),
          [(p, "%04o" % mode_of(p)) for p in backups16])

    A.restore(acct16, "crush", home=home16)
    check("restore writes the file back owner-only",
          mode_of(os.path.join(crush_dir, "crush.json")) == 0o600)
else:
    # Windows has no POSIX mode bits, so assert the mode we *ask for* is 0600
    # rather than open()'s 0666 & ~umask.
    seen16 = []
    real_chmod = os.chmod
    os.chmod = lambda p, m, *a, **k: seen16.append((p, m))
    try:
        A.integrate(acct16, "codex", "http://127.0.0.1:8788/v1", "key",
                    home=home16)
    finally:
        os.chmod = real_chmod
    auth16 = os.path.join(home16, ".codex", "auth.json")
    check("a fresh credentials write asks for 0600",
          (auth16 + ".tmp", 0o600) in seen16, seen16)

# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------

for d in (home, accounts, home2, acct2, home3, acct3, home4, acct4,
          home5, acct5, home6, acct6, home7, acct7, home8, acct8,
          home9, acct9, home10, acct10, home11, acct11, home12, acct12,
          home13, acct13, home14, acct14, home15, acct15, home16, acct16,
          home_paths):
    shutil.rmtree(d, ignore_errors=True)

print()
print()
print("[30] _agents_resolve_key raises AgentConfigError on non-existent key (issue #352)")
import wb_proxy as P
handler = P.Handler.__new__(P.Handler)
wb_agents = P.agents_module()
try:
    handler._agents_resolve_key({"key_id": "definitely-not-a-key"}, [])
    check("should have raised AgentConfigError", False)
except Exception as exc:
    check("raised AgentConfigError not NameError", isinstance(exc, wb_agents.AgentConfigError), type(exc))


print()
print("[31] codex one-click config no longer depends on an env var (issue #513)")
seed_dir = tempfile.mkdtemp(prefix="codex513-")
seed_cfg = os.path.join(seed_dir, "config.toml")
seed_auth = os.path.join(seed_dir, "auth.json")
with open(seed_cfg, "w", encoding="utf-8") as fh:
    fh.write('model_provider = "wb-proxy"\n\n[model_providers.wb-proxy]\nname = "WB Proxy"\n'
             'base_url = "http://old/v1"\nwire_api = "responses"\n'
             'env_key = "OPENAI_API_KEY"\nenv_key_instructions = "export it"\n')
codex_files = A._apply_codex({"config": seed_cfg, "auth": seed_auth},
                             {"base_url_v1": "http://new/v1", "api_key": "sk-inline",
                              "model": "m1"})
codex_out = codex_files[seed_cfg]
check("inline bearer token is written",
      'experimental_bearer_token = "sk-inline"' in codex_out, codex_out)
check("stale env_key and its instructions are removed",
      "env_key" not in codex_out, codex_out)
check("top-level keys survive the cleanup",
      'model_provider = "wb-proxy"' in codex_out and 'model = "m1"' in codex_out,
      codex_out)
shutil.rmtree(seed_dir, ignore_errors=True)

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
