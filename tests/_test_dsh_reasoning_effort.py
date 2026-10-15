"""DSH 思考档位与上下文窗口：一键配置写入 / 面板模型清单（无网络单元测试）。

issue #374：在 DSH 里看不到「推理等级」，也找不到改上下文长度的地方。根因是
一键配置只往 `~/.dsh/settings.yaml` 写了模型 id —— DSH 不读 `/v1/models` 里的
`reasoning_efforts`（它只读 id / name / contextWindow / maxTokens），模型条目不
声明 `reasoningEfforts` 就没有档位选择器；而 `contextWindow` 不写时客户端只能
吃自己的内置默认值。这个套件锁住修复后的契约：

  - `wb_agents.dsh_reasoning_levels()`  档位表归一化
  - `wb_agents._apply_dsh()`            写出的模型条目与 agent-default-model
  - `wb_proxy._agents_models()`         面板清单里的 `reasoning_efforts`
  - `wb_proxy.enrich_agent_models()`    只有 id 的清单按 id 补规格

    python tests/_test_dsh_reasoning_effort.py
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_agents as A
import wb_proxy as P

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


def provider_block(text):
    """settings.yaml 里 wb-proxy 那段的内联 JSON，解析成 dict。"""
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("wb-proxy: "):
            return json.loads(stripped.split(":", 1)[1].strip())
    return {}


def default_model_line(text):
    for line in text.split("\n"):
        if line.strip().startswith("agent-default-model:"):
            return line.strip()
    return ""


TMPDIRS = []


def temp_dir(prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    TMPDIRS.append(d)
    return d


# ---------------------------------------------------------------------------
print("[1] dsh_reasoning_levels 把网关宣告的档位列表变成 DSH 的映射")
# ---------------------------------------------------------------------------

check("列表 → {档位: 同名拼写}",
      A.dsh_reasoning_levels(["low", "high", "max"])
      == {"low": "low", "high": "high", "max": "max"})
check("映射原样透传（保留自定义线上拼写）",
      A.dsh_reasoning_levels({"max": "ultra"}) == {"max": "ultra"})
check("空白项被丢掉", A.dsh_reasoning_levels(["low", "", "  ", None])
      == {"low": "low"})
check("None / 字符串 / 数字都不声明档位",
      A.dsh_reasoning_levels(None) is None
      and A.dsh_reasoning_levels("") is None
      and A.dsh_reasoning_levels(42) is None)

# ---------------------------------------------------------------------------
print()
print("[2] _apply_dsh 把网关宣告的规格写进 DSH 模型条目")
# ---------------------------------------------------------------------------

home = temp_dir("wb-dsh-effort-home-")
acct = temp_dir("wb-dsh-effort-acct-")
models = [
    {"id": "m-thinky", "context_window": 1000000, "max_output": 128000,
     "reasoning_efforts": ["low", "high", "max"]},
    {"id": "m-fixed", "context_window": 256000, "max_output": 32000},
]
A.integrate(acct, "dsh", "http://gw:1", "k-dsh", model="m-thinky",
            models=models, home=home)
with open(os.path.join(home, ".dsh", "settings.yaml"), encoding="utf-8") as fh:
    text = fh.read()
block = provider_block(text)
by_id = {m["id"]: m for m in block.get("models") or []}

check("provider 带 compat（DSH 据此发 reasoning_effort）",
      block.get("compat") == {"thinkingFormat": "openai",
                              "supportsReasoningEffort": True},
      block.get("compat"))
check("有档位的模型写入 reasoningEfforts 映射",
      by_id.get("m-thinky", {}).get("reasoningEfforts")
      == {"low": "low", "high": "high", "max": "max"}, by_id.get("m-thinky"))
check("contextWindow 与 maxTokens 一并写入",
      by_id.get("m-thinky", {}).get("contextWindow") == 1000000
      and by_id.get("m-thinky", {}).get("maxTokens") == 128000,
      by_id.get("m-thinky"))
check("固定档位的模型不写 reasoningEfforts",
      "reasoningEfforts" not in by_id.get("m-fixed", {}),
      by_id.get("m-fixed"))
check("两个模型都进了配置", sorted(by_id) == ["m-fixed", "m-thinky"],
      sorted(by_id))

# ---------------------------------------------------------------------------
print()
print("[3] agent-default-model：换路由，但不丢用户自己设的档位")
# ---------------------------------------------------------------------------

home2 = temp_dir("wb-dsh-effort-home2-")
acct2 = temp_dir("wb-dsh-effort-acct2-")
os.makedirs(os.path.join(home2, ".dsh"), exist_ok=True)
with open(os.path.join(home2, ".dsh", "settings.yaml"), "w",
          encoding="utf-8") as fh:
    fh.write('agent-default-model: {"provider": "old", "model": "old",'
             ' "reasoningEffort": "max"}\n')
A.integrate(acct2, "dsh", "http://gw:1", "k", model="m-thinky",
            models=models, home=home2)
with open(os.path.join(home2, ".dsh", "settings.yaml"), encoding="utf-8") as fh:
    line2 = default_model_line(fh.read())
check("路由换成网关的 provider / model",
      '"provider": "wb-proxy"' in line2 and '"model": "m-thinky"' in line2,
      line2)
check("用户设的 reasoningEffort 被保留", '"reasoningEffort": "max"' in line2,
      line2)
check("块状写法退回裸路由（不猜）",
      A.dsh_default_model("agent-default-model:\n  provider: x\n",
                          "wb-proxy", "m")
      == {"provider": "wb-proxy", "model": "m"})
check("空内联映射同样退回裸路由",
      A.dsh_default_model("agent-default-model: {}", "wb-proxy", "m")
      == {"provider": "wb-proxy", "model": "m"})

# ---------------------------------------------------------------------------
print()
print("[4] 面板模型清单宣告 reasoning_efforts")
# ---------------------------------------------------------------------------

real_fetch = P.fetch_models


def fake_fetch(realm=None):
    return [
        ("m-thinky", {"maxInputTokens": 100000, "maxOutputTokens": 2000,
                      "reasoning": {"supportedEfforts": ["low", "high"]}}),
        ("m-fixed", {"maxInputTokens": 50000, "maxOutputTokens": 1000,
                     "reasoning": {"effort": "medium"}}),
        # 目录什么都没给：context 必须回落到内建知识表，而不是目录里的默认档
        ("glm-5.3", {}),
    ]


P.fetch_models = fake_fetch
try:
    listed = P.Handler._agents_models(None)
finally:
    P.fetch_models = real_fetch
by_id2 = {m["id"]: m for m in listed}
check("有 supportedEfforts 的模型宣告档位列表",
      by_id2.get("m-thinky", {}).get("reasoning_efforts") == ["low", "high"],
      by_id2.get("m-thinky"))
check("固定档位的模型不宣告档位",
      "reasoning_efforts" not in by_id2.get("m-fixed", {}),
      by_id2.get("m-fixed"))
check("context_window 走与 /v1/models 同一条查找链（知识表兜底）",
      by_id2.get("glm-5.3", {}).get("context_window") == 1000000,
      by_id2.get("glm-5.3"))

# ---------------------------------------------------------------------------
print()
print("[5] enrich_agent_models：只有 id 的清单按 id 补规格")
# ---------------------------------------------------------------------------

specs = {"m-thinky": {"id": "m-thinky", "context_window": 1000000,
                      "max_output": 128000,
                      "reasoning_efforts": ["low", "high", "max"]}}
merged = P.enrich_agent_models([{"id": "m-thinky"}], specs)
check("只有 id 的条目被补上规格",
      merged[0].get("context_window") == 1000000
      and merged[0].get("reasoning_efforts") == ["low", "high", "max"], merged[0])
explicit = P.enrich_agent_models([{"id": "m-thinky", "context_window": 5000}],
                                 specs)
check("条目里显式带来的字段优先", explicit[0]["context_window"] == 5000,
      explicit[0])
blank = P.enrich_agent_models([{"id": "m-thinky", "context_window": None}],
                              specs)
check("显式空值不算覆盖", blank[0]["context_window"] == 1000000, blank[0])
check("目录里没有的 id 原样保留",
      P.enrich_agent_models([{"id": "m-unknown"}], specs)
      == [{"id": "m-unknown"}])
check("空 specs 原样返回",
      P.enrich_agent_models([{"id": "x"}], {}) == [{"id": "x"}])
check("空清单安全", P.enrich_agent_models(None, specs) == [])

# ---------------------------------------------------------------------------
print()
print("[6] 端到端：看板只送 id → 写出的配置里出现档位")
# ---------------------------------------------------------------------------

home3 = temp_dir("wb-dsh-effort-home3-")
acct3 = temp_dir("wb-dsh-effort-acct3-")
panel_models = P.enrich_agent_models(
    [{"id": "m-thinky"}, {"id": "m-fixed"}],
    {"m-thinky": {"id": "m-thinky", "context_window": 1000000,
                  "max_output": 128000,
                  "reasoning_efforts": ["low", "high", "max"]},
     "m-fixed": {"id": "m-fixed", "context_window": 256000,
                 "max_output": 32000}})
A.integrate(acct3, "dsh", "http://gw:1", "k", model="m-thinky",
            models=panel_models, home=home3)
with open(os.path.join(home3, ".dsh", "settings.yaml"), encoding="utf-8") as fh:
    block3 = provider_block(fh.read())
by_id3 = {m["id"]: m for m in block3.get("models") or []}
check("看板送 id 也能写出档位表",
      by_id3.get("m-thinky", {}).get("reasoningEfforts")
      == {"low": "low", "high": "high", "max": "max"}, by_id3.get("m-thinky"))
check("没有档位的模型照样写入窗口与输出上限",
      by_id3.get("m-fixed", {}).get("contextWindow") == 256000
      and by_id3.get("m-fixed", {}).get("maxTokens") == 32000,
      by_id3.get("m-fixed"))

for d in TMPDIRS:
    shutil.rmtree(d, ignore_errors=True)

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
