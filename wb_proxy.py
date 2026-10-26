#!/usr/bin/env python3
"""WorkBuddy (workbuddy.ai) -> OpenAI-compatible reverse proxy.
Reuses the credentials the WorkBuddy desktop app already stored on this machine
(%%LOCALAPPDATA%%\\CodeBuddyExtension\\Data\\Public\\auth\\*.info), so no separate
login is needed. Exposes:
    GET  /v1/models
    GET  /v1/balance-summary
    POST /v1/chat/completions     (stream=true and stream=false)
    GET  /health
Only the Python standard library is required.
    python3 wb_proxy.py                    # bind 127.0.0.1:8788
    python3 wb_proxy.py --port 9000
    python3 wb_proxy.py --api-key sk-local # require a bearer token
Launchers: start-wb-proxy.bat / start-wb-proxy-lan.bat on Windows,
start-wb-proxy.command (or ./start-wb-proxy.sh) on macOS/Linux.
"""
import argparse
import calendar
import errno
import hashlib
import http.client
import io
import ipaddress
from collections import deque
import re
import atexit
import json
import os
import queue
import random
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import wb_accounts
import wb_activity
import wb_catalog
import wb_egress
import wb_ipintel
import wb_logstore
import wb_pricing
import wb_settings
import wb_tiers
import wb_webprompts
import wb_upstream_pool
import wb_webtools
import wb_identity
import wb_prompt
import wb_modelsdev
import wb_updates
from wb_wire import (_best_cached_tokens, _new_id, estimate_tokens, iter_sse_lines,
                     normalize_usage_cache_aliases, strip_data_prefix)
from wb_transforms import (LeakedToolCallStreamFilter, backfill_reasoning_content,
                           chat_finish_chunk, chat_stream_error_frame, chat_text_chunk,
                           chat_tool_call_chunks, chat_usage_chunk,
                           cleanup_orphan_tool_calls, is_truncated_arguments,
                           merge_adjacent_tool_calls, normalize_stop,
                           normalize_tool_choice, normalize_tools,
                           repack_tool_result_blocks, repair_leaked_tool_calls,
                           responses_stream_error_frame, sanitize_messages,
                           translate_max_completion_tokens)
from wb_translate import (_anthropic_estimate_chat_tokens, _encode_compaction_summary,
                          _has_compaction_trigger, _responses_usage,
                          additional_tools_from_input, anthropic_error_obj,
                          anthropic_sse_frame, chat_to_messages, chat_to_response,
                          custom_tool_names, follow_up_with_tool_results,
                          internal_calls_from_chat, local_ip_addresses,
                          messages_to_chat, reasoning_only_response, responses_to_chat,
                          stream_messages_events, stream_responses_events, sum_usage,
                          web_tools_active)
from wb_usage import (_save_account_caps, apply_daily_guards, compute_usage_analytics,
                      note_limit_event, perf_stats, perf_stats_etag, range_query,
                      recent_usage, record_error, record_upstream_usage,
                      remaining_priority_enabled, remaining_schedule_weights,
                      remaining_usage, remaining_usage_etag,
                      seconds_until_local_midnight, usage_analytics_etag,
                      usage_by_account, usage_by_account_etag, usage_snapshot,
                      usage_snapshot_etag, usage_timeseries, usage_timeseries_etag)
from wb_routes_settings import SettingsRoutes
from wb_routes_accounts import AccountRoutes
from wb_routes_agents import AgentRoutes
from wb_routes_ops import OpsRoutes
from wb_models import (VIRTUAL_ALIAS_MODELS, _models_cache, catalog_meta,
                       curated_live_sources,
                       fetch_models, model_entry,
                       models_available_for,
                       refresh_catalog, start_catalog_daily)

# wb_runtime.gateway() looks for this to tell the running gateway (usually
# __main__) from any other __main__; see wb_runtime for why that matters.
__wb_gateway__ = True

# 单请求体上限。默认值从 50MB 收紧到 16MB，是「内存护栏」：body 先整体读成 bytes、
# 再 decode 成 str、再 json.loads 成对象，实测 50MB body 的 decode+parse 峰值约
# 109MB、单请求峰值约 160MB；而读 body 发生在 chat 信号量 acquire 之前、线程数又
# 没有上限，路由器（可用内存约 480MB）上几个并发大 body 就能把机器打穿。
# 权衡：另一条路是把 body 读取也纳入 chat 信号量，但那会改动 401/404/413 早退与
# 503 的先后语义、并让慢客户端占着 slot 拖住信号量；只收紧默认值保守得多——16MB
# 对正常客户端绰绰有余（Codex 1M token 上下文的 JSON 也只有几 MB），确需更大可
# 显式设 WB_MAX_PAYLOAD_BYTES，内存代价由运维者自己拍板。
MAX_PAYLOAD_BYTES = int(os.environ.get("WB_MAX_PAYLOAD_BYTES", 16 * 1024 * 1024))  # 16MB limit
# Upstream chat calls may hold a handler thread for up to 600s, and every
# request gets its own thread, so an unbounded pool lets a handful of slow
# clients pin hundreds of threads and the memory behind them. Bound the number
# of chat/responses requests in flight; dashboard and management calls are not
# affected. Excess callers wait briefly, then get a 503 instead of queueing
# forever.
#
# The ceiling is a fixed 32 by default, which has nothing to do with how many
# accounts the pool holds: a 200-account gateway and a 5-account gateway share
# it. Set WB_MAX_CONCURRENT_CHAT=auto to size it from the pool instead - one
# slot per ready account, never below 32. See resize_chat_slots().
_CHAT_SLOTS_ENV = os.environ.get("WB_MAX_CONCURRENT_CHAT", "32").strip().lower()
CHAT_SLOTS_AUTO = _CHAT_SLOTS_ENV in ("auto", "pool", "dynamic")
CHAT_SLOTS_FLOOR = 32
try:
    MAX_CONCURRENT_CHAT = CHAT_SLOTS_FLOOR if CHAT_SLOTS_AUTO else int(_CHAT_SLOTS_ENV)
except ValueError:
    MAX_CONCURRENT_CHAT = CHAT_SLOTS_FLOOR
CHAT_SLOT_WAIT_SECONDS = float(os.environ.get("WB_CHAT_SLOT_WAIT", 30))

IS_WINDOWS = os.name == "nt"

# ---- 一键配置客户端（wb_agents）的可用性判定（issue #246）----
# 这个功能写的是「网关进程所在机器」的客户端配置，所以只有在浏览器和网关
# 同一台机器上打开看板时才成立。服务端 / Docker / OpenWrt 部署下看板都是
# 远程打开的，改了也到不了用户自己的电脑，还会白白付出探测与常驻的开销。
# 因此 wb_agents 改成懒加载：只有下面这道闸门放行、路由真的命中才 import。
_AGENTS_MODULE = None
_SERVER_DEPLOYMENT = None


def agents_module():
    """按需 import wb_agents；用不到它的部署连模块都不载入。"""
    global _AGENTS_MODULE
    if _AGENTS_MODULE is None:
        import wb_agents
        _AGENTS_MODULE = wb_agents
    return _AGENTS_MODULE


def server_deployment_form():
    """进程是否明显跑在服务端形态（容器 / OpenWrt）。

    只认环境标记；「看板是不是本机打开的」由 agents_client_allowed 按请求
    来源地址判断。结果缓存——进程活着期间答案不会变。
    """
    global _SERVER_DEPLOYMENT
    if _SERVER_DEPLOYMENT is None:
        blocked = False
        try:
            if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
                blocked = True
            elif os.path.exists("/proc/1/cgroup"):
                with open("/proc/1/cgroup", encoding="utf-8", errors="replace") as fh:
                    cgroup = fh.read().lower()
                blocked = any(m in cgroup for m in ("docker", "containerd", "kubepods"))
        except Exception:
            blocked = False
        if not blocked:
            try:
                if os.path.exists("/etc/openwrt_release"):
                    blocked = True
                elif os.path.exists("/etc/os-release"):
                    with open("/etc/os-release", encoding="utf-8", errors="replace") as fh:
                        blocked = "id=openwrt" in fh.read().lower()
            except Exception:
                blocked = False
        _SERVER_DEPLOYMENT = blocked
    return _SERVER_DEPLOYMENT


def agents_client_allowed(client_address):
    """这个来源地址能不能用一键配置：只认回环。

    非回环说明看板是远程打开的（手机、另一台电脑、容器端口映射），此时
    apply 改的是容器 / 服务器里的配置目录，需求不成立，直接按不可用处理。
    """
    if server_deployment_form():
        return False
    try:
        addr = str(client_address[0])
    except Exception:
        return False
    return addr in ("127.0.0.1", "::1", "::ffff:127.0.0.1")


# 会把模型清单写进自身配置的客户端（wb_agents 里 OpenCode / DSH / Crush /
# ZCode 的 apply 都读 ctx["models"]）：空清单会生成一个没有任何模型的
# provider，所以空勾选要在入口拒绝。Claude Code / Codex 不读 models，空清单
# 对它们无害，不拦。
MODEL_EMBEDDING_CLIENTS = frozenset({"opencode", "dsh", "crush", "zcode"})


def enrich_agent_models(models, specs):
    """把只有 id 的模型条目补上网关自己的目录规格。

    看板送过来的是一串模型 id，而 DSH / Crush 这类客户端要把规格写进自己的
    配置（上下文窗口、输出上限、思考档位）才会生成对应控件——只写 id 时它们
    只能吃自己的内置默认值，档位选择器根本不会出现。`specs` 是
    {"模型 id": 规格}；条目里显式带来的字段优先（空值不算显式）。
    """
    if not specs:
        return models
    out = []
    for item in models or []:
        spec = specs.get(str((item or {}).get("id")))
        if not spec:
            out.append(item)
            continue
        merged = dict(spec)
        merged.update({k: v for k, v in item.items() if v not in (None, "")})
        out.append(merged)
    return out


def launcher_hint(port):
    """Platform-appropriate launcher command for starting on another port."""
    if IS_WINDOWS:
        return "start-wb-proxy.bat %d" % port
    return "./start-wb-proxy.sh %d" % port
def port_owner_hint(port):
    """Command that lists the process holding a local TCP port."""
    if IS_WINDOWS:
        return "netstat -ano | findstr :%d" % port
    return "lsof -nP -iTCP:%d -sTCP:LISTEN" % port
CURRENT_REALM = os.environ.get("WB_PROXY_DEFAULT_REALM", "intl")
def detect_model_realm(model_id):
    if not model_id:
        return CURRENT_REALM
    m = str(model_id).lower()
    intl_only = {
        "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
        "gpt-5.5", "gpt-5.4", "gpt-5.3-codex", "gemini-3.5-flash",
        "grok-4.7"
    }
    if m in intl_only or any(m.startswith(p) for p in ("gpt-", "gemini-")):
        return "intl"
    cn_only = {
        "deepseek-v4-pro", "minimax-m3", "minimax-m2.7", "minimax-m2.5",
        "glm-5.1", "glm-5.0-turbo", "glm-4.6v",
        "kimi-k3-1", "kimi-k2.7", "kimi-k2-thinking",
        "hy3-x", "hy4-preview-dev", "hy4-preview-x"
    }
    if m in cn_only or any(m.startswith(p) for p in ("minimax-", "deepseek-v4-pro")):
        return "cn"
    return CURRENT_REALM
# glm-5.3-flash was listed as cn-only, but the international exit serves it:
# an official intl account posting to www.workbuddy.ai gets HTTP 200, and the
# intl desktop client ships it in its own model list. Only deepseek-v4-pro
# still answers "service info not found" there.
# Models that exist on one side only. Everything else (deepseek-v4.1-flash,
# hy3, glm-5.3 ...) is served by both exits, so it must not be treated as a
# conflict.
INTL_EXCLUSIVE_PREFIXES = ("gpt-", "gemini-")
CN_EXCLUSIVE_PREFIXES = ("deepseek-v4-pro",)
INTL_EXCLUSIVE = {
    "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    "gpt-5.5", "gpt-5.4", "gpt-5.3-codex", "gemini-3.5-flash",
    "grok-4.7",
}
CN_EXCLUSIVE = {
    "deepseek-v4-pro", "glm-5.1", "glm-5v-turbo",
    "kimi-k3-1", "kimi-k2.7", "minimax-m3",
    "hy3-x", "hy4-preview-dev", "hy4-preview-x",
}
def exclusive_realm(model_id):
    """"intl"/"cn" when only that exit serves the model, else ""."""
    if not model_id:
        return ""
    m = str(model_id).lower()
    if m in INTL_EXCLUSIVE or m.startswith(INTL_EXCLUSIVE_PREFIXES):
        return "intl"
    if m in CN_EXCLUSIVE or m.startswith(CN_EXCLUSIVE_PREFIXES):
        return "cn"
    return ""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
def install_console_close_handler():
    """Release the port when the console window is closed by the user.
    Windows does not kill child processes when a console window closes, so
    the proxy (started by the .bat as a child of cmd.exe) would survive and
    keep the port bound - the next launch then wrongly reports "another
    proxy is already running".
    Closing the window raises CTRL_CLOSE_EVENT in every process attached to
    that console, which is exactly the signal we want. Registering a handler
    for it is event-driven, so unlike polling a parent pid there is no
    chance of a false positive. Harmless when started without a console.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        PHANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        CTRL_CLOSE_EVENT = 2
        CTRL_LOGOFF_EVENT = 5
        CTRL_SHUTDOWN_EVENT = 6
        def _handler(event):
            if event in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
                try:
                    sys.stdout.flush()
                except Exception:
                    pass
                try:
                    flush_log_channel(0.5)   # 已入队的行尽力落盘，卡住也不拖延退出
                except Exception:
                    pass
                os._exit(0)
            return False
        handler = PHANDLER_ROUTINE(_handler)   # keep the callback referenced
        if not ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True):
            return None
        return handler
    except Exception:
        return None

# ---------------------------------------------------------------------------
# 日志窗口
#
# 面板的眼睛按钮改为在新窗口打开运行日志页（?tab=logs）。服务端不再 spawn
# 任何进程：容器 / Linux 上没有窗口可开，Windows 上也不需要第二个 python 进程
# （#427）。启动脚本仍可单独拉起 console_tail.py，那是命令行通道，与本按钮无关。
# ---------------------------------------------------------------------------
UPSTREAM = "https://www.workbuddy.ai"
CHAT_PATH = "/v2/chat/completions"
MODELS_PATH = "/v2/enterprises/personal/models"
DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."

# ---------------------------------------------------------------------------
# Background-model redirection (desktop clients).
#
# A desktop client's model picker only governs the MAIN chat thread. Its
# built-in "model routing config" (codex-apps/protocol/src/model-routing-config.ts)
# sends background work - memory generation, ambient suggestions, ephemeral
# generation, automations - to its own hard-coded models. Those requests hit
# this gateway on the same key as normal traffic, so they keep burning paid
# credits even when a free model is selected in the UI.
#
# Rewrite them to a free target so the client features keep working without
# spending credits. Override without editing code:
#   WB_BACKGROUND_MODEL_NAMES="gpt-5.6-terra,gpt-5.6-luna,gpt-5.3-codex"
#   WB_BACKGROUND_MODEL_TARGET="deepseek-v4.1-flash"
# ---------------------------------------------------------------------------
BACKGROUND_MODEL_TARGET = os.environ.get("WB_BACKGROUND_MODEL_TARGET", "deepseek-v4.1-flash")
BACKGROUND_MODEL_NAMES = tuple(
    m.strip().lower()
    for m in os.environ.get(
        "WB_BACKGROUND_MODEL_NAMES",
        "gpt-5.6-terra,gpt-5.6-luna,gpt-5.3-codex",
    ).split(",")
    if m.strip()
)


def _apply_background_model_alias(payload):
    """Rewrite known client background-task models to a free target model.

    A model name alone cannot tell a background job from a model the user
    picked: the desktop client requests `gpt-5.6-terra` for both. Only rewrite
    a body that also carries the client's background metadata; an explicit
    picker request must reach the model it names (issue #619).
    """
    if not isinstance(payload, dict):
        return
    name = payload.get("model")
    if not isinstance(name, str):
        return
    if name.strip().lower() not in BACKGROUND_MODEL_NAMES:
        return
    if not background_request_reason(payload):
        return
    payload["model"] = BACKGROUND_MODEL_TARGET
    log("alias: background model %s -> %s" % (name, BACKGROUND_MODEL_TARGET))


def _apply_model_aliases(payload, aliases):
    """Rewrite a client's model name from the operator's alias table.

    Claude Desktop's custom endpoint only offers ``claude-*`` names, while the
    WorkBuddy upstream only knows its real ids.  The mapping belongs before any
    protocol translation so chat, Responses and Anthropic requests all see the
    resolved id.  Unknown names pass through unchanged.
    """
    if not isinstance(payload, dict) or not isinstance(aliases, dict):
        return
    name = payload.get("model")
    if not isinstance(name, str):
        return
    target = aliases.get(name.strip().lower())
    if not target or target == name:
        return
    payload["model"] = target
    log("alias: model %s -> %s" % (name, target))


# The WorkBuddy AI desktop app caches its account product config here on every
# launch. That file carries the real model catalog the app shows in its picker
# (21 models, incl. deepseek-v4.1-flash / gpt-6-astra) - the CLI-facing
# /v2/enterprises/personal/models endpoint returns a narrower list, so prefer
# the cache and fall back to the endpoint.
PRODUCT_CONFIG_CACHE = os.path.join(os.path.expanduser("~"), ".workbuddy-ai", "cache", "acc-product-config-v3.json")
NOISE_KEYS = ("extra_fields", "refusal", "reasoning_content")
class BodyTooLarge(Exception):
    """Raised when a request body exceeds the configured cap."""
    def __init__(self, length):
        super(BodyTooLarge, self).__init__(length)
        self.length = length
class BadJSON(Exception):
    """Raised when a request body is present but not a JSON object."""
# CORS is only needed by browser-based chat clients that call the OpenAI-style
# API from another origin. Management routes (accounts, settings, usage,
# scheduler, panel) serve the dashboard, which is same-origin, so they get no
# ACAO header - that keeps a stray page on the LAN from reading their replies.
CORS_PATH_PREFIXES = ("/v1", "/chat", "/completions", "/models", "/responses")
# Management paths that happen to live under /v1 must not be treated as API:
# /v1/usage reports account-level spend and is gated by the panel session.
MANAGEMENT_PATH_PREFIXES = ("/v1/usage", "/usage", "/accounts", "/settings",
                            "/tasks", "/scheduler", "/panel", "/logs")
def cors_origin_allowed(path):
    """True when the OpenAI-style API path should advertise CORS."""
    path = (path or "").split("?")[0]
    if path.startswith(MANAGEMENT_PATH_PREFIXES):
        return False
    return path.startswith(CORS_PATH_PREFIXES)
_login_lock = threading.Lock()
_chat_slots = threading.BoundedSemaphore(MAX_CONCURRENT_CHAT)
def resize_chat_slots(ready_accounts):
    """Size the chat concurrency ceiling from the pool, when asked to.

    With WB_MAX_CONCURRENT_CHAT=auto the ceiling becomes one slot per ready
    account (never below CHAT_SLOTS_FLOOR), so the gateway scales with the pool
    it actually has instead of a constant that fits neither a 5-account nor a
    200-account deployment. A fixed numeric WB_MAX_CONCURRENT_CHAT keeps the
    1.6.x behaviour and makes this a no-op.

    Only ever grows the ceiling. Requests already holding a slot keep theirs -
    replacing the semaphore cannot revoke a permit - so a shrink would let the
    in-flight count exceed the new ceiling and the surplus releases would raise
    ValueError from BoundedSemaphore. Growing is the direction that matters:
    the pool is loaded once at startup, so in practice this runs before the
    listener accepts anything.
    """
    global _chat_slots, MAX_CONCURRENT_CHAT
    if not CHAT_SLOTS_AUTO:
        return MAX_CONCURRENT_CHAT
    try:
        wanted = max(CHAT_SLOTS_FLOOR, int(ready_accounts or 0))
    except (TypeError, ValueError):
        return MAX_CONCURRENT_CHAT
    if wanted <= MAX_CONCURRENT_CHAT:
        return MAX_CONCURRENT_CHAT
    MAX_CONCURRENT_CHAT = wanted
    _chat_slots = threading.BoundedSemaphore(wanted)
    log("chat slots : %d (one per ready account; WB_MAX_CONCURRENT_CHAT=auto)"
        % wanted)
    return wanted
_login_attempts = {}  # ip -> list of timestamp
def _prune_login_attempts(now=None, window=60):
    """Drop stale per-IP entries so the dict cannot grow without bound.
    Caller must hold _login_lock.
    """
    now = now or time.time()
    for ip in list(_login_attempts.keys()):
        recent = [t for t in _login_attempts[ip] if now - t < window]
        if recent:
            _login_attempts[ip] = recent
        else:
            del _login_attempts[ip]
# --------------------------------------------------- 面板登录限流的来源判定
# 限流按「客户端 IP」分桶（每 IP 5 次失败 / 60 秒），但反代部署下 socket 对端
# 永远不是真实客户端：路由器上 nginx 把 ai.home 转给 127.0.0.1:8788，所有请求
# 的对端都是 127.0.0.1。若照旧拿对端地址当键，任意一个人连错 5 次密码，就会把
# 所有经反代的用户、连同跑在同一台机器上的缓存预热器一起锁死 60 秒——实测里
# 预热器被 429 打掉后直接打印"登录失败（http=429），跳过本轮"，缓存转冷，
# 用户点开统计页要付一次全表扫描。所以键要取「真实来源」，且只在对端可信时
# 才读代理头：对端是回环地址（本机代理）或显式列进 WB_TRUSTED_PROXIES 的代理
# 时，按 X-Real-IP（优先）或 X-Forwarded-For 的第一个地址分桶；其余对端一律
# 忽略这两个头、仍按对端地址分桶——公网客户端不能靠伪造头换个桶绕过限流。
def parse_trusted_proxies(raw):
    """解析 WB_TRUSTED_PROXIES：逗号分隔的 IP 或 CIDR（如 10.0.0.1,fd00::/8）。
    解析不了的条目直接丢弃：一个笔误不该让网关起不来。
    """
    entries = []
    for item in str(raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            entries.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return tuple(entries)
TRUSTED_PROXIES = parse_trusted_proxies(os.environ.get("WB_TRUSTED_PROXIES", ""))
def peer_is_trusted(peer_ip):
    """对端是否有资格代表它的客户端说话：回环（本机反代）或显式配置的代理。"""
    try:
        addr = ipaddress.ip_address(str(peer_ip))
    except ValueError:
        return False
    # Windows 双栈下连 127.0.0.1 的对端会显示成 ::ffff:127.0.0.1。ipaddress 在
    # Python 3.13 之前不把 IPv4-mapped 地址算作回环（.is_loopback 为 False），
    # 于是本机反代的请求会被当成不可信来源，限流桶退化成所有客户端共用一个。
    mapped = addr.ipv4_mapped if addr.version == 6 else None
    if addr.is_loopback or (mapped is not None and mapped.is_loopback):
        return True
    for net in TRUSTED_PROXIES:
        if addr.version == net.version and addr in net:
            return True
    return False
_PROXY_HEADER_IP_MAX = 64  # 最长的 IPv6 文本也到不了 46 字符，再长就是垃圾
def _proxy_header_ip(value):
    """从代理头里取一个合法 IP 的规范文本；取不到返回 None（绝不抛异常）。

    接受裸 IPv4/IPv6，也接受 nginx 变体可能写出的 host:port / [v6]:port；
    多值（含逗号）、超长、其余畸形串一律判为不可用，由调用方决定回退。
    """
    text = str(value or "").strip()
    if not text or len(text) > _PROXY_HEADER_IP_MAX or "," in text:
        return None
    if text.startswith("["):
        end = text.find("]")
        if end == -1:
            return None
        text = text[1:end]
    elif text.count(":") == 1:
        # v4:port 形式；裸 IPv6 至少两个冒号，不会被这里截断
        host, _, port = text.partition(":")
        if port.isdigit():
            text = host
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None
def login_rate_limit_key(peer_ip, headers):
    """限流键：对端可信时取代理头里的真实客户端 IP，否则取对端地址。

    X-Real-IP 优先（nginx 里放的就是真实来源），其次 X-Forwarded-For 的
    第一个（链上最左是最初的客户端）。头不可用就退回对端地址：宁可让经
    反代的客户端共用一个桶（退化为旧行为），也不能拿解析不出来的串当键。
    """
    if not peer_is_trusted(peer_ip):
        return peer_ip
    real = _proxy_header_ip(headers.get("X-Real-IP") if headers else None)
    if real:
        return real
    forwarded = (headers.get("X-Forwarded-For") if headers else None) or ""
    first = forwarded.split(",")[0]
    via_chain = _proxy_header_ip(first)
    if via_chain:
        return via_chain
    return peer_ip
# Usage accounting: every upstream response carries a usage block, and the
# proxy also records one JSONL line per request. Defaults to a folder next to
# this script; override with --usage-dir or WB_PROXY_USAGE_DIR.
USAGE_DIR = os.environ.get("WB_PROXY_USAGE_DIR") \
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage")
USAGE_LOG = os.path.join(USAGE_DIR, "usage.jsonl")
DASHBOARD_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
# The page's stylesheets and scripts (assets/css, assets/js). Served as static
# files next to dashboard.html; only these two folders and these types.
DASHBOARD_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
ASSET_TYPES = {".css": "text/css; charset=utf-8",
               ".js": "text/javascript; charset=utf-8"}
# Web-panel access control. The panel is gated by its own password (default
# "admin"), independent of the /v1 API key. Sessions live in memory only, so a
# restart forces browsers to log in again.
PANEL = wb_settings.PanelSessions()
API_KEY_FILE_SET = False
def configured_keys():
    """Panel-managed API keys, always read fresh so panel edits apply at once."""
    try:
        return wb_settings.api_keys(ACCOUNTS_DIR)
    except Exception as exc:
        log("could not read api keys: %s" % exc)
        return []
def auth_required():
    """Whether /v1 calls must present a key at all."""
    if wb_settings.auth_disabled(ACCOUNTS_DIR):
        return False
    if any(entry.get("enabled") for entry in configured_keys()):
        return True
    return bool(API_KEY)
def identify_key(supplied):
    """Return the key entry a caller used, or None when nothing matches.
    Once the panel has at least one key, those keys are the only accepted
    credentials - otherwise a launcher key left in a .bat file would silently
    keep working after the panel was locked down.
    """
    extra = () if configured_keys() else (API_KEY,)
    return wb_settings.match_api_key(ACCOUNTS_DIR, supplied, extra_keys=extra)
# ---------------------------------------------------------------------------
# API Key 的日志显示名（issue #397）
# ---------------------------------------------------------------------------
# 请求日志原本只到账号粒度（`acct=`）。多人共用一个网关时，看到异常流量分不清
# 「是哪个人发起的」——只能事后去用量页倒推。这里把 key 的 name（没填 name 就
# 回退 id）拼进日志行。
#
# 只做「id -> 显示名」的只读查表：key 本身仍由调用方显式传入（理由见
# record_usage 的说明），不引入线程局部的隐式通道。名单带短 TTL 缓存，
# 避免每条请求都读一次 settings.json。
_KEY_LABEL_TTL = 5.0
_KEY_LABEL_FALLBACK = "(切换前)"
_key_label_cache = {"at": 0.0, "map": {}}
_key_label_lock = threading.Lock()


def key_label(key_id):
    """key 的 settings id -> 日志里显示的名字；name 为空就回退 id。

    空 id（未带 key / 面板会话 / 未配置 key 的部署）返回空串 —— 调用方据此
    决定要不要拼这一段；未知 id 原样返回，日志里仍能对上一个具体的 key。
    """
    if not key_id:
        return ""
    now = time.time()
    with _key_label_lock:
        if (now - _key_label_cache["at"]) > _KEY_LABEL_TTL:
            try:
                _key_label_cache["map"] = {
                    str(entry.get("id")): (str(entry.get("name") or "").strip()
                                           or str(entry.get("id")))
                    for entry in configured_keys()
                }
            except Exception:
                _key_label_cache["map"] = {}
            _key_label_cache["at"] = now
        return _key_label_cache["map"].get(str(key_id)) or str(key_id)


LIMIT_EVENTS_FILE = os.path.join(USAGE_DIR, "limit-events.jsonl")
# 按天持久化的估算数据（每天的预算口径 + 每个组合的截断前后分段）。与事件
# 日志一样必须跟随 --usage-dir：常量在 import 时按默认目录算过，漏掉它真机上
# 会一直写 ENOENT（2026-10-10 实测 limit-events.jsonl 踩过同一个坑）。
ESTIMATE_DAILY_FILE = os.path.join(USAGE_DIR, "estimate-daily.json")
# recent 的行偏移索引（页 = 日志尾部的第 N 页）。同样必须跟随 --usage-dir：
# 常量在 import 时按默认目录算过，漏掉它换目录后索引会写到旧目录去。
RECENT_INDEX_FILE = os.path.join(USAGE_DIR, "usage-recent-index.json")


def affinity_stable_key_enabled():
    """更稳的会话粘滞键开关（默认关，关着时与上游逻辑一字不差）。"""
    return wb_settings.affinity_stable_key_enabled(ACCOUNTS_DIR) is True


POOL = None

# Pinned-account routes (/u/<ref>/v1/...): the handler strips the prefix and
# resolves the ref before dispatching. One thread serves one connection at a
# time, so resetting at the start of every request is enough to keep a
# keep-alive connection from leaking the previous request's pin.
_PINNED = threading.local()


def pinned_ref():
    return getattr(_PINNED, "ref", None)


def pinned_account():
    return getattr(_PINNED, "account", None)
SCHEDULER = None
PRICING = None
CREDITS_REFRESHER = None

# ------------------------------------------------- 全量刷积分的并发与节奏护栏
# 「一键刷新积分」与账号页的自动补快照都会打这条全扫路径：不带 uid/realm 时它对
# **整个账号池**逐个发上游计费请求。真机实测一次 = 23 个账号 = 23 次上游调用，而它
# 既没有单飞、也没有账号间间隔——池子一大，或某个账号始终拿不到快照被前端每 15 秒
# 重试一次，就会变成成串的上游轰炸（网关日志里出现过 10 秒内 9 次全量刷）。
#
# 两道护栏，口径对齐隔壁 workbuddy2api-panel 的 internal/scheduler/staggered.go：
#   1) CAS 单飞：同一时刻只允许一次全量刷，重复并发直接拒（HTTP 200 + busy=true，
#      是正常回执而不是错误）；
#   2) 账号间 800~1200ms 拟人间隔：避免几十个账号在同一秒把上游打成一串。
#
# 只有「全池」与「按区域」这类多账号目标才吃护栏；带 uid 的单账号刷新本来就只发一次
# 请求，点了就该立刻有反应，不受这两道限制。
#
# 间隔可用 WB_CREDITS_SWEEP_GAP_MS 覆盖（"min-max" 或单个数字；0 = 关掉间隔，回到
# 改造前的连发行为）。23 个账号 × 1 秒 ≈ 23 秒的等待是「拟人」的代价，调小它就等于
# 削弱对外观感上的打散效果。
_CREDITS_SWEEP_GUARD = threading.Lock()
_CREDITS_SWEEP_RUNNING = False


def _credits_sweep_gap_seconds():
    """账号间隔的 (lo, hi)，单位秒。默认 (0.8, 1.2)，可用环境变量覆盖。"""
    raw_gap = (os.environ.get("WB_CREDITS_SWEEP_GAP_MS") or "").strip()
    if raw_gap:
        try:
            if "-" in raw_gap:
                lo_s, hi_s = raw_gap.split("-", 1)
                lo, hi = float(lo_s), float(hi_s)
            else:
                lo = hi = float(raw_gap)
            if hi < lo:
                lo, hi = hi, lo
            return max(0.0, lo) / 1000.0, max(0.0, hi) / 1000.0
        except (TypeError, ValueError):
            pass          # 环境变量写坏了就回默认，不因为一个配置让功能不可用
    return 0.8, 1.2


def _credits_sweep_begin():
    """抢一次全量刷：拿到返回 True，已有一次在跑返回 False（CAS）。"""
    global _CREDITS_SWEEP_RUNNING
    with _CREDITS_SWEEP_GUARD:
        if _CREDITS_SWEEP_RUNNING:
            return False
        _CREDITS_SWEEP_RUNNING = True
        return True


def _credits_sweep_end():
    global _CREDITS_SWEEP_RUNNING
    with _CREDITS_SWEEP_GUARD:
        _CREDITS_SWEEP_RUNNING = False


def _credits_sweep(targets, paced=True):
    """串行刷一批账号的积分，账号之间按拟人间隔停一下。

    targets 里的 None（uid 查不到）跳过；单个账号取数**抛异常**也只记下来，
    不中断整轮（池里一个坏凭证不该让其余账号全部刷不到）。
    paced=False（单账号）时一步不停，保持改造前的即时反应。
    """
    lo, hi = _credits_sweep_gap_seconds() if paced else (0.0, 0.0)
    results = []
    first = True
    for account in targets:
        if account is None:
            continue
        if not first and hi > 0:
            time.sleep(lo if hi <= lo else random.uniform(lo, hi))
        first = False
        try:
            res = account.fetch_credits()
        except Exception as exc:
            # 单个账号炸掉只记下来，不中断整轮——否则池里一个坏凭证会让其余账号
            # 全部刷不到（WBAPI 的同位逻辑也是记完就 continue）。
            log("credits sweep: %s failed: %s" % (account.uid[:8], exc))
            res = {"ok": False, "error": str(exc)}
        results.append({"uid": account.uid, "ok": res.get("ok", False),
                        "credits": account.credits, "error": res.get("error", "")})
    return results

# Release discovery only - the checker never downloads or installs anything.
UPDATES = None
ACCOUNTS_DIR = os.environ.get("ACCOUNTS_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'accounts')
try:
    # Panel-set version overrides ride on top of the verified defaults; the
    # identity module only changes the version strings it sends.
    wb_identity.configure_versions(wb_settings.identity_versions(ACCOUNTS_DIR))
except Exception:
    pass
def realm_state_file():
    """Path of the persisted realm switch.

    Computed on every access rather than cached in a module constant: the
    constant was built from the default ACCOUNTS_DIR at import time, so a
    later --accounts-dir (or a Docker volume pointing somewhere else) still
    read and wrote the realm switch next to the script - the panel then
    reported an exit that did not match the configured account store.
    """
    return os.path.join(ACCOUNTS_DIR, "active_realm.json")


def load_persisted_realm():
    global CURRENT_REALM
    path = realm_state_file()
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
                r = d.get("realm")
                if r in ("intl", "cn"):
                    CURRENT_REALM = r
                    return CURRENT_REALM
        except Exception as e:
            log("could not load active realm: %s" % e)
    return CURRENT_REALM
def save_persisted_realm(realm):
    global CURRENT_REALM
    if realm in ("intl", "cn"):
        CURRENT_REALM = realm
        try:
            os.makedirs(ACCOUNTS_DIR, exist_ok=True)
            with open(realm_state_file(), "w", encoding="utf-8") as fh:
                json.dump({"realm": realm, "updated_at": time.time(), "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S")}, fh, indent=2)
            log("persisted active realm '%s' to disk" % realm)
        except Exception as exc:
            log("failed to persist active realm: %s" % exc)
    return CURRENT_REALM
API_KEY = None
SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT
def import_desktop_accounts(realm=None):
    imported = []
    for p, r in wb_accounts.desktop_credential_candidates():
        if realm and r != realm:
            continue
        try:
            account = POOL.import_desktop_credential(path=p, realm=r)
            imported.append(account)
            log("imported %s (%s) from %s" % (account.uid[:8], account.realm, os.path.basename(p)))
        except Exception as exc:
            log("skip %s: %s" % (os.path.basename(p), exc))
    return imported
def desktop_credential_scan():
    """Read-only scan of the desktop client credentials on this machine."""
    return wb_accounts.scan_desktop_credentials()
def account_views(realm=None):
    """List view of every account, including a live readiness flag."""
    if not POOL:
        return []
    return POOL.list_public(realm=realm)


def _finite_number(value):
    """True for a float that is neither NaN nor +/-inf.

    Deliberately avoids `math`: the portable runtime ships the compiled
    `math` module, and the release-asset test maps every imported stdlib
    name to a `Lib/*.py` file, which `math` does not have.
    """
    return value == value and value not in (float("inf"), float("-inf"))


def credit_balance_summary(realm=None):
    """Aggregate cached credit snapshots without account-identifying fields.

    This intentionally never calls fetch_credits(): a balance widget may poll
    this route frequently, and each upstream refresh would spend time and risk
    needless upstream traffic. Unknown snapshots stay unknown instead of being
    reported as a zero balance.
    """
    accounts = [
        account for account in (POOL.accounts if POOL else [])
        if account.enabled and (
            not realm or realm == "all" or account.realm == realm
        )
    ]
    total = 0.0
    known = 0
    updated = []
    for account in accounts:
        credits = account.credits if isinstance(account.credits, dict) else {}
        try:
            remain = float(credits.get("remain"))
        except (TypeError, ValueError):
            continue
        if not _finite_number(remain) or remain < 0:
            continue
        total += remain
        known += 1
        try:
            stamp = float(credits.get("updated_at"))
        except (TypeError, ValueError):
            continue
        if _finite_number(stamp):
            updated.append(stamp)
    return {
        "balance": round(total, 2) if known else None,
        "unit": "credit",
        "realm": realm or CURRENT_REALM,
        "accounts_enabled": len(accounts),
        "accounts_with_credit_data": known,
        "updated_at": max(updated) if updated else None,
    }

# --------------------------------------------------------- 积分获取历史（发放记录）
# 上游对每个账号返回一份积分包清单（免费套餐、每日活跃奖励的 Bonus Pack、活动包…），
# 每个包带面额、发放时间与到期时间。看板的「积分获取历史」就是这份清单的合并视图：
# 只读内存里的账号积分快照，**不发任何上游请求**（要更新的数字先点「一键刷新积分」），
# 所以它同时给出快照时刻，让面板标明数据有多旧。
GRANT_STATUS_ACTIVE = "active"        # 上游标了 in_usage：当前正从它扣减
GRANT_STATUS_AVAILABLE = "available"  # 有剩余、未过期，只是当前不参与扣减
GRANT_STATUS_USED_UP = "used_up"      # 用完了
GRANT_STATUS_EXPIRED = "expired"      # 已过期


def _parse_stamp_epoch(text):
    """'2026-10-10 00:42:45' -> epoch；认不出来返回 None。"""
    if not text:
        return None
    try:
        return time.mktime(time.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None


def _grant_status(pkg, now):
    """一个积分包现在的状态；判定顺序是「过期 > 用完 > 在扣减 > 可用」。"""
    try:
        remain = float(pkg.get("remain") or 0)
    except (TypeError, ValueError):
        remain = 0.0
    expired = bool(pkg.get("is_expired"))
    expire_at = _parse_stamp_epoch(pkg.get("expire_time"))
    if expire_at is not None and expire_at <= now:
        expired = True
    if expired:
        return GRANT_STATUS_EXPIRED
    if remain <= 0:
        return GRANT_STATUS_USED_UP
    return GRANT_STATUS_ACTIVE if pkg.get("in_usage") else GRANT_STATUS_AVAILABLE


# 发放记录与本机动作的关联窗口：动作在发放前 2 小时内算「这次动作带来的」，
# 上游记的发放时间可能比我们那条动作记录早几秒，所以向后也留一点余量。
GRANT_ACTION_BEFORE = 2 * 3600
GRANT_ACTION_AFTER = 600


def _grant_actions():
    """uid -> [{at, ts, task, ok}]（按时间升序），只含签到与每日活跃的真实尝试。

    历史文件是磁盘上的东西：读取一律先过 wb_activity.project()，多出来的键
    （别的写入方、手工编辑、被塞进来的凭证）不会跟着载荷出去；任何读取失败都
    退化成「没有关联动作」，绝不让一次查询挂掉。
    """
    index = {}
    try:
        rows = wb_activity.load()
    except Exception:
        return index
    for raw in rows or []:
        row = wb_activity.project(raw)
        task = row["task"]
        if task not in (wb_activity.TASK_CHECKIN, wb_activity.TASK_DAILY_CHAT):
            continue
        at = wb_activity._parse_ts(row["ts"])
        if at is None:
            continue
        index.setdefault(row["uid"], []).append(
            {"at": at, "ts": row["ts"], "task": task, "ok": bool(row["ok"])})
    for items in index.values():
        items.sort(key=lambda item: item["at"])
    return index


def _grant_action(index, uid, at):
    """发放时刻之前最近的一次本机签到 / 每日活跃尝试；窗口外没有就 None。"""
    if at is None:
        return None
    for item in reversed(index.get(uid) or ()):
        if item["at"] > at + GRANT_ACTION_AFTER:
            continue
        if item["at"] < at - GRANT_ACTION_BEFORE:
            break
        return {"task": item["task"], "ts": item["ts"], "ok": item["ok"],
                "delta_seconds": int(round(at - item["at"]))}
    return None

def credit_grants(now=None):
    """每个积分包一行（新的在前）＋汇总；只读快照，不发上游请求。"""
    now = time.time() if now is None else now
    actions = _grant_actions()
    rows = []
    fetched = 0.0
    for account in (POOL.accounts if POOL else []):
        credits = getattr(account, "credits", None) or {}
        try:
            fetched = max(fetched, float(credits.get("updated_at") or 0))
        except (TypeError, ValueError):
            pass
        for pkg in credits.get("packages") or []:
            if not isinstance(pkg, dict):
                continue
            def _num(key):
                try:
                    return float(pkg.get(key) or 0)
                except (TypeError, ValueError):
                    return 0.0
            expire_iso = pkg.get("expire_time") or pkg.get("cycle_end_time") or ""
            create_at = _parse_stamp_epoch(pkg.get("create_time"))
            rows.append({
                "uid": account.uid,
                "nickname": account.nickname or (account.uid or "")[:8],
                "realm": account.realm,
                "name": str(pkg.get("name") or pkg.get("package_code") or "Package"),
                "product": str(pkg.get("sub_product_name") or pkg.get("product_name") or ""),
                "grant_reason": str(pkg.get("grant_reason") or ""),
                "size": _num("size"),
                "remain": _num("remain"),
                "used": _num("used"),
                "unit": str(pkg.get("unit") or "credit"),
                "create_at": create_at,
                "create_iso": str(pkg.get("create_time") or ""),
                "expire_at": _parse_stamp_epoch(expire_iso),
                "expire_iso": str(expire_iso),
                "no_expiry": bool(pkg.get("no_expiry")),
                "days_left": pkg.get("days_left"),
                "status": _grant_status(pkg, now),
                "action": _grant_action(actions, account.uid, create_at),
            })
    # 新的在前；同一秒按账号、包名稳定排序，免得每次刷新顺序都在跳。
    rows.sort(key=lambda r: (-(r["create_at"] or 0), r["nickname"], r["name"]))
    return {
        "ok": True,
        "generated_at": now,
        "generated_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "fetched_at": fetched or None,
        "fetched_iso": (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(fetched))
                        if fetched else None),
        "rows": rows,
        "summary": {
            "count": len(rows),
            "size": round(sum(r["size"] for r in rows), 2),
            "remain": round(sum(r["remain"] for r in rows), 2),
            "used": round(sum(r["used"] for r in rows), 2),
            "accounts": len(set(r["uid"] for r in rows)),
        },
    }


# --------------------------------------------------------------- 任务状态快照
# GET /tasks 每次都要向上游要两份数据（成长任务 + 汇总），实测约 2 秒；而看板切区域、
# 切账号、切页面都会各打一次，用户在界面上就是"点了要等几秒才变"。这里按账号放一份
# 短 TTL 快照：只有这条只读路径吃缓存，任何会改变任务状态的动作（执行/旅行/签到）都
# 先清掉它，所以"刚点完执行却看到旧状态"不会发生。TTL 可用 WB_TASKS_CACHE_TTL 调，
# 设 0 即关闭缓存、回到每次直连上游。
TASKS_CACHE_TTL = float(os.environ.get("WB_TASKS_CACHE_TTL") or "20")
_tasks_cache = {}
_tasks_cache_lock = threading.Lock()

def invalidate_tasks_cache():
    """任务状态变了就清掉快照，下一次读重新向上游取。"""
    with _tasks_cache_lock:
        _tasks_cache.clear()

def growth_snapshot(account):
    """成长任务 + 汇总；TTL 内直接复用上一份快照，过期或没缓存才请求上游。

    两条查询互不依赖（汇总那边自己还要问三个接口），所以并发发出：冷启动时这块
    从"两条串起来等"变成一个来回，实测约 2 秒降到 1.5 秒以内。
    """
    if TASKS_CACHE_TTL <= 0:
        from wb_tasks import fetch_growth_tasks, fetch_growth_summary
        return _both(fetch_growth_tasks, fetch_growth_summary, account)
    now = time.time()
    with _tasks_cache_lock:
        hit = _tasks_cache.get(account.uid)
        if hit and hit[0] > now:
            return hit[1]
    from wb_tasks import fetch_growth_tasks, fetch_growth_summary
    data = _both(fetch_growth_tasks, fetch_growth_summary, account)
    with _tasks_cache_lock:
        _tasks_cache[account.uid] = (time.time() + TASKS_CACHE_TTL, data)
    return data

def _both(tasks_fn, summary_fn, account):
    """并发跑两条上游查询；任一条抛错就照旧往外抛（不写进快照）。"""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks_future = pool.submit(tasks_fn, account)
        summary_future = pool.submit(summary_fn, account)
        return tasks_future.result(), summary_future.result()


PROXY_DISCOVER_HOST = os.environ.get("WB_PROXY_DISCOVER_HOST") or "cli-proxy-mihomo"


def _proxy_port_range():
    raw = os.environ.get("WB_PROXY_DISCOVER_PORTS") or "17901-17910"
    if "-" in raw:
        lo, _, hi = raw.partition("-")
        if lo.strip().isdigit() and hi.strip().isdigit():
            return range(int(lo), int(hi) + 1)
    if raw.strip().isdigit():
        return [int(raw)]
    return range(17901, 17911)


def probe_proxy_exit(proxy_url, timeout=12):
    """Return (exit_ip, error) for one proxy URL."""
    try:
        opener = wb_accounts.opener_for_proxy(proxy_url)
        if opener is None:
            return "", "empty proxy url"
        req = urllib.request.Request("https://api.ipify.org", method="GET")
        with opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace").strip(), ""
    except Exception as exc:
        return "", str(exc)[:160]


def probe_proxy_intel(proxy_url, timeout=12):
    """Probe one proxy: its exit IP, and where and what that exit is.

    Both steps live here so the discovery list, the per-slot test and the stored
    slot all describe an exit the same way. `latency_ms` covers the proxy probe
    only - the lookup goes out directly and would otherwise inflate it.
    """
    started = time.time()
    exit_ip, error = probe_proxy_exit(proxy_url, timeout=timeout)
    latency_ms = int((time.time() - started) * 1000)
    intel = wb_ipintel.lookup(exit_ip) if exit_ip else wb_ipintel.empty()
    # Two steps, two outcomes. `ok` covers the proxy probe only: the lookup is a
    # separate third-party call that fails on its own (blocked, timeout, 429, a
    # reply without a status). Callers that already know an exit need to tell
    # "the lookup did not answer" from "the lookup answered with nothing", or a
    # blip at the geo service would erase what the last good probe learned.
    intel_ok = any(str(intel.get(field) or "").strip() for field in wb_ipintel.FIELDS)
    return {
        "ok": not error,
        "exit_ip": exit_ip,
        "latency_ms": latency_ms,
        "error": error,
        "intel_ok": intel_ok,
        "country": intel["country"],
        "country_code": intel["country_code"],
        "ip_type": intel["ip_type"],
        "isp": intel["isp"],
        "asn": intel["asn"],
    }


def discover_proxy_slots():
    """Probe the configured mihomo host/ports and report reachable exits."""
    out = []
    for port in _proxy_port_range():
        url = "http://%s:%d" % (PROXY_DISCOVER_HOST, port)
        probe = probe_proxy_intel(url)
        probe["url"] = url
        probe["reachable"] = probe["ok"]
        out.append(probe)
    return out


def slot_label(entry):
    """What to call a slot: the operator's name, else its exit, else its id."""
    name = str((entry or {}).get("name") or "").strip()
    if name:
        return name
    auto = wb_ipintel.slot_name(entry.get("country"), entry.get("ip_type"))
    return auto or str((entry or {}).get("id") or "")


def proxy_slots_view():
    """Proxy slots plus how many enabled accounts are bound to each."""
    counts = {}
    if POOL:
        for account in POOL.accounts:
            slot_id = account.proxy_slot
            if slot_id and account.enabled:
                counts[slot_id] = counts.get(slot_id, 0) + 1
    out = []
    for entry in wb_settings.proxy_slots(ACCOUNTS_DIR):
        item = dict(entry)
        item["bound"] = counts.get(entry["id"], 0)
        item["label"] = slot_label(entry)
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Prefix-based session affinity (PATCHED-BY-OPS)
# ---------------------------------------------------------------------------
# 上游 prompt cache 是【账号级】的：只有同一个账号再次看到相同前缀才会命中。
# 实测证据（wk 实例 11 个号）：8 次完全相同的前缀请求被轮询分散到 8 个账号，
# 缓存率全部为 0%；而带上会话标识固定落到同一账号时，第 2 次起缓存率即 95.2%。
#
# sub2api / DSH 等客户端并不发送 X-Conversation-Id 之类的会话标识，
# 于是 hub 走纯轮询，同一对话每一轮都换账号，缓存必然归零。
#
# 这里在缺少显式会话键时，用【对话稳定前缀】派生亲和键：
# 取消息列表的前两条（system + 首条 user），它们在整段对话生命周期内不变，
# 因此同一对话的每一轮都会落到同一账号；而不同对话的首条 user 不同，
# 依旧会分散到各账号，负载均衡不受影响。
AFFINITY_BY_PREFIX = os.environ.get("WB_AFFINITY_BY_PREFIX", "1").lower() not in (
    "0", "false", "no", "off")
AFFINITY_DEBUG = os.environ.get("WB_AFFINITY_DEBUG", "0").lower() in (
    "1", "true", "yes", "on")
# 亲和的上限：对话超过这么多条消息后不再绑定账号。
#
# 为什么需要上限：亲和把整段对话钉在同一个账号上，而对话的上下文是单调
# 增长的，于是那一个账号要反复接收越来越大的请求体。实测（wk4 实例，
# 2026-10-08，333 个请求）请求体与上游断连率的关系：
#
#     msgs <150    82 个请求   断连率  0.0%
#     msgs 150-300 59 个请求   断连率  3.4%
#     msgs 300-400 64 个请求   断连率  7.8%
#     msgs 400-500 50 个请求   断连率 10.0%
#
# 断连（TimeoutError / RemoteDisconnected）会触发重试，把一个 8.8s 的请求
# 拖到 11.6s，首字延迟随之翻倍。超过阈值后放弃亲和，让这个对话重新参与
# 轮询：代价是它丢掉前缀缓存，收益是断连和重试消失。
#
# 阈值不能设得太低，否则会波及正常长度的对话（本实例 94% 的请求缓存命中率
# 来自亲和）。设 0 表示不限制，保持 1.6.x 的原有行为。
AFFINITY_MAX_MSGS = int(os.environ.get("WB_AFFINITY_MAX_MSGS", "400") or 0)
# 超限对话改成【页内轮转】而不是撒到整个池子。
#
# 为什么：整池轮转下每个账号要等「池子大小」轮才再见到这段对话，间隔一旦
# 超过上游缓存的存活期就整段漏。线上实测（2026-10-10，37706 条请求）：超限
# 对话占 59% 的 prompt token，却贡献了 64% 的漏掉量，签名是只命中约 24k 的
# 系统提示（账号见过同一客户端的系统提示、没见过这段历史）——典型的「落在
# 没预热过的账号上」。页内轮转把「再见到」的周期从「池子大小」轮缩到 size
# 轮，页里每个账号都留着前缀；同时超大请求体仍摊在 size 个账号上，不是压回
# 一个（那正是 400 条上限要解决的断连问题）。
#
# 取值：≥2 = 页大小（账号数）；1 = 和普通对话一样钉住（等于取消上限）；
# 0 = 保持 1.6.x 的整池轮转。页的划分见 wb_accounts.AccountPool._page_slice。
AFFINITY_PAGE_SIZE = int(os.environ.get("WB_AFFINITY_PAGE_SIZE", "3") or 0)
def affinity_route(messages):
    """This conversation's (session_key, page).

    `session_key` is the stable conversation key; `page` is the (index, size)
    of the pool page a conversation past AFFINITY_MAX_MSGS rotates over, or
    None for every other case (short conversations, the page switched off, and
    conversations whose client supplied its own session key). Both come from
    the same head hash, so every turn of a conversation agrees on its key and
    its page without any server-side state.

    With ``affinity_stable_key_enabled`` the hashed head is longer: the default
    head is [system, first user], which two conversations with the same opening
    line share - they then overwrite each other's binding every turn and each
    turn lands on the other one's account, dropping the prefix cache each time.
    Adding the first assistant reply (and the user turn after it) makes the two
    keys diverge from the second turn on. The switch is off by default so an
    existing install keeps exactly the upstream behaviour and can roll back.
    """
    if not AFFINITY_BY_PREFIX:
        return None, None
    try:
        msgs = messages or []
        if not msgs:
            return None, None
        head = msgs[:2]
        if affinity_stable_key_enabled() and len(msgs) > 2:
            # 只加「首条 assistant 回复」这一条：它是两个同开场白会话第一个必然
            # 不同的地方，又只占一轮的 token，哈希开销可忽略。system 提示相同
            # 时它同样稳定（同一会话每轮都能算出同一个键）。
            head = msgs[:3]
        blob = json.dumps(head, ensure_ascii=False, sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(blob).hexdigest()[:16]
        key = "pfx-" + digest
        if AFFINITY_MAX_MSGS > 0 and len(msgs) > AFFINITY_MAX_MSGS:
            if AFFINITY_PAGE_SIZE > 1:
                page = (int(digest[:8], 16) % AFFINITY_PAGE_SIZE, AFFINITY_PAGE_SIZE)
                if AFFINITY_DEBUG:
                    log("affinity: %d msgs (> %d), page %d/%d"
                        % (len(msgs), AFFINITY_MAX_MSGS, page[0], page[1]))
                return key, page
            if AFFINITY_PAGE_SIZE == 1:
                # Pinned like any other conversation: the cap is switched off.
                return key, None
            if AFFINITY_DEBUG:
                log("affinity: skip %d msgs (> %d), letting the pool rotate"
                    % (len(msgs), AFFINITY_MAX_MSGS))
            return None, None
        return key, None
    except Exception:
        return None, None
def derive_affinity_key(messages):
    """Conversation key only; see affinity_route for the pool page."""
    return affinity_route(messages)[0]
def prompt_fingerprint(messages):
    """Privacy-safe fingerprint of the outgoing prompt.
    Cache hits need a byte-identical prefix, so these hashes answer "is my
    prefix stable / is my conversation continuous?" without storing any text.
    """
    try:
        def h(obj):
            blob = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
            return hashlib.sha256(blob).hexdigest()[:12]
        msgs = messages or []
        out = {"msgs_sha": h(msgs), "n_msgs": len(msgs)}
        if msgs:
            out["system_sha"] = h(msgs[0]) if msgs[0].get("role") == "system" else ""
            out["prefix_sha"] = h(msgs[:-1]) if len(msgs) > 1 else ""
        return out
    except Exception:
        return {}

LOG_BUFFER = deque(maxlen=2000)
_LOG_LOCK = threading.Lock()
_LOG_COUNTER = 0

# ------------------------------------------------- 账号手机号脱敏（隐私出口）
# 账号池里多数账号把手机号当昵称用，而面板要把它显示出来。真值只保留在内存对象
# 与 accounts/*.json 磁盘文件里；**所有出口**（面板 JSON、导出件、看板日志、
# 控制台 stderr）一律换成 138****1234。
#
# 为什么值级正则带 ^$ 锚点、文本级正则带 (?!\d)/(?<!\d) 边界：面板载荷里有 26 位
# 订单号（deal_name）、10 位时间戳、11 位以上的 token 计数，裸匹配 1[3-9]\d{9}
# 会把它们切一段出来当成手机号改掉——那是数据损坏，不是脱敏。
#
# 首三位 1[3-9]\d 仍要求连续（真实号码不会是 1-38-…）；只在 3-4-4 分组之间容忍
# 常见书写分隔符（- . · 空格 制表符）。刻意不含换行，避免跨行把两段数字拼成号码。
# 分隔符容忍不削弱「大数字不被切段」这条保证：26 位订单号是连续数字，10 位时间戳
# 根本凑不满 11 位，二者的数字边界 (?!\d)/(?<!\d) 依旧成立。
_PHONE_EXACT = re.compile(r"^1[3-9]\d{9}$")
_PHONE_SEP = r"[ \t\-.·]?"
# 文本级还要挡住「标识符里恰好有一段 3-4-4 数字」：#581 的国内号 uid
# `6ef20bf4-76fa-4085-b179-97517476cc1e` 里的 `179-97517476` 满足 3-4-4 形状，
# 掩码后 uid 变成 `…b179****7476cc1e`，面板拿这个假 uid 回传，
# `AccountPool.get()` 永远返回 None → 404 no such account → 前端「操作失败」。
# 左右都要求不是标识符字符（字母 / 数字 / 下划线），散文里的号码不受影响
# （中文、空格、标点都是合法边界）。
_PHONE_IN_TEXT = re.compile(
    r"(?<![0-9A-Za-z_])(1[3-9]\d)" + _PHONE_SEP + r"(\d{4})" + _PHONE_SEP
    + r"(\d{4})(?![0-9A-Za-z_])"
)
# 键名本身承载标识符：这些值里的数字串不是手机号，一律原样返回。
# 值级正则（_mask_phone）只看 nickname，不受影响。
_IDENTITY_KEYS = frozenset((
    "uid", "account", "accountId", "id", "file", "path", "machineId",
    "sessionId", "key", "token", "accessToken", "refreshToken",
))


def _mask_phone(value):
    """整个值就是手机号时掩码成 138****1234；其余（含非字符串）原样返回。

    幂等：掩码结果不再是 11 位纯数字，重复调用不会二次掩码。
    """
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not _PHONE_EXACT.match(s):
        return value
    return "%s****%s" % (s[:3], s[-4:])


def _mask_text(text):
    """把一段文本中出现的手机号就地掩码（日志、服务端先拼好的摘要串）。

    容忍 3-4-4 分组间的常见分隔符（138-1234-5678 / 138 1234 5678 / 138.1234.5678 /
    138·1234·5678），输出统一为 138****5678，故幂等。
    """
    if not isinstance(text, str) or not _PHONE_IN_TEXT.search(text):
        return text
    return _PHONE_IN_TEXT.sub(lambda m: m.group(1) + "****" + m.group(3), text)


def _redact_account_names(obj):
    """递归脱敏账号名，**copy-on-write**：无命中时原对象原样返回（同一引用）。

    两条规则：
      * 键名恰为 "nickname" → 先 _mask_phone（值级，严格），不中再 _mask_text
        （覆盖「老王(13812345678)」这种昵称里嵌号码的形态）
      * 其余字符串值        → _mask_text —— 覆盖服务端**先拼成字符串再返回**的
        logs / msg 通道（昵称根本不在 nickname 键里），这是首轮方案漏掉的那一类

    绝不原地改：这些载荷里有的是直接返回共享 TTL 缓存对象（remaining_usage()、
    usage_snapshot() 都返回缓存本体），原地改会把掩码写进缓存、污染其他读者。
    """
    if isinstance(obj, dict):
        out = None
        for k, v in obj.items():
            if k == "nickname":
                nv = _mask_phone(v)
                if nv is v:
                    nv = _mask_text(v)
            elif isinstance(v, str) and k in _IDENTITY_KEYS:
                # 标识符原样：改写 uid 会让面板拿到池子里不存在的 id（#581）。
                nv = v
            else:
                nv = _redact_account_names(v)
            if nv is not v:
                if out is None:
                    out = dict(obj)
                out[k] = nv
        return obj if out is None else out
    if isinstance(obj, list):
        out = None
        for i, v in enumerate(obj):
            nv = _redact_account_names(v)
            if nv is not v:
                if out is None:
                    out = list(obj)
                out[i] = nv
        return obj if out is None else out
    if isinstance(obj, str):
        return _mask_text(obj)
    return obj

# 日志级别与模块的自动归类。调用点显式传了 level/tag 就用传的，没传才按内容猜。
#
# 级别：早先只认英文关键字，而本仓大量日志是中文——定价侧的「抓取失败，继续用
# 上一版价格」「首次抓取异常」全被判成 INFO，看板上按 ERROR 筛选根本看不到它们。
# 中文关键字补上后这些才落到正确的级别。英文侧同样收紧了几个明显的漏网（"fail"
# 只认 "failed"，"fail:" 这种短标签一直没被认出来）。
_LOG_ERROR_HINTS = (
    "error", "exception", "failed", "fail:", "reject", "traceback", "errno", "fatal",
    "11-128", "11101", "11140",
    "失败", "异常", "错误", "拒绝", "不可用", "无效",
)
_LOG_WARN_HINTS = (
    "warn", "warning", "retry", "timeout", "throttl",
    "重试", "超时", "告警", "警告", "降级", "限流",
)
# 模块：顺序即优先级，越靠前越先判定。新增的 pricing / egress / upstream /
# update / usage / webtools 都是从原先那个大杂烩 "system" 里拆出来的——这些
# 模块本来就在打日志，只是推断规则不认识它们的词，于是一律归到 system，事后
# 想按模块看某一类问题时筛不出来。原有类别（chat/scheduler/tasks/accounts/
# catalog/auth/settings）的相对顺序保持不变，避免改变既有日志的归类。
_LOG_TAG_HINTS = (
    ("chat", ("chat:", "chat done", "request error", "/v1/chat",
              "/chat/completions", "responses")),
    ("scheduler", ("scheduler", "调度器")),
    ("tasks", ("task", "任务", "打卡", "签到", "猫猫", "travel", "growth")),
    ("accounts", ("account", "账号", "pool", "imported")),
    ("pricing", ("pricing", "定价", "取价", "价格", "price", "抓取")),
    ("egress", ("egress", "出口", "槽位", "proxy slot", "mihomo")),
    ("upstream", ("upstream", "上游")),
    ("webtools", ("webtool", "web tool", "本地网络工具")),
    ("catalog", ("model", "catalog", "模型")),
    ("auth", ("auth", "token", "oauth")),
    ("settings", ("settings", "设置")),
    ("update", ("更新", "release")),
    ("usage", ("usage", "用量")),
    ("agents", ("agents", "智能体")),
)

# 可选的 SQLite 落库（默认关闭）。None = 还没初始化（模块 import 期），启动时按
# settings 建好，之后由面板开关驱动 wb_logstore.LogStore.configure()。关闭时它
# 要么是 None、要么 enabled=False，整条日志路径与上游一字不差：只写内存环形
# 缓冲，不建库、不落盘。
LOG_STORE = None


def _log_db_active():
    return LOG_STORE is not None and LOG_STORE.enabled


def add_log_entry(msg, level=None, tag=None):
    global _LOG_COUNTER
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    t_short = time.strftime("%H:%M:%S")
    msg_str = str(msg).rstrip()
    if not level:
        lower = msg_str.lower()
        if any(k in lower for k in _LOG_ERROR_HINTS):
            level = "ERROR"
        elif any(k in lower for k in _LOG_WARN_HINTS):
            level = "WARN"
        else:
            level = "INFO"
    if not tag:
        lower = msg_str.lower()
        for candidate, hints in _LOG_TAG_HINTS:
            if any(k in lower for k in hints):
                tag = candidate
                break
        else:
            tag = "system"
    # 掩码必须发生在 level/tag 推断**之后**：推断用的是原始串（见上），若先掩码，
    # 含手机号的日志行其分类会被改变，属行为回归。buffer 里只存掩码后的文本。
    safe_msg = _mask_text(msg_str)
    with _LOG_LOCK:
        _LOG_COUNTER += 1
        entry = {
            "id": _LOG_COUNTER,
            "ts": ts,
            "time": t_short,
            "level": level,
            "tag": tag,
            "msg": safe_msg,
        }
        LOG_BUFFER.append(entry)
    # 落库在锁外：append 只是入队，但没必要把持锁时间拉长到调用 queue.put。
    if _log_db_active():
        LOG_STORE.append(entry)
    return entry

# ---------------------------------------------------------------------------
# stderr 日志通道：请求线程只入队，真正的 stderr 写入交给唯一的后台线程。
#
# #489 处理的是「写 stderr 会抛异常」（启动终端被回收 → OSError）。还有一种形态它
# 盖不住：stderr 接到「有人建、没人读」的管道时，写满约 64KB 后 write() 会**永久
# 阻塞且不抛异常**，try/except 拦不住。而 send_response() 会先调 log_request()，
# 于是所有不在 log_message 静默白名单里的端点一起停摆，/health /realm /panel/status
# 却照常 200 —— 外部看起来「服务健在、只有面板坏了」，实际请求线程全停在 log()。
#
# Request threads only enqueue; a single daemon thread owns stderr. If that write ever
# blocks, only the writer parks: requests keep flowing and the queue drops lines (with a
# counter surfaced through the panel log buffer) instead of wedging every endpoint.
# ---------------------------------------------------------------------------
_LOG_QUEUE_MAX = 2000
_LOG_QUEUE = queue.Queue(maxsize=_LOG_QUEUE_MAX)
_LOG_WRITER = None
_LOG_WRITER_LOCK = threading.Lock()
_LOG_DROPPED = 0
_LOG_STREAM_CLOSED = False
_LOG_SHUTTING_DOWN = False
_LOG_ATEXIT_REGISTERED = False


def _log_writer_loop():
    """唯一的 stderr 写者：阻塞也只阻塞它自己（daemon，随进程退出）。"""
    global _LOG_STREAM_CLOSED
    while True:
        line = _LOG_QUEUE.get()
        try:
            if line is None:
                return
            if _LOG_STREAM_CLOSED:
                continue
            stream = sys.stderr
            if stream is None:
                # pythonw / 无控制台时并不总是可写对象，别再逐行试
                _LOG_STREAM_CLOSED = True
                continue
            try:
                stream.write(line)
                stream.flush()
            except Exception:
                # 读端消失 / 终端被回收：放弃控制台通道，buffer 与日志库照常
                _LOG_STREAM_CLOSED = True
        finally:
            _LOG_QUEUE.task_done()


def _ensure_log_writer():
    global _LOG_WRITER, _LOG_ATEXIT_REGISTERED
    writer = _LOG_WRITER
    if writer is not None and writer.is_alive():
        return writer
    with _LOG_WRITER_LOCK:
        if _LOG_WRITER is None or not _LOG_WRITER.is_alive():
            worker = threading.Thread(target=_log_writer_loop, name="log-writer",
                                      daemon=True)
            worker.start()
            _LOG_WRITER = worker
            if not _LOG_ATEXIT_REGISTERED:
                # 解释器退出前先让写线程收手。daemon 线程在 finalize 阶段还攥着
                # stderr 的缓冲锁时，CPython 会以 `_enter_buffered_busy` 直接中止
                # 进程 —— 套件里表现为「测试全 OK、退出码非零」，短命的 CLI 进程
                # （例如 console_tail）也会踩到。实测：不加这一层，60 行日志后立刻
                # 退出连续 6/6 次中止；加上后 10/10 干净退出。
                atexit.register(_stop_log_writer)
                _LOG_ATEXIT_REGISTERED = True
    return _LOG_WRITER


def _stop_log_writer(timeout=0.5):
    """atexit：尽力写完已入队的行，再让写线程自己退出（不让它拖住 finalize）。"""
    global _LOG_SHUTTING_DOWN
    flush_log_channel(timeout)
    _LOG_SHUTTING_DOWN = True
    writer = _LOG_WRITER
    if writer is None or not writer.is_alive():
        return
    try:
        _LOG_QUEUE.put_nowait(None)          # 叫醒它：写线程见到 None 就返回
    except queue.Full:
        pass                                 # 队列满＝它正卡在写里，join 会超时
    try:
        writer.join(max(0.05, float(timeout)))
    except Exception:
        pass


def flush_log_channel(timeout=0.5):
    """退出前尽力把已入队的行写完；写卡住时最多等 timeout 秒，绝不为日志拖延退出。

    判据用 `unfinished_tasks`（入队 +1、写完 -1）而不是 `empty()`：写线程 `get()`
    取下最后一行后队列就已经空了，但它可能还在 `write()` 里 —— 退出时漏掉那一行
    的正是这个窗口。卡住的管道同样只让这里等到 timeout。
    """
    try:
        deadline = time.time() + max(0.0, float(timeout))
    except (TypeError, ValueError):
        return
    while time.time() < deadline:
        if _LOG_STREAM_CLOSED or not _LOG_QUEUE.unfinished_tasks:
            return
        time.sleep(0.01)


def log(msg, level=None, tag=None):
    # 控制台 stderr 与看板 buffer 同源脱敏。写入交给后台线程（见 _log_writer_loop）：
    # 队列满只丢行并计数，绝不在请求线程里阻塞，也绝不让日志通道打死请求。
    global _LOG_DROPPED
    if _LOG_SHUTTING_DOWN:
        # 退出途中写线程已收手：只进 buffer / 日志库，不再往队列里放
        add_log_entry(msg, level=level, tag=tag)
        return
    _ensure_log_writer()
    line = f"[wb-proxy] {time.strftime('%H:%M:%S')} {_mask_text(str(msg))}\n"
    try:
        _LOG_QUEUE.put_nowait(line)
    except queue.Full:
        _LOG_DROPPED += 1
        if _LOG_DROPPED == 1 or _LOG_DROPPED % 1000 == 0:
            # 只进 buffer / 日志库，不再走 stderr：那条路已经堵住了
            add_log_entry("stderr log channel is blocked; %d line(s) dropped so far"
                          % _LOG_DROPPED, level="WARN", tag="system")
    except Exception:
        pass
    add_log_entry(msg, level=level, tag=tag)

# 本地网路工具的多引擎轮换、冷却与失败分类写进看板日志（tag=webtools）
wb_webtools.set_logger(log)

def get_logs(limit=200, level="", tag="", search="", since_id=0):
    # 落库开启时以数据库为准：它是完整历史（受保留上限约束），而内存环形缓冲
    # 只留最新 2000 条，拿它回答会看不到刚过去的窗口。查询失败或超时就退回内存，
    # 面板宁可少看历史，也不能因为日志库出问题而整页空白。
    if _log_db_active():
        try:
            stored = LOG_STORE.query(limit=limit, level=level, tag=tag,
                                     search=search, since_id=since_id)
        except Exception:
            stored = None
        if stored is not None:
            return stored
    with _LOG_LOCK:
        items = list(LOG_BUFFER)
    if since_id > 0:
        items = [x for x in items if x["id"] > since_id]
    if level:
        items = [x for x in items if x["level"] == level.upper()]
    if tag:
        items = [x for x in items if x["tag"].lower() == tag.lower()]
    if search:
        s = search.lower()
        items = [x for x in items if s in x["msg"].lower() or s in x["tag"].lower()]
    total = len(items)
    if limit and limit > 0 and since_id == 0:
        items = items[-limit:]
    max_id = items[-1]["id"] if items else since_id
    return {"total": total, "logs": items, "max_id": max_id}

def clear_logs():
    with _LOG_LOCK:
        LOG_BUFFER.clear()
    if _log_db_active():
        try:
            LOG_STORE.clear()
        except Exception:
            pass


def apply_log_db_settings():
    """Read the panel's log-database settings into the live store.

    Called once at startup (after USAGE_DIR is settled) and again on every
    settings save, so flipping the switch or editing a cap takes effect at once
    without a restart. Also pulls the id counter up to whatever the database
    already holds, so ids stay monotonic across a restart and the panel's
    `since_id` incremental fetch keeps working.
    """
    global _LOG_COUNTER
    if LOG_STORE is None:
        return False
    LOG_STORE.configure(
        wb_settings.log_db_enabled(ACCOUNTS_DIR),
        max_rows=wb_settings.log_db_max_rows(ACCOUNTS_DIR),
        max_days=wb_settings.log_db_max_days(ACCOUNTS_DIR),
        max_bytes=wb_settings.log_db_max_bytes(ACCOUNTS_DIR),
    )
    if LOG_STORE.enabled:
        try:
            highest = LOG_STORE.max_id()
        except Exception:
            highest = 0
        with _LOG_LOCK:
            if highest > _LOG_COUNTER:
                _LOG_COUNTER = highest
    return LOG_STORE.enabled

def clean_chunk(raw):
    """Drop the empty noise fields the WorkBuddy gateway pads deltas with."""
    try:
        obj = json.loads(raw)
    except Exception:
        return raw
    changed = False
    if isinstance(obj.get("usage"), dict):
        before = dict(obj["usage"])
        normalize_usage_cache_aliases(obj["usage"])
        if obj["usage"] != before:
            changed = True
    for choice in obj.get("choices") or []:
        if isinstance(choice, dict) and choice.get("finish_reason") == "":
            choice["finish_reason"] = None
            changed = True
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        # PATCHED-BY-OPS: 原判断 `if not delta.get("function_call")` 对
        # {"name":"","arguments":""} 为假（非空 dict 是真值），空占位删不掉。
        # 改为显式检查：name 与 arguments 均空才视为占位噪音。
        fc = delta.get("function_call")
        if fc is not None:
            fc_empty = False
            if isinstance(fc, dict):
                fc_empty = not fc.get("name")
            else:
                fc_empty = not fc
            if fc_empty:
                delta.pop("function_call", None)
                changed = True
        if isinstance(delta.get("tool_calls"), list) and not delta["tool_calls"]:
            delta.pop("tool_calls")
            changed = True
        for key in NOISE_KEYS:
            if key in delta and not delta.get(key):
                delta.pop(key)
                changed = True
        if not delta and not choice.get("finish_reason"):
            return ""
    return json.dumps(obj, ensure_ascii=False) if changed else raw
def _strip_empty_fc(obj):
    """PATCHED-BY-OPS: 递归剔除空 function_call 占位（Responses/chat 通用）。"""
    changed = False
    if isinstance(obj, dict):
        fc = obj.get("function_call")
        if isinstance(fc, dict) and not fc.get("name"):
            obj.pop("function_call", None)
            changed = True
        tc = obj.get("tool_calls")
        if isinstance(tc, list) and not tc:
            obj.pop("tool_calls", None)
            changed = True
        for v in list(obj.values()):
            if _strip_empty_fc(v):
                changed = True
    elif isinstance(obj, list):
        for v in obj:
            if _strip_empty_fc(v):
                changed = True
    return changed
def clean_responses_frame(frame):
    """PATCHED-BY-OPS: 清洗 Responses SSE 帧（bytes）。
    输入 b'event: x\ndata: {...}\n\n'；只改写 data: 行的 JSON，
    event: 行原样保留。解析失败原样返回（不破坏未知格式）。
    """
    if not frame:
        return frame
    try:
        text = frame.decode("utf-8")
    except Exception:
        return frame
    out, changed = [], False
    for line in text.splitlines():
        st = line.strip()
        if st.startswith("data:"):
            payload = st[5:].strip()
            if payload and payload != "[DONE]":
                try:
                    obj = json.loads(payload)
                    if _strip_empty_fc(obj):
                        line = "data: " + json.dumps(obj, ensure_ascii=False)
                        changed = True
                except Exception:
                    pass
        out.append(line)
    return ("\n".join(out) + "\n\n").encode("utf-8") if changed else frame
def normalize_roles(messages):
    """Map role names the upstream rejects onto ones it accepts.
    WorkBuddy only knows system / user / assistant / tool. OpenAI's newer
    "developer" role (used by the Codex CLI and current SDKs) is the same thing
    as "system", but sending it verbatim fails with code 11128.
    """
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            out.append(m)
            continue
        item = m
        if m.get("role") == "developer":
            item = dict(m)
            item["role"] = "system"
        out.append(item)
    return out
# ---------------------------------------------------------------------------
# 模型封锁表
#
# 背景：客户端除了使用者的对话，还会自己发背景请求（记忆整理、自动复核等）。
# 这些请求不经过模型选单，而是直接使用目录上的模型 ID，因此可能在使用者
# 没有实际操作时，用付费模型消耗额度。
#
# 对策（选用）：把要拒绝的模型填进 ALLOWED_MODELS / BANNED_SUBSTRING /
#               EXTRA_BANNED；命中的请求在本机直接回 400，完全不碰上游。
#               预设全部为空 = 不封锁任何模型，行为与原版相同。
#
# 调整方式：
#   要放行某个模型 -> 加进 ALLOWED_MODELS 或 ALLOWED_PREFIXES
#   要连非 gpt 的模型一起挡 -> 加进 EXTRA_BANNED
# ---------------------------------------------------------------------------

# 允许放行的模型（你要用的）
ALLOWED_MODELS = {
    # 预设不封锁任何模型；填入模型 id 即可只放行这些
}

# 允许前缀：涵盖 -high / -preview / [1M] 等变体
ALLOWED_PREFIXES = ()

# 封锁字串：模型名里含这个就拒绝
BANNED_SUBSTRING = ""

# 额外封锁的内部模型（不在 gpt- 前缀内，但也会烧点）
EXTRA_BANNED = set()


def is_model_banned(model):
    """True 表示这个模型名不该被送去上游。

    规则：ALLOWED_MODELS / ALLOWED_PREFIXES 命中就放行；其余只要命中
    BANNED_SUBSTRING 或 EXTRA_BANNED 就拒绝，没命中则照常送往上游。
    三个设定预设都是空的，所以预设不封锁任何模型。
    """
    if not model:
        return False
    m = str(model).strip().lower()
    # 白名单优先（含 -high / -preview / [1M] 这类变体）
    if m in ALLOWED_MODELS:
        return False
    if any(m.startswith(a) for a in ALLOWED_PREFIXES):
        return False
    # 命中封锁字串就拒绝
    if BANNED_SUBSTRING and BANNED_SUBSTRING in m:
        return True
    # 其他已知会烧点的内部模型
    if m in EXTRA_BANNED:
        return True
    return False


# ---------------------------------------------------------------------------
# 背景请求拦截
#
# Codex App 除了使用者的对话，还会自己发背景请求（记忆整理、环境建议、自动复核…）。
# 这些请求不经过模型选单，所以单靠模型白名单挡不住 —— 它们可能直接用目录上
# 的付费模型（例如 gpt-6-astra 这类），在使用者没有实际操作时照样消耗额度。
#
# Codex 会在 client_metadata 里带 x-codex-turn-metadata，内容像：
#   {"request_kind":"memory","thread_source":"memory_consolidation",
#    "turn_trigger":"memory_consolidation"}
# 这里就靠这个标记判断：命中背景关键字 -> 本地直接拒绝，不碰上游、不扣点。
# ---------------------------------------------------------------------------

# 要不要拦截背景请求（False = 全部放行，维持原行为）
BLOCK_BACKGROUND_REQUESTS = False

# 命中任一关键字就视为背景请求（不分大小写、子字串比对）
BACKGROUND_TRIGGER_KEYWORDS = (
    "memory_consolidation",
    "memory",
    "memory-write",
    "memory_write",
    "memorywriting",
    "ambient",
    "suggestion",
    "auto_review",
    "auto-review",
    "autoreview",
    "title",
    "compaction",
    "compact",
    "summariz",
)


# Thread sources that belong to a job the client started on its own. A
# compaction request carries one of these when the client triggered it, and the
# user's own thread when the operator pressed "compact the context" - so the
# source has to be read before the keyword list, where "compaction" matches
# both and would otherwise refuse the button.
BACKGROUND_THREAD_SOURCES = (
    "memory_consolidation",
    "memory",
    "ambient",
    "suggestion",
    "auto_review",
    "autoreview",
    "title",
)


def turn_metadata_fields(payload):
    """Flatten the request_kind / turn_trigger / thread_source hints we get.

    Codex sends them either as plain client_metadata keys or as a JSON string
    under a metadata key of its own, so both shapes are read. Returns {} when
    the payload carries none of them.
    """
    if not isinstance(payload, dict):
        return {}
    meta = payload.get("client_metadata")
    if not isinstance(meta, dict):
        return {}

    # 收集所有可能的来源/触发栏位
    fields = {}
    for key, value in meta.items():
        if isinstance(value, str) and value.strip().startswith("{"):
            try:
                inner = json.loads(value)
            except Exception:
                inner = None
            if isinstance(inner, dict):
                for k in ("request_kind", "turn_trigger", "thread_source", "kind", "trigger", "source"):
                    if k in inner:
                        fields[k] = inner[k]
        if key in ("request_kind", "turn_trigger", "thread_source"):
            fields[key] = value
    return fields


def is_compaction_request(payload):
    """True for the operator's own "compact the context" request.

    request_kind=compaction carries the same word as the background keyword, but
    this request is one the user asked for: the client sends it on the user's
    thread, while a compaction the client started by itself names the job that
    started it. Refusing this one takes the context-compaction button away.
    """
    fields = turn_metadata_fields(payload)
    kind = str(fields.get("request_kind") or "").strip().lower()
    if "compact" not in kind:
        return False
    source = str(fields.get("thread_source") or "").strip().lower()
    return source not in BACKGROUND_THREAD_SOURCES


def background_request_reason(payload):
    """若这是 Codex 自己发的背景请求，回传说明字串；否则回传 ""。

    只看 client_metadata，不碰讯息内容。
    """
    fields = turn_metadata_fields(payload)
    if not fields:
        return ""

    if is_compaction_request(payload):
        return ""

    blob = " ".join(str(v) for v in fields.values()).lower()
    for kw in BACKGROUND_TRIGGER_KEYWORDS:
        if kw in blob:
            return "%s=%s" % (
                ",".join(sorted(fields.keys())),
                ",".join(str(fields[k]) for k in sorted(fields)),
            )
    return ""


def client_effort_of(body):
    """The effort the request itself carries, under either client spelling.

    build_upstream_body() reads both "reasoning_effort" and "reasoningEffort",
    and only fills in the model default for the models it injects for - so a
    camelCase request keeps the camelCase key, and a request to a model the
    catalog pins to one level carries no effort at all.
    """
    if not isinstance(body, dict):
        return None
    return body.get("reasoning_effort") or body.get("reasoningEffort")


def upstream_effort_of(body, model=None):
    """The reasoning effort a request actually runs at, or None when unknown.

    Resolved the way the upstream will apply it:

      - a model the catalog pins to one level (reasoning.effort, no
        supportedEfforts) always runs there: the picker offers no choice for it,
        so a value the request carries anyway does not change the answer;
      - a request that switched thinking off, or asked for "none", ran without
        reasoning and nothing below overrides that;
      - otherwise the client's own value wins, under either spelling;
      - otherwise the model's declared defaultEffort applies.

    Reading the body alone is not enough for the last two: the gateway only
    writes an effort into the body for the models it injects for, so a plain
    request to a pinned model would otherwise be reported as "no effort".
    """
    given = client_effort_of(body)
    if model:
        fixed = model_fixed_effort(model)
        if fixed:
            return fixed
    thinking = (body or {}).get("thinking") if isinstance(body, dict) else None
    if isinstance(thinking, dict) and \
            str(thinking.get("type") or "").strip().lower() == "disabled":
        return "none"
    if str(given or "").strip().lower() == "none":
        return "none"
    if given:
        return given
    return model_default_effort(model) if model else None


def background_request_message(reason):
    return ("這是客戶端自己發的背景請求（%s），本機代理已擋下，"
            "避免在沒有實際操作時消耗上游額度。"
            "要放行請把 wb_proxy.py 的 BLOCK_BACKGROUND_REQUESTS 改成 False。"
            % reason)


def banned_model_message(model):
    allowed = "、".join(sorted(ALLOWED_MODELS))
    return ("模型 %s 已被本機代理封鎖（依 ALLOWED_MODELS / BANNED_SUBSTRING 設定）。"
            "目前允許：%s。要放行請編輯 wb_proxy.py 的 ALLOWED_MODELS。"
            % (model, allowed))


def key_open_kwargs(entry):
    """Open-upstream kwargs carrying a key's account restriction, if any.

    The unrestricted path must not pass the keyword at all: test stubs and
    third-party pool implementations keep their old signature, and None is
    exactly "no restriction".
    """
    allowed = wb_settings.key_allowed_accounts(entry)
    return {"allowed_accounts": allowed} if allowed is not None else {}


def key_model_message(entry, model):
    """Explain a per-key model restriction the same way the global ban does."""
    name = (entry or {}).get("name") or "未命名"
    allowed = "、".join((entry or {}).get("models") or []) or "-"
    asked = str(model or "").strip() or "(未指定模型)"
    return ("API Key「%s」的模型限制不允許 %s。該 Key 目前允許：%s。"
            "請在看板「設置」頁修改這個 Key 的模型限制，或改用允許該模型的 Key。"
            % (name, asked, allowed))


# Process-memory degradation window: content-blocked passthrough/append
# traffic switches to the minimal neutral prompt until the next 00:00 CST
# (panel degrade.go). Restarts clear it, which is fine - the next rejection
# re-triggers it.
PROMPT_DEGRADE = wb_prompt.DegradeGate()


def prompt_mode_config():
    """Prompt mode settings, fail-open to passthrough on any read error."""
    try:
        cfg = wb_settings.prompt_config(ACCOUNTS_DIR)
    except Exception:
        cfg = None
    if not isinstance(cfg, dict):
        cfg = {"mode": "passthrough", "file": "", "text": ""}
    return cfg


def apply_prompt_mode(messages):
    """Apply the configured prompt mode to one request's messages.

    passthrough is the legacy behaviour (client system prompts ride through);
    custom/append are opt-in. While the degrade window is active, passthrough
    and append switch to the minimal neutral prompt; custom never degrades.
    An unreadable prompt file fails open to passthrough rather than blocking.
    """
    cfg = prompt_mode_config()
    mode = cfg.get("mode") or "passthrough"
    degraded = PROMPT_DEGRADE.active()
    if mode in ("custom", "append"):
        try:
            text = wb_prompt.load_prompt(mode, cfg.get("file"), cfg.get("text"))
        except Exception:
            text = ""
        if text:
            return wb_prompt.apply_mode(messages, mode, text, degraded=degraded)
        return messages
    if degraded:
        return wb_prompt.apply_mode(messages, mode, "", degraded=True)
    return messages


def build_upstream_body(payload):
    model = payload.get("model") or ""
    # Map an unsupported level before anything below reads the effort, so the
    # thinking backfill and the logged level describe what will actually run.
    payload = map_reasoning_effort(dict(payload), model)
    # Resolve the effective thinking state before the backfill below: while
    # thinking is on, the upstream requires reasoning_content on every
    # assistant message, whether or not the client kept a reasoning trace.
    thinking = payload.get("thinking")
    thinking_type = ""
    if isinstance(thinking, dict):
        thinking_type = str(thinking.get("type") or "").strip().lower()
    effort = payload.get("reasoning_effort") or payload.get("reasoningEffort")
    thinking_enabled = False
    if str(model).lower().startswith("deepseek"):
        if thinking_type == "enabled":
            thinking_enabled = True
        elif thinking_type != "disabled" and str(effort or "").strip().lower() != "none":
            thinking_enabled = True
    messages = normalize_roles(payload.get("messages") or [])
    # Prompt mode runs before sanitize/backfill so the gateway prompt is the
    # one the upstream sees, with the client's fingerprint-y system text gone.
    messages = apply_prompt_mode(messages)
    messages = sanitize_messages(messages)
    messages = backfill_reasoning_content(
        messages, model, thinking_enabled=thinking_enabled
    )
    if not messages or (messages[0].get("role") != "system"):
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
    body = dict(payload)
    # Private request markers ride along on the chat body for the Responses
    # path (the namespace map, the local-web-tools flag). They are not part of
    # the upstream protocol, so drop them here rather than trusting the
    # upstream to ignore unknown keys.
    for _marker in [k for k in body if str(k).startswith("_")]:
        body.pop(_marker, None)
    # dict(payload) 会把原始模型名一起带过去，所以别名要在这里覆盖回去
    body["model"] = model
    body["messages"] = messages
    # Repair tool-call pairing before the body leaves: a call whose result never
    # came back, results split from their batch by an interleaved message, or a
    # parallel batch split into adjacent assistant messages makes the upstream
    # reject every later turn of that conversation. Order matters: merge the
    # split declarations first, so repack sees one complete batch.
    repaired, _merged = merge_adjacent_tool_calls(body["messages"])
    repaired, _repacked = repack_tool_result_blocks(repaired)
    repaired, _cleaned = cleanup_orphan_tool_calls(repaired)
    body["messages"] = repaired
    translate_max_completion_tokens(body)
    # Upstream GPT-family models enforce a minimum max_output_tokens >= 16;
    # smaller values (e.g. max_tokens=1 from probes) fail with 400 integer_below_min_value (#364, #252).
    if "max_tokens" in body and str(model).lower().startswith("gpt-"):
        try:
            mt = int(body["max_tokens"])
            if mt < 16:
                log("clamping %s max_tokens from %d to 16 for upstream requirements" % (model, mt))
                body["max_tokens"] = 16
        except (TypeError, ValueError):
            pass
    normalize_tool_choice(body)
    normalize_tools(body)
    normalize_stop(body)
    if "max_tokens" not in body:
        default_max = model_default_max_output_tokens(model)
        if default_max:
            body["max_tokens"] = default_max
    # Thinking injection for DeepSeek models.
    #
    # thinking.type=enabled on its own does not switch the reasoning trace on:
    # the upstream still answers without one unless an effort level rides along.
    # Measured against the live upstream on deepseek-v4.1-flash, same prompt:
    #   enabled + no effort   -> reasoning_tokens 0,  reasoning_content len 0
    #   reasoning_effort=high -> reasoning_tokens 37, reasoning_content len 117
    # The client's own choice always wins; an effort level is only filled in
    # when it left the field out, and never for a request that opted out.
    if str(model).lower().startswith("deepseek"):
        thinking = body.get("thinking")
        opted_out = isinstance(thinking, dict) and \
            str(thinking.get("type") or "").strip().lower() == "disabled"
        effort = body.get("reasoning_effort") or body.get("reasoningEffort")
        if not opted_out and str(effort or "").strip().lower() != "none":
            if "thinking" not in body:
                body["thinking"] = {"type": "enabled"}
            if not effort:
                mode = default_reasoning_effort_mode()
                if mode != "off":
                    chosen = model_default_effort(model)
                    if not chosen and mode == "high":
                        chosen = "high"
                    if chosen:
                        body["reasoning_effort"] = chosen
    body["stream"] = True
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}
    return body


def encode_upstream_body(body):
    """Serialize a chat body the way the official client does.

    官方客户端是 Node 的 JSON.stringify：token 之间没有空白。json.dumps 默认的
    separators=(", ", ": ") 会多出空格，body 字节与 Content-Length 都成一处机器
    特征（上游解析结果等价，只影响线上字节）。单独成函数，让「出站 chat body
    用紧凑 JSON」有唯一入口、可被测试钉住。
    """
    return json.dumps(body, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def model_catalog_meta(model):
    """The catalog metadata dict for a model, or {}.

    Read from the same merged catalog that /v1/models advertises.
    Deliberately side-effect free: it reads the already-populated model cache
    and the shipped static tables only. Calling fetch_models() here would let a
    cold cache trigger an upstream discovery round-trip from inside request
    handling, turning one chat call into a network fetch.
    """
    if not model:
        return {}
    try:
        realm = detect_model_realm(model) or CURRENT_REALM
        entries = (_models_cache.get(realm) or {}).get("data")
        if not entries:
            name = "STATIC_CN_MODELS" if realm == "cn" else "STATIC_INTL_MODELS"
            table = getattr(wb_catalog, name, None) or wb_catalog.STATIC_MODELS
            entries = [(m.get("id"), m) for m in table if isinstance(m, dict)]
        m_lower = str(model).strip().lower()
        for mid, meta in entries:
            if str(mid).strip().lower() == m_lower:
                return meta or {}
    except Exception as exc:
        log("catalog meta lookup failed for '%s': %s" % (model, exc))
    return {}


def model_reasoning_meta(model):
    """The catalog's reasoning block for a model, or {}."""
    return model_catalog_meta(model).get("reasoning") or {}


def model_default_max_output_tokens(model):
    """The max output tokens the catalog declares for a model, or None."""
    val = model_catalog_meta(model).get("maxOutputTokens")
    if val is not None:
        try:
            val = int(val)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    return None


def model_default_effort(model):
    """The effort the catalog applies when the client asks for none, or None."""
    effort = model_reasoning_meta(model).get("defaultEffort")
    return effort.strip() if isinstance(effort, str) and effort.strip() else None


def _thinking_off_value(value):
    """Whether an effort value means "no reasoning" rather than a level."""
    return str(value or "").strip().lower() in ("off", "none", "disabled", "disable")


def _fallback_effort(reasoning):
    """The level to use when the requested one is not available.

    Mirrors the catalog's own precedence: an explicit defaultEffort, then a
    pinned effort, then the first supported level (the catalog order is the
    upstream's, so its first entry is at least one the model accepts).
    """
    default = reasoning.get("defaultEffort")
    if isinstance(default, str) and default.strip():
        return default.strip()
    efforts = reasoning.get("supportedEfforts") or []
    if not efforts:
        fixed = reasoning.get("effort")
        if isinstance(fixed, str) and fixed.strip():
            return fixed.strip()
    if efforts:
        first = str(efforts[0]).strip()
        if first:
            return first
    return None


def map_reasoning_effort(body, model):
    """Rewrite an effort the model does not accept to its default level.

    A client with a fixed level picker (low/medium/high) keeps sending levels
    a model does not declare; the upstream then rejects the whole request
    instead of picking one. When the catalog knows the model's levels, an
    unsupported value is replaced with the model's default; a request that
    disables thinking is only kept when the model allows disabling it.

    Off via the reasoning_effort_map setting. Only the value changes: the key
    keeps its original spelling and every other field is left alone.
    """
    if not isinstance(body, dict) or not model:
        return body
    if not wb_settings.reasoning_effort_map(ACCOUNTS_DIR):
        return body
    key = None
    for candidate in ("reasoning_effort", "reasoningEffort"):
        if candidate in body:
            key = candidate
            break
    if key is None:
        return body
    raw = body.get(key)
    if not isinstance(raw, str):
        return body
    requested = raw.strip()
    if not requested:
        return body
    reasoning = model_reasoning_meta(model)
    if not reasoning:
        return body
    efforts = reasoning.get("supportedEfforts") or []
    fixed = reasoning.get("effort")
    fixed = fixed.strip() if isinstance(fixed, str) and fixed.strip() else ""
    if _thinking_off_value(requested):
        # "none" is not a level, and the catalog often omits the flag for
        # models that do allow switching thinking off (the deepseek default).
        # Only an explicit false makes it unavailable; anything else keeps the
        # request's own opt-out.
        if reasoning.get("canDisableThinking") is not False:
            return body
        replacement = _fallback_effort(reasoning)
    elif efforts:
        if any(str(level).strip().lower() == requested.lower()
               for level in efforts):
            return body
        replacement = _fallback_effort(reasoning)
    elif fixed:
        if fixed.lower() == requested.lower():
            return body
        replacement = _fallback_effort(reasoning)
    else:
        # The catalog only says the model reasons, without a level list:
        # there is nothing to check the request against.
        return body
    if not replacement or replacement == requested:
        return body
    body[key] = replacement
    return body


def default_reasoning_effort_mode():
    """How to fill reasoning_effort when the client left it out.

    high    - catalog default, else "high" (legacy behaviour, default)
    catalog - catalog default only; leave the field out when the model has none
    off     - never inject a default

    The forced "high" is what makes deepseek spend the whole round on reasoning
    and occasionally return no output text (issue #508).
    """
    raw = os.environ.get("WB_DEFAULT_REASONING_EFFORT", "").strip().lower()
    if raw in ("off", "none", "0", "false", "no"):
        return "off"
    if raw in ("catalog", "model", "default"):
        return "catalog"
    return "high"


def model_fixed_effort(model):
    """The effort the catalog pins a model to, or None when it is selectable.

    reasoning.effort without supportedEfforts means the model always runs at
    that level: /v1/models advertises it as reasoning_fixed_effort and the
    picker offers no choice for it, so an effort the request carries anyway does
    not change what ran. A model that declares supportedEfforts is selectable
    whatever else its block carries - an `effort` beside it is the default, not
    a pin.
    """
    reasoning = model_reasoning_meta(model)
    if reasoning.get("supportedEfforts"):
        return None
    effort = reasoning.get("effort")
    return effort.strip() if isinstance(effort, str) and effort.strip() else None


def prompt_cache_key_enabled():
    """Whether to inject prompt_cache_key into outbound requests.

    Off by default. The upstream turns out to cache repeated prefixes on its
    own: with an identical ~8k-token prefix sent twice, the second call already
    reports prompt_cache_hit_tokens=9600 and the same credit with or without
    this field, on both exits (www.workbuddy.ai and copilot.tencent.com) and on
    both a free and a billed model. Injecting it changed neither the hit rate
    nor the charge, so it is left as an opt-in for experimenting rather than
    added to every request.
    """
    return os.environ.get("WB_PROMPT_CACHE_KEY", "0").strip().lower() in (
        "1", "true", "yes", "on")


def inject_prompt_cache_key(body, uid, conversation):
    """Add the upstream prompt_cache_key so its prefix cache can be reused.

    Kept for experimentation only: measurement on this upstream showed the
    prefix cache working without it (see prompt_cache_key_enabled). The key
    still carries the account uid, because the upstream cache is scoped per
    account -- a key shared between accounts would let one account's request
    read another's cached prefix, so this must never be made a fixed string.

    Priority matches the client's intent: an explicit prompt_cache_key is never
    overwritten; then the body's own conversation id; then the session key the
    gateway resolved (header or conversation-prefix derived).
    """
    if not isinstance(body, dict):
        return body
    existing = body.get("prompt_cache_key")
    if isinstance(existing, str) and existing.strip():
        return body
    conv = conversation if isinstance(conversation, str) else ""
    for field in ("conversation_id", "conversationId"):
        value = body.get(field)
        if isinstance(value, str) and value.strip():
            conv = value.strip()
            break
    uid8 = (uid or "")[:8] or "-"
    seed = "%s|%s" % (uid or "", conv)
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    out = dict(body)
    out["prompt_cache_key"] = "wb2a-%s-%s" % (uid8, digest)
    return out
class PinnedAccountError(Exception):
    """A /u/<ref> request cannot use the account it was pinned to.

    Deliberately not rotated to another account: the caller asked for this
    one, so the failure has to be reported instead of silently changing it.
    """

    def __init__(self, status, message, account_uid=None):
        self.status = int(status)
        self.message = str(message)
        self.account_uid = account_uid
        super().__init__(self.message)


class ContentRejected(Exception):
    """Upstream content review rejected this request (403 / code 11140).

    Not an account problem: another credential gets the same 403 for the same
    content, so it is passed straight through instead of cooling the pool.
    """

    def __init__(self, http_error=None, detail=""):
        self.http_error = http_error
        self.detail = detail or ""
        super().__init__("upstream rejected the request content (403)")

    @property
    def code(self):
        return 403


class RateLimited(Exception):
    """Upstream throttled this model (429 / code 6004). Distinct from a dead
    pool: the credential is fine, only the model is cooling down for a while."""

    def __init__(self, http_error=None, detail="", wait=60, message=""):
        self.http_error = http_error
        self.detail = detail or ""
        self.wait = max(1, int(wait or 60))
        # 429s answered without an upstream call (the pool is parked by the
        # daily token guard) carry their own text instead of the upstream
        # wording.
        self.message = message or ""
        super().__init__("upstream rate limit: %s" % (self.detail[:200] or "429"))


def gateway_hint(status, message):
    """A gateway-side note that sits beside the raw upstream message.

    The hint never replaces the upstream wording (clients keep parsing the
    same envelope); it only explains what the gateway could classify, so a
    user staring at "400 Bad Request" learns whether the fix is a smaller
    context, a different model, a re-login or simply waiting. An empty string
    means "nothing to add" - no hint is invented for unknown failures.

    Covers the shapes the hub can classify from the upstream body/status:
    11133 model_param_invalid, 11135 invalid_image_data, rate limits, content
    review, exhausted credits, dead sessions, missing models, prompt-length
    rejections, WAF blocks and the local no-usable-account case.
    """
    text = str(message or "")
    lower = text.lower()
    if status == 413 and "payload too large" in lower:
        return ("single request body exceeds the gateway cap "
                "(WB_MAX_PAYLOAD_BYTES, default 16MB); raise it and restart wb-proxy")
    if status == 503 and "concurrent chat limit" in lower:
        return "gateway is busy at its concurrency limit; retry shortly"
    if ("11133" in lower or "model_param_invalid" in lower
            or "invalid request parameters" in lower):
        return ("request parameters were rejected by the model provider; "
                "check message format and model capabilities")
    if ("11135" in lower or "invalid_image_data" in lower
            or "replace the image" in lower):
        return ("image data rejected by upstream; use a real/valid image, "
                "may need a new conversation")
    if ("11140" in lower or "账号存在异常行为" in text
            or "account has been restricted" in lower
            or "unusual activity" in lower):
        return ("上游已对该账号做账号级风控限制（403 / code 11140）；网关已让该账号"
                "冷却并停止换号重试。请勿换号或高频重试（同设备账号会连坐），"
                "按 WorkBuddy 官方渠道申诉，通常 1-3 个工作日自动解除。")
    if "no usable account" in lower or "no healthy account" in lower:
        return "no healthy account available in pool; check /status or retry later"
    if "本机网络不可用" in text or "local network unavailable" in lower:
        return ("this machine has no working network (DNS/route failed); accounts were "
                "not penalised - check the host's network or proxy")
    if ("context length" in lower or "context_length" in lower
            or "prompt too long" in lower or "too many tokens" in lower
            or "maximum context" in lower):
        return ("request context exceeds the model's limit; reduce "
                "history/message size")
    if status == 429 or "rate limit" in lower or "frequency limit" in lower:
        return "rate limited by upstream; retry after reset"
    if status == 402 or ("insufficient" in lower and "credit" in lower):
        return ("account credits exhausted at upstream; waiting for daily "
                "check-in to restore")
    if "session" in lower and ("not found" in lower or "expired" in lower):
        return ("account session expired at upstream; the account is disabled "
                "until re-login")
    if "content" in lower and ("reject" in lower or "policy" in lower):
        return ("request content was rejected by content policy; adjust the "
                "prompt and retry")
    if ("no such model" in lower or "model not found" in lower
            or "unsupported model" in lower):
        return ("upstream has no such model on this backend; switch model or "
                "retry on another account")
    if status == 403 and "waf" in lower:
        return "upstream WAF blocked the gateway; retry after the block window"
    return ""


# ---------------------------------------------------------------------------
# 出站身分自动切换
#
# 官方有三套身分（workbuddy / vscode / cli），端点与配额通道各不相同，
# 对照表见 wb_identity._ENDPOINTS。
#
# 某模型在某条通道被限流（429 / code 6004）时，换成另一套身分通常还能继续
# 用 —— 那是另一条配额线。每轮最多切 MAX_PRODUCT_SWITCHES 次，避免来回弹跳。
#
# 身分会写进凭证档并在重启后读回（issue #76）：面板手动切换当下就落盘，
# 这里的自动切换则在下一次任何 save() 时一并写入。
#
# 这个开关交给面板设定决定（issue #67），预设关闭：自动切换会吃掉重试预算，
# 也会把帐号留在操作者没主动选过的身分上，要用的话自己开。
# ---------------------------------------------------------------------------

MAX_PRODUCT_SWITCHES = 4
_SWITCH_LOG = {}

# --------------------------------------------------- 账号级并发上限（可选）
#
# 上游按「账号 + 分钟」计 token，而一个大上下文会话单请求就带十几万 token。
# 两个会话同时落在同一个账号上，一分钟内很容易过百万 token，上游回 429；网关
# 随即把该账号停放 60 秒并换号，新账号没有这段对话的前缀缓存，缓存命中率从
# 99% 掉到 0、TTFT 从 1.5s 涨到 5-6s —— 并发越高，每个请求反而越慢。
#
# 这里给每个账号一个「在飞名额」：名额满了就换下一个账号（不是排队等），
# 这样两个会话会自然落到两个账号上，各自保留自己的前缀缓存。
#
# 默认 0 = 不限，派发行为与上游一字不差（见 wb_settings.account_max_concurrent）。
_ACCOUNT_INFLIGHT = {}
_ACCOUNT_INFLIGHT_LOCK = threading.Lock()


def account_slot_acquire(account, limit=None):
    """给这个账号占一个在飞名额。回传 True 表示占到了。

    上限 <= 0（默认）时永远回传 True 且不记账：关闭状态下这条路径不产生任何
    状态，也就不可能有泄漏。`limit` 由调用方在读设置时算好传进来，避免同一次
    请求里「占名额时开关是开的、还名额时已经关了」这种不一致。
    """
    if limit is None:
        limit = wb_settings.account_max_concurrent(ACCOUNTS_DIR)
    if limit <= 0:
        return True
    with _ACCOUNT_INFLIGHT_LOCK:
        current = _ACCOUNT_INFLIGHT.get(account.uid, 0)
        if current >= limit:
            return False
        _ACCOUNT_INFLIGHT[account.uid] = current + 1
        return True


def account_slot_release(account):
    """归还名额。必须与成功的 acquire 配对；关闭时是空操作。"""
    if account is None:
        return
    with _ACCOUNT_INFLIGHT_LOCK:
        current = _ACCOUNT_INFLIGHT.get(account.uid, 0)
        if current <= 1:
            _ACCOUNT_INFLIGHT.pop(account.uid, None)
        else:
            _ACCOUNT_INFLIGHT[account.uid] = current - 1


def bind_account_slot_to_response(response, account):
    """把在飞名额绑到响应对象的生命周期上。

    名额必须一直占到「上游 body 读完/关掉」为止，而不是占到「拿到响应头」——
    流式回复拿到头之后还要读几十秒，那段时间上游仍算这个账号在跑。响应对象的
    close() 是唯一必然发生的终点（`with upstream:`、显式 close、客户端中断都走
    它），所以把归还挂在 close 上，并加一次性保护防重复扣减。
    """
    if account is None:
        return
    if response is None:
        # 没有响应对象可挂：没有终点可等，立刻归还，否则这个名额会永久泄漏。
        account_slot_release(account)
        return
    released = [False]

    def release_once():
        if released[0]:
            return
        released[0] = True
        account_slot_release(account)

    try:
        original_close = response.close
    except Exception:
        release_once()
        return

    def close_and_release(*args, **kwargs):
        try:
            return original_close(*args, **kwargs)
        finally:
            release_once()

    try:
        response.close = close_and_release
    except Exception:
        # 内置类型不接受实例属性赋值：退回「读完即还」，宁可早还也不能漏还。
        release_once()


def account_inflight_snapshot():
    """{uid: 在飞数}，面板与测试用。"""
    with _ACCOUNT_INFLIGHT_LOCK:
        return dict(_ACCOUNT_INFLIGHT)


#: 出口级退避在单次请求里最多睡这么久（见 wb_egress）：退避阶梯可以到十几秒，
#: 但把一个客户端请求按住十几秒是不可接受的，所以只睡这个上限；共享计数器仍然
#: 让并发请求一起变慢（这才是「一次短暂的全局退避」）。
EGRESS_SLEEP_MAX = 5.0

#: 固定账号（/u/<ref>/ 路由、流式中断原地重连）在名额占满时最多等这么久。
#: 固定账号没法「换一个」，所以只能等；但这个等待必须有界，否则一个卡住的
#: 流会把同账号的后续请求全部拖死。
ACCOUNT_SLOT_WAIT_MAX = 2.0


def auto_switch_product_enabled():
    """Whether a 429 may rotate the outbound identity (panel setting, off by default)."""
    return wb_settings.auto_switch_product(ACCOUNTS_DIR)


def _switch_count(account, model):
    """这一轮已经切过几次（60 秒内的切换算同一轮）。"""
    entry = _SWITCH_LOG.get((account.uid, model))
    if not entry:
        return 0
    count, last = entry
    if time.time() - last > 60:
        return 0
    return count


def _try_switch_product(account, model):
    """429 时换身分重试。回传 True 表示已切换、可以重试。

    只会在面板配置允许的身份之间按 WB → VSC → CLI 顺序切换；同一请求内最多
    切 MAX_PRODUCT_SWITCHES 次，四次都不行就放弃，让呼叫端回报真正的 429。
    """
    count = _switch_count(account, model)
    if count >= MAX_PRODUCT_SWITCHES:
        return False
    current = getattr(account, "product", wb_identity.PRODUCT_DESKTOP)
    allowed = wb_settings.auto_switch_products(ACCOUNTS_DIR)
    sequence = (wb_identity.PRODUCT_DESKTOP, wb_identity.PRODUCT_VSCODE,
                wb_identity.PRODUCT_CLI)
    try:
        start = sequence.index(current)
    except ValueError:
        start = -1
    target = None
    for offset in range(1, len(sequence) + 1):
        candidate = sequence[(start + offset) % len(sequence)]
        if candidate != current and candidate in allowed:
            target = candidate
            break
    if not target:
        return False
    try:
        changed = account.set_product(target)
    except Exception as exc:
        log("product switch failed: %s" % exc, level="WARN")
        return False
    if not changed:
        return False
    _SWITCH_LOG[(account.uid, model)] = (count + 1, time.time())
    if len(_SWITCH_LOG) > 500:
        _SWITCH_LOG.clear()
    log("account %s: %s 被限流，自動切換身分 -> %s (第 %d/%d 次)"
        % (account.uid[:8], current, target, count + 1, MAX_PRODUCT_SWITCHES),
        level="WARN")
    return True


def reset_switch_counter(account, model):
    """成功之后归零，下一次请求重新享有 4 次切换额度。"""
    _SWITCH_LOG.pop((account.uid, model), None)


def retry_after_seconds(model, realm):
    """Shortest wait until any account of this realm can serve `model` again."""
    if not POOL:
        return 60
    waits = [a.throttle_wait(model=model) for a in POOL.accounts
             if a.realm == realm and a.enabled and a.access_token]
    active = [w for w in waits if w > 0]
    return int(min(active)) if active else 60


def realm_model_throttled(realm, model):
    """True when accounts exist and are healthy but all are cooling this model.

    Only a *model* cooldown counts. A plain account cooldown usually comes from
    a transient network error, and reporting that as "rate limited" told
    clients to back off from a model that was never throttled.
    """
    if not POOL:
        return (False, 0)
    existing = [a for a in POOL.accounts
                if a.realm == realm and a.enabled and a.access_token]
    if not existing:
        return (False, 0)
    waits = []
    for a in existing:
        wait = getattr(a, "model_cooldowns", {}).get(model, 0.0) - time.time()
        waits.append(max(0.0, wait))
    if waits and all(w > 0 for w in waits):
        return (True, int(min(waits)))
    return (False, 0)


def _network_error_details(exc):
    """Read wrapped OS codes without depending on the host's error language."""
    errors = (exc, getattr(exc, "reason", None))
    text = " ".join("%s %s" % (type(error).__name__, error)
                    for error in errors if error is not None).lower()
    # HTTPError inherits OSError, but its missing attributes may be delegated
    # to an absent/closed response on Python 3.9. HTTP status is not an OS
    # error code, so never probe errno/winerror on that response wrapper.
    codes = {getattr(error, attr, None) for error in errors
             if not isinstance(error, urllib.error.HTTPError)
             for attr in ("errno", "winerror")}
    # Some urllib callers only retain a string. Match the complete number:
    # the old substring "errno 100" also matched Windows ECONNRESET (10054).
    codes.update(int(code) for code in re.findall(
        r"\b(?:errno|winerror)\s+(-?\d+)\b", text))
    return codes, text


def is_transient(exc):
    """Network-level flakiness that deserves a retry, not a cooldown.

    Upstream occasionally drops a TLS handshake mid-stream
    (SSL: UNEXPECTED_EOF_WHILE_READING / Remote end closed connection).
    Treating that as a dead account took the only intl account offline for 60s
    and turned one hiccup into a 502 storm.
    """
    # A certificate/hostname verification failure is deterministic: every
    # retry hits the same chain. Treating it as a transport hiccup consumed
    # the egress budget and turned a configuration error into a 502 storm.
    if wb_accounts.is_tls_verify_error(exc):
        return False
    codes, t = _network_error_details(exc)
    # ECONNREFUSED (10061) stays out on purpose: nothing is listening, which
    # usually means a dead per-account proxy. It keeps the old charge + 60s
    # cooldown path so the pool moves on instead of re-picking that account.
    if codes.intersection((errno.EPIPE, errno.ECONNABORTED, errno.ECONNRESET,
                           errno.ETIMEDOUT, 10053, 10054, 10060)):
        return True
    markers = (
        "ssl", "unexpected_eof", "eof occurred", "remote end closed",
        "connection reset", "connection aborted", "connectionreseterror",
        "connectionabortederror", "timed out", "timeout", "temporarily unavailable",
        "bad gateway", "502", "503", "504", "incompleteread",
    )
    return any(m in t for m in markers)


# Upstream bodies that say the model/service is unavailable, not the
# credential. 11134 = model provider temporarily unavailable, 11102 =
# service info not found, 11150 = reasoning effort unsupported, 11158 =
# model not available, 11129 = invalid function parameters.
MODEL_SCOPED_ERROR_CODES = frozenset((11102, 11129, 11134, 11150, 11158))


def upstream_error_code(detail):
    """The numeric code field of an upstream error body, or None."""
    try:
        payload = json.loads(detail or "")
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return int(payload.get("code"))
    except (TypeError, ValueError):
        return None


def http_error_detail(exc, limit=600):
    """Read an HTTPError body and rewind it for the caller.

    open_upstream has to inspect the body to tell a model-level 5xx from an
    account fault, but the HTTP handler later reads the same exception to build
    the client response. Keep the bytes on the exception so both readers work.
    """
    try:
        raw = exc.read(limit)
    except Exception:
        return ""
    try:
        exc.wb_body = raw
    except Exception:
        pass
    return raw.decode("utf-8", "replace")


def read_http_error(exc, limit=600):
    """The body read by http_error_detail, or a fresh read for other errors."""
    raw = getattr(exc, "wb_body", None)
    if raw is not None:
        return raw[:limit]
    try:
        return exc.read(limit)
    except Exception:
        return b""


def model_scoped_errors_penalize_account():
    """Escape hatch: restore the old "any 5xx charges the account" behaviour."""
    raw = os.environ.get("WB_MODEL_SCOPED_ERRORS_PENALIZE", "")
    return str(raw).strip().lower() in ("1", "true", "yes", "on")

# 「本机没网」的指纹：DNS 解析不出来、路由不可达、网卡没起来。这些失败里账号
# 连一个字节都没发出去，结局却和「上游挂了」长得一样（都是 URLError），所以必须
# 单独认出来。
LOCAL_NETWORK_MARKERS = (
    "gaierror", "getaddrinfo failed", "name or service not known",
    "nodename nor servname", "temporary failure in name resolution",
    "network is unreachable", "network is down", "no route to host",
)

def looks_like_local_network(exc):
    """这次失败是「本机没网」，还是「上游出问题」。

    两者在账号治理上必须分开：本机断网时每一次尝试都会失败，若照旧逐账号记一笔
    并把账号冷却 60 秒，等网络恢复时池子里已经全是冷却中的账号——生产上就是这么
    把 9 个能用的账号全关掉的，用户看到的是「明明测了能用，它还熔断」。
    """
    if isinstance(exc, socket.gaierror):
        return True
    codes, lower = _network_error_details(exc)
    if codes.intersection((errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH,
                           -2, -3, -5, 100, 101, 113,
                           10050, 10051, 10065, 11001)):
        return True
    return any(marker in lower for marker in LOCAL_NETWORK_MARKERS)

class LocalNetworkError(Exception):
    """本机网络不通：不是账号的问题，也不是上游的问题。

    单独成一个类型，是因为调用方需要给出不同的建议（去查网线/Wi-Fi/代理，而不是
    去查账号或等上游），也因为它绝不该给账号记失败。
    """

    def __init__(self, detail=""):
        self.detail = detail or ""
        # 中英并列：日志里 grep「本机网络不可用」或 "local network unavailable" 都能
        # 命中，而 str(exc) 落到用户可见处（比如上游报错信封）时也是人话。
        super().__init__("本机网络不可用 (local network unavailable): %s"
                         % (self.detail[:200] or "DNS/路由失败"))

def exc_detail(exc, limit=180):
    """异常的可读原因（类型 + 底层文本），而不只是类型名。

    这些行原先只插值 `type(exc).__name__`，于是现场只剩一串 `URLError`：事后
    分不清那一下到底是 SSL 握手中断、连接被重置还是读超时，而这三者指向完全
    不同的根因——上游边缘在丢连接 / 连接被重置 / 连接池复用了已死的连接。
    URLError 的真实原因挂在 `.reason` 上（`str(exc)` 会包一层 `<urlopen error
    ...>`，`.reason` 才是干净的内层文本），所以优先取 `.reason`，空则退回
    `str(exc)`，再空则退回类型名，保证这一格永远有内容。
    """
    reason = getattr(exc, "reason", None)
    text = str(reason) if reason is not None else str(exc)
    text = (text or "").strip()
    if not text:
        return type(exc).__name__
    return ("%s: %s" % (type(exc).__name__, text))[:limit]

# The shapes the upstream uses for a rate limit. A body naming one is a soft
# limit; it still says nothing about scope, so the caller decides that from the
# reset clock (one model), from credential wording (the account), or - when
# neither is present - from repetition, and then only on one model.
SOFT_LIMIT_MARKERS = ("usage exceeds frequency limit", "too many requests",
                      "请求过于频繁")
SOFT_LIMIT_CODES = (14003,)     # intl, observed on a real 429

# Wording that scopes a 429 to the credential itself. These are matched as whole
# words or explicit phrases, never as bare substrings: a 429 body carries
# metadata such as "accountId", "account_id" or "credentialId", and a substring
# test reads those field names as a scope statement and hands the credential the
# 10m -> 2h ladder. Provisional: no captured body has shown yet what a genuine
# account-level 429 says, so the list stays short and fails towards the narrow
# model window.
ACCOUNT_SCOPE_PHRASES = (
    "account-level", "account level", "account-wide", "account scope",
    "account quota", "account rate limit", "account limit",
    "per account", "per-account", "this account", "your account",
    "the account", "an account",
    "credential-level", "credential level", "credential scope",
    "credential limit", "per credential", "this credential",
    "the credential", "your credential",
    "账号级", "账户级", "该账号", "此账号", "该账户", "此账户",
    "账号限流", "账户限流", "凭证级", "该凭证",
)


def credential_scope_phrase(detail):
    """The scope phrase the body uses, or None when it names no scope.

    ASCII phrases are anchored on word boundaries, so an identifier like
    `accountId` cannot satisfy one. The Chinese phrases are plain substrings:
    they have no word boundary to anchor to and cannot occur inside an
    identifier.
    """
    lowered = (detail or "").lower()
    for phrase in ACCOUNT_SCOPE_PHRASES:
        if phrase.isascii():
            if re.search(r"\b%s\b" % re.escape(phrase), lowered):
                return phrase
        elif phrase in lowered:
            return phrase
    return None


def rate_limit_code(detail):
    """The `code` field of a JSON 429 body, or None when it is not JSON."""
    try:
        obj = json.loads(detail)
    except Exception:
        return None
    return obj.get("code") if isinstance(obj, dict) else None


def rate_limit_is_soft_shape(detail):
    """True when a 429 body names a rate limit, whatever its scope.

    Both shapes the incident environment produced are covered: the bare "usage
    exceeds frequency limit" body (229 of the 263 recorded 429s) and the intl
    code 14003 body ("too many requests"). Recognising them is what lets the
    caller answer with a window on one model rather than on the credential.
    """
    text = (detail or "").lower()
    if any(marker in text for marker in SOFT_LIMIT_MARKERS):
        return True
    return rate_limit_code(detail) in SOFT_LIMIT_CODES


# 上游「这个模型的窗口额度用满」的应答码。6004 是唯一带重置墙钟的额度判定，
# 也是用户口径里「达到限额」的唯一标志：别的 429（14003、裸频率限制文本、
# 账号级软限）即使碰巧带了个时钟，也不是额度用满的证据。
CAP_LIMIT_CODE = 6004


def rate_limit_is_cap(detail):
    """True only for the upstream's quota-cap answer (code 6004).

    Tolerant on purpose: the field has only ever been seen as an integer, but
    a string form must not silently drop a real cap. Anything unparsable is
    not a cap, so the caller's persistent-disable path fails closed.
    """
    try:
        return int(rate_limit_code(detail)) == CAP_LIMIT_CODE
    except (TypeError, ValueError):
        return False


def rate_limit_is_account_level(detail, reset_at):
    """True only with positive evidence that the 429 is credential-scoped.

    The model-scoped form names a reset wall clock (code 6004). Treating the
    *absence* of one as account scope made every 429 without a timestamp - which
    includes the most common body the upstream sends - buy the whole credential
    a 600s cooldown doubling to 7200s, so a handful of them left every account of
    a realm cooling at once while a panel test kept reporting the credential
    healthy. Absence is evidence of nothing: the credential is parked only when
    the body itself says the limit is on the credential; everything else gets a
    window on one model, decided by the caller.

    Provisional: the phrase list is short because no captured body has shown yet
    what a genuine account-level 429 looks like, so it is meant to be re-checked
    against one rather than grown speculatively.
    """
    if reset_at is not None:
        return False
    return credential_scope_phrase(detail) is not None


#: 上游风控限制（HTTP 403, code 11140「账号存在异常行为，已被限制使用」）
#: 的账号冷却时长。定义在 wb_accounts：这是账号级状态且要持久化
#: （restrictedUntil），时长跟着它走，避免两处各写一份。
RESTRICTED_403_COOLDOWN = wb_accounts.RESTRICTED_ACCOUNT_COOLDOWN

# --- 风控连坐保护 ---------------------------------------------------------
# 社区复盘（Zhengyuuuui/workbuddy2api《封禁事件复盘与改进执行清单》）给出的
# 处置口径：看到 11140 立即停手，解封前严禁换号继续打同一台设备——设备指纹会
# 连坐。网关原来在 11140 上「冷却该号、立刻换下一个号继续同一请求」，一个请求
# 能遍历整个池子；#422 用户实测 7 个号在一次任务里全被封。下面四个开关就是这条
# 红线的代码化，默认值按「宁可少服务一会儿，也别把账号送走」取：
#   WB_RESTRICTED_403_ROTATE_MAX  一次请求最多再换几个号（默认 0 = 不换）
#   WB_RESTRICTION_WAVE_MIN       窗口内几个号被限制就整池暂停（默认 2）
#   WB_RESTRICTION_WAVE_WINDOW    连坐判定的时间窗，秒（默认 900）
#   WB_RESTRICTION_HOLD_SECONDS   整池暂停时长，秒（默认 1800；0 = 关闭暂停）
def _env_number(name, default, cast=float):
    try:
        return max(0, cast(os.environ.get(name) or default))
    except (TypeError, ValueError):
        return default


RESTRICTED_403_ROTATE_MAX = int(_env_number("WB_RESTRICTED_403_ROTATE_MAX", 0, int))
RESTRICTION_WAVE_MIN = max(1, int(_env_number("WB_RESTRICTION_WAVE_MIN", 2, int)))
RESTRICTION_WAVE_WINDOW = _env_number("WB_RESTRICTION_WAVE_WINDOW", 900.0)
RESTRICTION_HOLD_SECONDS = _env_number("WB_RESTRICTION_HOLD_SECONDS", 1800.0)

_RESTRICTION_LOCK = threading.Lock()
_RESTRICTION_EVENTS = {}   # realm -> {uid: epoch}（窗口内被限制的账号）
_RESTRICTION_HOLD = {}     # realm -> 暂停到哪个 epoch


def reset_restriction_state():
    """清空连坐保护状态（测试，以及人工确认「已申诉、可以恢复派发」时用）。"""
    with _RESTRICTION_LOCK:
        _RESTRICTION_EVENTS.clear()
        _RESTRICTION_HOLD.clear()


def restriction_hold_remaining(realm, now=None):
    """该区域整池暂停还剩多少秒（0 = 没有暂停）。"""
    now = time.time() if now is None else now
    with _RESTRICTION_LOCK:
        return max(0.0, _RESTRICTION_HOLD.get(realm, 0.0) - now)


def note_account_restriction(realm, uid="", now=None):
    """记一次账号级风控限制；返回整池暂停的到期 epoch（0 = 尚未触发）。

    同一窗口内达到 RESTRICTION_WAVE_MIN 个**不同**账号被限制，就认为上游在
    按设备 / 出口做连坐清扫：把整个区域暂停 RESTRICTION_HOLD_SECONDS，期间
    不再向上游发任何请求（open_upstream 直接快速失败），等人工确认或时间到
    再恢复。按 uid 去重，所以同一个号被反复探测 / 测试不会把暂停骗出来。
    """
    now = time.time() if now is None else now
    uid = str(uid or "").strip() or "-"
    with _RESTRICTION_LOCK:
        events = {key: at for key, at in _RESTRICTION_EVENTS.get(realm, {}).items()
                  if now - at <= RESTRICTION_WAVE_WINDOW}
        events[uid] = now
        _RESTRICTION_EVENTS[realm] = events
        if len(events) < RESTRICTION_WAVE_MIN or RESTRICTION_HOLD_SECONDS <= 0:
            return 0.0
        until = max(_RESTRICTION_HOLD.get(realm, 0.0),
                    now + RESTRICTION_HOLD_SECONDS)
        _RESTRICTION_HOLD[realm] = until
    log("upstream restriction wave on %s: %d account(s) restricted within %.0fs; "
        "pausing dispatch for %.0fs to avoid cross-account bans (uid %s)"
        % (realm, len(events), RESTRICTION_WAVE_WINDOW,
           RESTRICTION_HOLD_SECONDS, uid[:8]))
    return until


def restriction_hold_message(realm, remaining):
    """整池暂停期间交给客户端的说明。"""
    return ("上游风控已限制本区域账号（403 / code 11140，账号存在异常行为）。"
            "网关已暂停派发 %d 秒，避免继续换号重试导致同设备账号连坐被封。"
            "请勿换号或高频重试；按 WorkBuddy 官方渠道申诉（通常 1-3 个工作日"
            "自动解除），解封后再恢复。 Upstream restricted the account(s); "
            "dispatch paused %ds to avoid further bans."
            % (int(remaining), int(remaining)))


def _persist_restriction(account):
    """把 restrictedUntil 落盘；失败只记日志，不影响请求路径。"""
    try:
        path = getattr(account, "path", "") or ""
        if path:
            account.save(os.path.dirname(path))
    except Exception as exc:
        log("account %s: restriction not persisted: %s"
            % ((getattr(account, "uid", "") or "?")[:8], exc))

#: 识别风控限制 403 的正文特征。内容审核的 403 走降级提示词那条路，二者必须
#: 分开：对被限制的账号换提示词没有意义。code 匹配用正则（requestId 是 hex，
#: 裸字符串 "11140" 可能撞上），措辞只收实测的限制用语，不猜。
_RESTRICTED_403_CODE_RE = re.compile(r'"code"\s*:\s*"?11140"?')
_RESTRICTION_403_MARKERS = ("unusual activity", "账号存在异常行为", "已被限制")


def restriction_is_account_level(detail):
    """True when a 403 body says the ACCOUNT is restricted, not the content.

    实测正文：{"code":11140,"msg":"request illegal",...,"displayMsg":{"en":
    "Your account has been restricted due to unusual activity","zh":"账号存在
    异常行为，已被限制使用"}}。en 在前 zh 在后，短截断也保得住 code 与 en；
    拿不全的正文宁可漏判回内容审核路径，也不误判。
    """
    if not detail:
        return False
    if _RESTRICTED_403_CODE_RE.search(detail):
        return True
    lowered = detail.lower()
    return any(marker in lowered for marker in _RESTRICTION_403_MARKERS)


def _unescape_body(detail):
    """还原 JSON 转义过的响应体（`\\u5c06\\u5728` -> `将在`）。

    线上上游返回的是原始 UTF-8 中文，但中间层或某些网关会把它转成
    json.dumps 的默认转义形式。只在确实出现 `\\u` 时还原：本来就正常的
    UTF-8 中文经 unicode_escape 会被解成乱码，那比不还原更糟。
    """
    if "\\u" not in detail:
        return detail
    try:
        return detail.encode("utf-8", "surrogatepass").decode("unicode_escape")
    except Exception:
        return detail


def parse_rate_limit_reset(detail):
    """Pull the reset time out of an upstream 429 body, if it names one.

    Upstream answers code 6004 with one of two shapes:
        "... your usage will reset at 2026-09-19 18:29:03 UTC+8 ..."
        "您的使用量已超出频率限制，将在 2026-10-02 16:00:34 UTC+8 重置 ..."
    Matching only the English phrasing left every cn 429 without a reset
    time, so it read as an account-level soft limit and cooled the whole
    credential instead of parking just the throttled model - and left the
    window probe (wb_window_probe) with no reset instant to schedule against.
    Both shapes are accepted here.

    The stamp is wall-clock time in the body's own UTC offset, so it is
    converted with timegm: mktime would interpret it in the *host* timezone
    and land hours off on any machine that is not already at UTC+8.

    Returns an epoch or None. Kept tolerant on purpose: an unparseable body
    must not break the request path.
    """
    if not detail:
        return None
    text = _unescape_body(detail)
    m = re.search(r"(?:reset at|将在)\s*(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", text)
    if not m:
        # 措辞不认识（本地化文案、转义残留）时的兜底：上游固定把时间戳写成
        # 「YYYY-MM-DD HH:MM:SS UTC±N」，认这个搭配比认关键词稳。
        m = re.search(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})\s*UTC[+-]\d{1,2}", text)
    if not m:
        return None
    stamp = m.group(1).replace("T", " ")
    tz = re.search(r"UTC([+-]\d{1,2})(?::?(\d{2}))?", text)
    offset = 0
    if tz:
        hours = int(tz.group(1))
        minutes = int(tz.group(2) or 0)
        offset = hours * 3600 + (minutes * 60 if hours >= 0 else -minutes * 60)
    try:
        base = calendar.timegm(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
        return base - offset
    except Exception:
        return None


def upstream_timeouts():
    """(header, idle) seconds for the chat upstream socket.

    header covers connect/TLS/first byte; idle bounds each mid-stream
    read, so active data keeps the connection alive while a silent
    stream fails out and releases its in-flight lease. There is no
    total-duration cap, matching the panel project's semantics.
    """
    try:
        cfg = wb_settings.upstream_config(ACCOUNTS_DIR)
    except Exception:
        cfg = None
    header = 120.0
    idle = 300.0
    if isinstance(cfg, dict):
        try:
            header = float(cfg.get("header_timeout_seconds") or header)
        except (TypeError, ValueError):
            pass
        try:
            idle = float(cfg.get("idle_timeout_seconds") or idle)
        except (TypeError, ValueError):
            pass
    return max(1.0, header), max(1.0, idle)


def _apply_stream_idle_timeout(response, seconds):
    """Switch the upstream socket to the mid-stream idle timeout.

    urllib's timeout covers the initial request; the same socket is then
    read for the whole SSE stream. Setting the socket timeout to the idle
    value bounds each read instead of the whole stream. Best-effort: an
    unknown socket shape keeps the header timeout.
    """
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return False
    if seconds <= 0:
        return False
    fp = getattr(response, "fp", None)
    raw = getattr(fp, "raw", None)
    sock = getattr(raw, "_sock", None)
    if sock is None:
        sock = getattr(fp, "_sock", None)
    if sock is None:
        return False
    try:
        sock.settimeout(seconds)
        return True
    except Exception:
        return False


def schedule_window_probe(account, model, reset_at):
    """把上游给出的重置时刻登记给窗口探针（wb_window_probe）。

    上游的额度窗口按首次请求起算，到点无人请求就不会开始新一轮，所以这个
    时刻要在后台被主动用掉，而不是只当作一段冷却。登记本身不能影响请求
    路径：任何异常都只写日志，本次 429 照常换号重试。
    """
    if not reset_at or account is None or not model:
        return False
    # 以池子的目录为准：--accounts-dir / Docker 卷会把它指到别处，而
    # ACCOUNTS_DIR 只是启动默认值。登记目录必须和 run_due() 读的目录一致，
    # 否则窗口登记在一处、执行在另一处，探针永远不触发。
    accounts_dir = getattr(POOL, "dir", "") or ACCOUNTS_DIR
    try:
        import wb_window_probe
        return wb_window_probe.schedule(accounts_dir, account.uid, model, reset_at)
    except Exception as exc:
        log("window probe schedule failed: %s" % exc)
        return False


_FREE_MODEL_CREDIT_FALLBACKS = {
    ("intl", "deepseek-v4.1-flash"): "deepseek-v4.1-flash-sg",
}


def _free_model_credit_fallback_enabled():
    if wb_settings.free_model_credit_fallback(ACCOUNTS_DIR):
        return True
    return os.environ.get("WB_ENABLE_FREE_MODEL_CREDIT_FALLBACK", "0").strip().lower() in (
        "1", "true", "yes", "on")


def open_upstream(payload, session_key=None, target_realm=None, prebuilt_body=None,
                  allow_credit_fallback=True, allow_product_switch=True,
                  pin_account=None, defer_egress_success=False,
                  allowed_accounts=None):
    # Refresh the daily guards before picking. The scan underneath is
    # incremental and TTL-cached, so this is a stat() plus a cached dict on
    # the hot path, and an account parked by any of the guards is skipped
    # like any other unusable one. One call covers all three guards: they
    # share the same fold, and calling them separately paid for it three
    # times on every request.
    apply_daily_guards()
    # 剩余用量优先调度（开关默认关）：把「估计剩余接近耗尽」的 (账号, 模型)
    # 权重推给池，让它们先接单。只在开关打开时读剩余估算（载荷有短 TTL，热路径
    # 上是缓存命中）；关着时推空表，把上一次可能留下的权重清掉，下一个请求就
    # 回到与从前逐字节一致的纯轮询。
    if POOL is not None:
        POOL.apply_remaining_weights(
            remaining_schedule_weights() if remaining_priority_enabled() else None)
    realm = target_realm or detect_model_realm(payload.get("model")) or CURRENT_REALM
    model = str(payload.get("model") or "")
    pinned = pin_account if pin_account is not None else pinned_account()
    if pinned is not None:
        if allowed_accounts is not None and pinned.uid not in allowed_accounts:
            raise PinnedAccountError(
                403, "pinned account '%s' is not allowed by this API key"
                % pinned_ref(), pinned.uid)
        if pinned.realm != realm:
            raise PinnedAccountError(
                409, "pinned account '%s' belongs to realm %s, request targets %s"
                % (pinned_ref(), pinned.realm, realm), pinned.uid)
        if not pinned.ready(model=model):
            raise PinnedAccountError(
                503, "pinned account '%s' is not usable right now: %s"
                % (pinned_ref(), pinned.unavailable_reason(model)), pinned.uid)
    # 连坐保护：同一区域刚有账号被上游风控限制，整池暂停派发。这里直接快速
    # 失败、一个上游请求都不发——继续打只会把同一台设备上的其他账号也送走。
    hold = restriction_hold_remaining(realm)
    if hold > 0:
        raise RateLimited(None, "upstream account restriction in effect",
                          wait=int(hold) + 1,
                          message=restriction_hold_message(realm, hold))
    # 复用调用方已建好的 body：/v1/chat/completions 在进这里之前已经 build 过
    # 一次（那次的产物只用于一行日志和 prompt_fingerprint），此前这里会为同一
    # 个 payload 再建一遍，大请求实测白付 ~13.6ms/请求。403 降级重试时仍按
    # payload 重建（degrade 会切换 prompt 模式，必须拿到新 body）。
    upstream_body = (prebuilt_body if prebuilt_body is not None
                     else build_upstream_body(payload))
    # PATCHED-BY-OPS: 客户端未提供会话标识时，用对话稳定前缀兜底。
    # 位置放在 build_upstream_body 之后，保证键与真正发往上游的消息一致
    # （该函数可能在最前面插入 SYSTEM_PROMPT）。affinity_page 只在超限对话上
    # 有值（页内轮转），客户端自带会话键的请求不参与。
    affinity_page = None
    if not session_key:
        session_key, affinity_page = affinity_route(upstream_body.get("messages"))
        if session_key and AFFINITY_DEBUG:
            log("affinity: derived %s for %d msgs"
                % (session_key, len(upstream_body.get("messages") or [])))
    # 一轮 user send 的 request id：官方 CLI 在这一轮的所有请求（含重试/换号）
    # 复用同一个 X-Conversation-Request-ID / X-Root-Request-ID，只有 message id
    # 每请求新生成（见 wb_identity.new_turn_request_id）。生成在重试循环外，
    # 循环里只把它传给身份构造；桌面端 / VSCode 分支不消费它，行为不变。
    turn_request_id = wb_identity.new_turn_request_id()
    # Only pass `allowed` when the request actually carries a restriction: a
    # stub pool in tests (and any external pool implementation) keeps its old
    # count_ready signature for the unrestricted path.
    if POOL is None:
        ready_total = 1
    elif allowed_accounts is not None:
        ready_total = POOL.count_ready(realm, model=model,
                                       allowed=allowed_accounts)
    else:
        ready_total = POOL.count_ready(realm, model=model)
    total = 1 if pinned is not None else max(1, ready_total)
    tried = set()
    # 这一轮请求已经记过账的账号：重试轮（transient_hits 后 tried.clear() 整池
    # 重轮）会把同一个账号再挑一遍，若每次都记，一次请求就能给同一个账号记上
    # 好几笔 fails/degrade——池子越空、重轮越频繁，越容易把一个刚抖了一下的
    # 账号直接推过熔断阈值。一次请求对一个账号最多记一次。
    charged = set()
    # A 401 may simply mean the access token expired before its JWT clock did.
    # Refresh at most once per account per request, then retry that same
    # account instead of silently rotating away from a recoverable credential.
    refreshed = set()
    refresh_uid = None

    def pool_account(uid):
        """Look up one account by uid; test pools only expose an accounts list."""
        if POOL is None:
            return None
        getter = getattr(POOL, "get", None)
        if getter is not None:
            return getter(uid)
        for candidate in getattr(POOL, "accounts", ()) or ():
            if candidate.uid == uid:
                return candidate
        return None

    def charge_account(account, message, unknown):
        """给账号记一笔失败，一次请求最多记一笔（见上面的 charged）。"""
        if account.uid in charged:
            return False
        charged.add(account.uid)
        if unknown:
            account.note_unknown_failure(message)
        else:
            account.note_failure(message)
        return True

    last_error = None
    last_uid = None
    last_429 = None
    last_429_detail = ""
    last_403_detail = ""
    transient_hits = 0
    degraded_retried = False
    # 一次请求里因为 11140 换过的号数：默认 0，即第一个号被限制就停手。
    restricted_rotations = 0
    # 本机断网的次数：这一轮里只要出现过，最终报错就按「网络不可用」报，不按
    # 「上游不稳」报——两者的处置完全相反（去查本机网络 vs 去查账号/上游）。
    local_net_hits = 0
    # Read once per request, not per attempt: this is a panel setting, and a
    # settings read on every retry would be pure overhead.
    auto_switch = (auto_switch_product_enabled()
                   if allow_product_switch and pinned is None else False)
    # Panel parity: header/idle timeouts are read once per request, not
    # once per retry, so a settings read never lands in the retry loop.
    header_timeout, idle_timeout = upstream_timeouts()
    # Same treatment for the egress-level backoff switch (issue #271): one read
    # per request, and when it is off the retry loop below is byte-for-byte the
    # old path.
    egress_backoff = wb_settings.egress_backoff_enabled(ACCOUNTS_DIR)
    # 同一段出口在一次请求里只记一次：重试轮（tried.clear() 之后整池重轮）会把
    # 同一个账号 / 同一段出口再挑一遍，若每次都记，一次请求就能把共享计数器推高
    # 好几步——那正是 issue #271 里「一次请求给同一账号记好几笔」的另一半。已经
    # 记过的那一段沿用同一段退避，不重复记。
    egress_wait = {}

    def charge_egress(url, proxy):
        key = wb_upstream_pool.egress_key(url, proxy)
        if key not in egress_wait:
            egress_wait[key] = wb_egress.note_transient(key)
        return egress_wait[key]

    max_attempts = (1 if pinned is not None else
                    max(2, total) + 1 + (MAX_PRODUCT_SWITCHES if auto_switch else 0))
    # 官方 VSCode 扩展的 X-Conversation-ID 是 32hex、会话稳定；网关没有会话
    # 映射，至少保证同一次 open_upstream（含换号重试）里是同一个 id。桌面端 /
    # CLI 分支保持原行为（每次尝试各自新生成带连字符的 UUID），不受影响。
    vscode_conversation_id = wb_identity.new_message_id()
    # 每次失败都把【出错的那个账号】从这段对话的历史里降级（demote），而不是
    # 忘掉整段对话：重选时优先回到还留着这段前缀的暖号（见 SessionAffinity），
    # 一次大 prompt 的冷启动比重选一次账号贵得多。
    # 账号级并发上限（可选，默认关）用到的「名额已满」账号。它与 tried 分开：
    # tried 记的是「这一轮试过且失败」的账号，busy 只是「此刻没空」，等它的请求
    # 结束后这个账号完全可用，不该被当成失败账号影响熔断或亲和。
    busy = set()
    # 当前这一轮已经占住、但还没交给调用方的账号名额。任何失败路径都要归还
    # （循环开头统一处理），成功路径把它绑到响应对象上后置空。
    slot_held = None
    def pick_account():
        """按当前 exclude/busy 选一个账号，并占住它的在飞名额。

        名额上限关闭时 account_slot_acquire 恒真且不记账，这里与改动前完全等价。
        """
        nonlocal busy
        while True:
            exclude = tried | busy
            kwargs = dict(realm=realm, session_key=session_key,
                          exclude=exclude, model=model, page=affinity_page)
            if allowed_accounts is not None:
                kwargs["allowed"] = allowed_accounts
            candidate = POOL.pick_for_session(**kwargs) if POOL else None
            if candidate is None:
                return None
            if account_slot_acquire(candidate):
                return candidate
            busy.add(candidate.uid)
            if len(busy) >= max(1, total):
                # 池子里每个可用账号都满了：不再空转，交给上层排队/报错。
                return None

    attempt = 0
    while True:
        attempt += 1
        if attempt > max_attempts:
            break
        # 上一轮拿到的名额如果没被交出去（任何 continue/break 都意味着这次尝试
        # 没把响应交给调用方），在这里统一归还。成功路径会在 return 前把它绑到
        # 响应对象上并清空这个标记，所以只有失败路径会走到这里。
        if slot_held is not None:
            account_slot_release(slot_held)
            slot_held = None
        if pinned is not None:
            account = pinned if pinned.uid not in tried else None
            # 固定账号（/u/<ref>/ 路由、流式中断后的原地重连）也要占名额，
            # 否则下一轮开头的统一归还就会释放一个从未占过的名额。固定账号没有
            # 「换一个」的余地，所以名额被占满时短暂等待，等不到就报池子忙。
            if account is not None:
                got = account_slot_acquire(account)
                waited = 0.0
                while not got and waited < ACCOUNT_SLOT_WAIT_MAX:
                    time.sleep(0.05)
                    waited += 0.05
                    got = account_slot_acquire(account)
                if not got:
                    account = None
        elif refresh_uid is not None:
            account = pool_account(refresh_uid)
            refresh_uid = None
            # 令牌刷新后的原地重试同样是「固定账号」：占不到名额就等，等不到
            # 就放弃这一次尝试，不能凭空释放一个没占过的名额。
            if account is not None:
                got = account_slot_acquire(account)
                waited = 0.0
                while not got and waited < ACCOUNT_SLOT_WAIT_MAX:
                    time.sleep(0.05)
                    waited += 0.05
                    got = account_slot_acquire(account)
                if not got:
                    account = None
        else:
            account = pick_account()
        if (account is None and pinned is None
                and (transient_hits or local_net_hits)):
            # Pool rotation is bookkeeping, not an upstream attempt. Re-pick
            # in this iteration so a two-account pool keeps its third request.
            # Local DNS/route failures need the same bounded retry path.
            tried.clear()
            time.sleep(min(1.5 * (transient_hits or local_net_hits), 3.0))
            account = pick_account()
        if account is None:
            break
        if account.realm != realm:
            if session_key and POOL: POOL.affinity.demote(session_key, account.uid)
            account_slot_release(account)
            continue
        tried.add(account.uid)
        last_uid = account.uid
        slot_held = account
        cfg = wb_accounts.get_realm_config(account.realm)
        chat_url = account.chat_base_url() + CHAT_PATH
        # The cache key is account scoped, so it is rebuilt per candidate rather
        # than once up front. Opt-in only: measurement showed the upstream
        # caches prefixes without it (see prompt_cache_key_enabled).
        if prompt_cache_key_enabled():
            attempt_body = inject_prompt_cache_key(upstream_body, account.uid, session_key)
        else:
            attempt_body = upstream_body
        # 官方 CLI 的 chat 请求体（见 wb_identity）：gzip（RequestBodyGzip=true，
        # 带 Content-Encoding: gzip）+ 注入 cache_retention=long；Responses API
        # 的字段 / 客户端 metadata 不带。VSCode 官方主 chat 也默认 gzip
        # （RequestBodyGzip=true → executeByMode → createCustomFetch 压缩）。
        cli_identity = account.is_cli_identity()
        vscode_identity = account.product == wb_identity.PRODUCT_VSCODE
        if cli_identity:
            attempt_body = {k: v for k, v in attempt_body.items()
                            if k not in wb_identity.CLI_BODY_DROP_FIELDS}
            attempt_body["cache_retention"] = wb_identity.CLI_CACHE_RETENTION
        # 紧凑 JSON：官方客户端（Node JSON.stringify）不带分隔符后的空格，默认的
        # json.dumps(separators=(", ", ": ")) 会让 body 字节与 Content-Length 都成
        # 一处机器特征。上游解析等价，只影响线上字节。
        attempt_data = encode_upstream_body(attempt_body)
        if cli_identity or vscode_identity:
            attempt_data = wb_identity.gzip_body(attempt_data)
        # 会话头族（X-Conversation-ID / -Request-ID）的语义见 wb_identity：桌面端
        # 官方客户端同一会话复用同一个 id：桌面端 / CLI 按 (账号, 会话键) 稳定
        # 派生（#448），客户端没给会话键时保持每请求新生成；VSCode 按官方
        # BaseAgent 的 32hex 形态每请求新生成，并带 X-Model-ID。
        # 缺了这套头上游的渠道校验会回 400 / code 11-128（conversation id
        # missing）。跨账号重试时按账号各自派生，不会把两个号串到同一个会话上。
        # 重试换号时 session_key / turn_request_id / vscode_conversation_id 都不变。
        if account.product == wb_identity.PRODUCT_VSCODE:
            conv_id = vscode_conversation_id
        elif session_key:
            conv_id = wb_identity.conversation_id_for(account.uid, session_key)
        else:
            conv_id = None
        req_headers = account.headers(
            purpose="chat", session_key=session_key,
            turn_request_id=turn_request_id, model=model,
            conversation_id=conv_id)
        if cli_identity or vscode_identity:
            req_headers["Content-Encoding"] = wb_identity.REQUEST_CONTENT_ENCODING
        req = urllib.request.Request(chat_url, data=attempt_data, method="POST",
                                     headers=req_headers)
        req.wb_exact_headers = list(req_headers.items())
        try:
            resp = wb_accounts.urlopen(req, timeout=header_timeout,
                                       proxy=account.proxy)
            _apply_stream_idle_timeout(resp, idle_timeout)
            account.note_success(model=model)
            if egress_backoff and not defer_egress_success:
                # This outbound path just served a request, so its transient
                # streak is over (same rule as the account's note_success).
                # Callers that read the body afterwards defer this to the end
                # of the read: a stream that dies mid-body is not a success.
                wb_egress.note_success(
                    wb_upstream_pool.egress_key(chat_url, account.proxy))
            reset_switch_counter(account, model)
            # 名额交给响应对象：它一直占到上游 body 读完/关掉为止（流式回复拿到
            # 头之后还要读几十秒，那段时间上游仍算这个账号在跑）。绑定后本地
            # 标记置空，循环开头就不会再重复归还。
            bind_account_slot_to_response(resp, account)
            slot_held = None
            # The third element is the reasoning effort this request ran at: the
            # body is rebuilt per attempt, but the effort is a property of the
            # model and the request, and the callers record it on the usage row.
            return resp, account, upstream_effort_of(upstream_body, model)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                try:
                    detail = read_http_error(exc, 600).decode("utf-8", "replace")
                except Exception:
                    detail = ""
                reset_at = parse_rate_limit_reset(detail)
                if rate_limit_is_account_level(detail, reset_at):
                    wait = account.note_soft_rate("HTTP 429 (account soft rate)")
                    log("account %s soft-rate limited (%r), cooling %.0fs (streak %d)"
                        % (account.uid[:8], credential_scope_phrase(detail), wait,
                           account.soft_streak))
                    if session_key and POOL:
                        POOL.affinity.demote(session_key, account.uid)
                    last_error = exc
                    last_429 = exc
                    last_429_detail = detail
                    continue
                # No reset clock, so nothing here says whether the limit is on
                # the credential or on one model. For a recognised soft-limit
                # shape the repetition is the evidence: the window goes on this
                # model and grows on the short ladder (60s up to 10m) instead of
                # the credential's 600s -> 2h one. An unrecognised body gets the
                # same 60s on one model, with no streak at all.
                wait = max(1.0, reset_at - time.time()) if reset_at else 60.0
                if reset_at is None and rate_limit_is_soft_shape(detail):
                    wait = account.note_unscoped_rate(model)
                    log("account %s: unscoped 429, parking '%s' for %.0fs "
                        "(unscoped streak %d)"
                        % (account.uid[:8], model, wait, account.unscoped_streak))
                # Model-scoped: only this model is throttled for this account,
                # so sibling models stay serviceable on the same credential.
                account.note_error("HTTP 429 (model throttled)", model=model, until=reset_at,
                                   cooldown=wait, detail=detail)
                # 除了记冷却，还要把重置时刻交给后台探针：到点主动发一个最小
                # 请求，把新一轮窗口的计时起点钉在那一刻（窗口按首次请求起算）。
                schedule_window_probe(account, model, reset_at)
                if reset_at:
                    # A cap with a reset clock is the one observable moment the
                    # window budget can be measured from; the usage rows alone
                    # lose it (only the last account of a retry batch keeps its
                    # attribution). See note_limit_event().
                    note_limit_event(account, model, reset_at)
                    # 「达到限额」的唯一标志是上游返回 6004（用户口径）：别的
                    # 429 即使带重置墙钟也不构成额度判定，不记 cap、不写盘。
                    # 记录范围（免费模型）由 note_model_cap 把守——范围外的
                    # 模型返回 False，重启后自动放开，不参与持久化禁用。
                    if rate_limit_is_cap(detail):
                        try:
                            if account.note_model_cap(model, reset_at):
                                _save_account_caps(account, model, reset_at)
                        except Exception as exc:
                            log("account %s: model cap not persisted: %s"
                                % (account.uid[:8], exc))
                if auto_switch and _try_switch_product(account, model):
                    # 换了身分就等于换了一条配额线：要把它从「已试过」拿掉，
                    # 并清掉刚刚记下的模型冷却，否则下一轮回圈会找不到帐号。
                    # keep_caps：6004 的禁用（仍在恢复期内）跨身分成立，不能
                    # 被这次清理带走——真机实测它会让 6004 之后账号立刻又可用。
                    tried.discard(account.uid)
                    try:
                        account.clear_error(model=model, keep_caps=True)
                    except Exception:
                        pass
                    if session_key and POOL:
                        POOL.affinity.demote(session_key, account.uid)
                    continue
                log("account %s throttled on '%s' (429), retry in %ds"
                    % (account.uid[:8], model, int(wait)))
                if session_key and POOL:
                    POOL.affinity.demote(session_key, account.uid)
                last_error = exc
                last_429 = exc
                last_429_detail = detail
                continue
            if exc.code == 403:
                try:
                    detail = read_http_error(exc, 600).decode("utf-8", "replace")
                except Exception:
                    detail = ""
                # 先分流：上游风控限制（code 11140「账号存在异常行为，已被限制
                # 使用」）是账号级的坏，降级提示词救不了。处置按社区复盘的红线
                # 走：冷却这个账号（持久化）、记一次连坐事件，然后**停手**——
                # 不再把同一条请求换到下一个号上继续打（默认，见
                # RESTRICTED_403_ROTATE_MAX）。
                if restriction_is_account_level(detail):
                    account.note_restricted(detail=detail,
                                            cooldown=RESTRICTED_403_COOLDOWN)
                    _persist_restriction(account)
                    log("account %s restricted by upstream (403 11140), "
                        "cooling %.0fs (persisted)" % (account.uid[:8],
                                                       RESTRICTED_403_COOLDOWN))
                    if session_key and POOL:
                        POOL.affinity.demote(session_key, account.uid)
                    last_error = exc
                    last_403_detail = detail
                    note_account_restriction(realm, account.uid)
                    # 红线：解封前不换号继续打同一台设备。默认把原因交给客户端
                    # 就停手，下一次请求自然会落到别的账号；不再把同一条请求
                    # 原样复制到池里每个账号——那正是「一封全封」的放大器。
                    # 需要旧行为（透明换号）时把 WB_RESTRICTED_403_ROTATE_MAX
                    # 调大即可。
                    if restricted_rotations >= RESTRICTED_403_ROTATE_MAX:
                        break
                    restricted_rotations += 1
                    continue
                # Panel degrade.go: a content rejection in passthrough/append
                # mode is usually a system-prompt fingerprint false positive.
                # Switch to the minimal neutral prompt until next 00:00 CST
                # and retry this turn once; custom mode opted out.
                if (not degraded_retried
                        and prompt_mode_config().get("mode") in ("passthrough", "append")):
                    PROMPT_DEGRADE.trigger()
                    degraded_retried = True
                    upstream_body = build_upstream_body(payload)
                    tried.discard(account.uid)
                    log("upstream 403 (content review) -> degraded prompt retry")
                    continue
                log("upstream 403 for '%s' (content review), passing through" % model)
                if session_key and POOL:
                    POOL.affinity.demote(session_key, account.uid)
                last_error = exc
                last_403_detail = detail
                break
            if exc.code == 402:
                try:
                    detail = read_http_error(exc, 400).decode("utf-8", "replace")
                except Exception:
                    detail = ""
                account.note_balance_cooled(detail or "HTTP 402 (insufficient credits)")
                log("account %s out of credits (402), parked until 04:00"
                    % account.uid[:8])
                if session_key and POOL:
                    POOL.affinity.demote(session_key, account.uid)
                last_error = exc
                continue
            if exc.code == 401:
                if session_key and POOL:
                    POOL.affinity.demote(session_key, account.uid)
                if account.uid not in refreshed:
                    refreshed.add(account.uid)
                    try:
                        refreshed_ok = account.refresh()
                    except Exception as refresh_exc:
                        refreshed_ok = False
                        log("account %s: token refresh after 401 failed: %s"
                            % (account.uid[:8], exc_detail(refresh_exc, 160)))
                    if refreshed_ok:
                        log("account %s: HTTP 401, token refreshed; retrying the same account"
                            % account.uid[:8])
                        tried.discard(account.uid)
                        refresh_uid = account.uid
                        last_error = exc
                        continue
                log("account %s rejected (HTTP 401), rotating" % account.uid[:8])
                account.note_error("HTTP 401",
                                   cooldown=60,
                                   single_account=(total <= 1))
                last_error = exc
                continue
            if exc.code == 400:
                # A 400 is passed through unchanged. `11158` (model not allowed
                # for this user type) used to be recorded here for the model
                # catalogue label; the tier map from `/v3/config` now drives
                # both the scheduler and that label, so the request never gets
                # this far for a gated model.
                raise
            if exc.code >= 500:
                detail = http_error_detail(exc)
                code = upstream_error_code(detail)
                if (code in MODEL_SCOPED_ERROR_CODES
                        and not model_scoped_errors_penalize_account()):
                    # A bad model name must not take the whole pool down. The
                    # body says this is the model/service, not the credential;
                    # stop rotating and hand the upstream body back unchanged.
                    log("upstream %s for '%s' (code %s, model-scoped); "
                        "not charging the account"
                        % (exc.code, model, code))
                    last_error = exc
                    break
                # 一切服务端 5xx（含上游自定义的 550 这类）都按瞬时故障轮转，
                # 不在已知清单里的也不能把错误原样甩回客户端——换号是默认，
                # 全试完才交出去。
                transient_hits += 1
                if egress_backoff and exc.code in (502, 503, 504):
                    # Gateway-side blips (502/503/504) say the path is unhappy,
                    # not the credential: charge the egress and back off for
                    # seconds (issue #271). 500 stays on the account - it can be
                    # a real fault tied to this credential and needs another look
                    # before it is moved off the breaker.
                    wait = charge_egress(chat_url, account.proxy)
                    log("upstream %s for '%s', backing off the egress %.1fs"
                        % (exc.code, model, wait))
                    if session_key and POOL:
                        POOL.affinity.demote(session_key, account.uid)
                    last_error = exc
                    time.sleep(min(wait, EGRESS_SLEEP_MAX))
                    continue
                charge_account(account, "HTTP %d" % exc.code, unknown=False)
                log("upstream %s for '%s' (code %s), retrying (fails=%d)"
                    % (exc.code, model, code if code is not None else "-",
                       account.fails))
                if session_key and POOL:
                    POOL.affinity.demote(session_key, account.uid)
                last_error = exc
                continue
            raise
        except Exception as exc:
            if wb_accounts.is_tls_verify_error(exc):
                # One attempt is enough: the chain is the same for every
                # account, and retrying only multiplies the failed handshakes.
                last_error = exc
                break
            if session_key and POOL:
                POOL.affinity.demote(session_key, account.uid)
            if looks_like_local_network(exc):
                # 本机没网：不记账。账号没发出去一个字节，这笔不该算它的；
                # 断网期间每次都记的话，网络一恢复池子里全是冷却中的账号。
                # 也不能只 break——网络抖动几秒很常见，先原地重试几轮。
                local_net_hits += 1
                if local_net_hits == 1:
                    log("local network looks down (%s); not charging any account"
                        % exc_detail(exc))
                last_error = exc
                time.sleep(min(0.3 * local_net_hits, 1.5))
                continue
            if is_transient(exc):
                transient_hits += 1
                if egress_backoff:
                    # A transport blip is the path's problem, not the account's:
                    # charge the egress and back off for seconds (issue #271).
                    # The account's fails/degrade counters are left alone, so a
                    # few seconds of flakiness no longer parks healthy accounts.
                    wait = charge_egress(chat_url, account.proxy)
                    log("account %s: upstream connection hiccup for '%s' (%s), "
                        "backing off the egress %.1fs"
                        % (account.uid[:8], model, exc_detail(exc), wait))
                    last_error = exc
                    time.sleep(min(wait, EGRESS_SLEEP_MAX))
                    continue
                charge_account(account, "connection: %s" % exc_detail(exc, 120),
                               unknown=True)
                # 带上 UID：这一行同时是一次扣分（连续 3 次就把账号熔断 30
                # 分钟），不写清楚是谁挨的这一下，事后只能看着一串 hiccup 猜
                # 是哪几个账号被关掉、池子为什么突然空了。
                log("account %s: upstream connection hiccup for '%s' (%s), "
                    "retrying (degrade=%d)"
                    % (account.uid[:8], model, exc_detail(exc), account.degrade_count))
                last_error = exc
                time.sleep(min(0.6 * transient_hits, 2.0))
                continue
            charge_account(account, str(exc)[:120], unknown=True)
            account.note_error(str(exc)[:120], cooldown=60, single_account=(total <= 1))
            # 这一类失败以前是静默的：账号被记一笔 + 冷却 3/60 秒，日志里一个字
            # 都没有。于是「账号不断冷却、可面板测试又是绿的」在生产上只能靠猜
            # ——池子空了的报错只说得清「谁在冷却」，说不清「是谁把它记成冷却的」。
            # 把 uid、异常原文与刚设的冷却时长打出来，下次对上号就能直接定位。
            log("account %s: %s, cooling %.0fs (fails=%d, ready=%d)"
                % (account.uid[:8], exc_detail(exc, 160),
                   3.0 if total <= 1 else 60.0, account.fails, total))
            last_error = exc
            continue
    # 循环可能从 break 退出（TLS 校验失败、已知硬错误），这时本轮占的名额还在
    # 手上；成功路径已经在 return 前把它交给响应对象并置空，所以这里释放是安全的。
    if slot_held is not None:
        account_slot_release(slot_held)
        slot_held = None
    # Only spend credits after every enabled, credential-bearing account in
    # this realm has a persisted upstream 6004 cap for this free model. The
    # switch is opt-in because the retry can incur real charges.
    paid_model = _FREE_MODEL_CREDIT_FALLBACKS.get((realm, model))
    eligible = ([a for a in POOL.accounts
                 if a.realm == realm and a.enabled and a.access_token
                 and (allowed_accounts is None or a.uid in allowed_accounts)]
                if POOL else [])
    if (allow_credit_fallback and paid_model and _free_model_credit_fallback_enabled()
            and eligible and all(a.cap_until(model) > time.time() for a in eligible)):
        paid_payload = dict(payload)
        paid_payload["model"] = paid_model
        log("free model '%s' exhausted on all %d %s account(s); retrying paid model '%s'"
            % (model, len(eligible), realm, paid_model))
        response, account, effort = open_upstream(
            paid_payload, session_key=session_key, target_realm=realm,
            allow_credit_fallback=False, allow_product_switch=False,
            **({"allowed_accounts": allowed_accounts}
               if allowed_accounts is not None else {}))
        try:
            response._wb_billed_model = paid_model
        except Exception:
            pass
        return response, account, effort
    if last_error is not None:
        # Carry the account that produced the failure out to the caller, so
        # the error row can name it even though the local variable that would
        # have held it was never assigned in the caller.
        try:
            last_error.account_uid = last_uid
        except Exception:
            pass
        if wb_accounts.is_tls_verify_error(last_error):
            exc = wb_accounts.TLSVerifyError(
                wb_accounts.tls_verify_hint(last_error))
            exc.account_uid = last_uid
            raise exc
        if looks_like_local_network(last_error):
            # 最后一笔就是本机网络的问题：按「本机网络不可用」上报，不要报成
            # 「上游不可达」——后者会让人去查上游/账号，而真正要查的是本机。
            exc = LocalNetworkError(exc_detail(last_error))
            exc.account_uid = last_uid
            raise exc
        if last_429 is not None:
            exc = RateLimited(last_429, last_429_detail,
                              wait=retry_after_seconds(model, realm))
            exc.account_uid = last_uid
            raise exc
        if last_403_detail:
            exc = ContentRejected(last_error, last_403_detail)
            exc.account_uid = last_uid
            raise exc
        raise last_error
    throttled, wait = realm_model_throttled(realm, model)
    if throttled:
        raise RateLimited(None, "usage exceeds frequency limit", wait=wait)
    enabled = [a for a in POOL.accounts
               if a.realm == realm and a.enabled and a.access_token] if POOL else []
    if enabled and all(a.daily_limit_blocked() for a in enabled):
        reason = ("every usable account reached today's token limit (%s per "
                  "account); the pool resumes after local midnight"
                  % wb_settings.daily_token_limit(ACCOUNTS_DIR, realm))
        raise RateLimited(None, reason,
                          wait=seconds_until_local_midnight(), message=reason)
    if enabled and model and all(a.credit_limit_blocked(model) for a in enabled):
        reason = ("every usable account reached today's credit limit (%s per "
                  "account); paid models resume after local midnight, free "
                  "models keep working"
                  % wb_settings.daily_credit_limit(ACCOUNTS_DIR, realm))
        raise RateLimited(None, reason,
                          wait=seconds_until_local_midnight(), message=reason)
    if enabled and model and all(a.model_token_limit_blocked(model) for a in enabled):
        reason = ("every usable account reached today's token limit for %s "
                  "(%s per account); the model resumes after local midnight"
                  % (model, wb_settings.model_daily_token_limit(ACCOUNTS_DIR, realm)))
        raise RateLimited(None, reason,
                          wait=seconds_until_local_midnight(), message=reason)
    if busy and last_error is None:
        # 名额全满：账号本身完全健康，只是每个都在跑别的请求。必须报得比
        # 「全部停用/冷却/过期」清楚——否则排查的人会去看账号状态，而真正
        # 要做的只是等一两秒重发（或把上限调大）。
        limit = wb_settings.account_max_concurrent(ACCOUNTS_DIR)
        reason = ("every usable account is already serving %d concurrent "
                  "request(s) (account_max_concurrent=%d); retry shortly or "
                  "raise the cap" % (limit, limit))
        raise RateLimited(None, reason, wait=1, message=reason)
    raise RuntimeError("no usable account for realm '%s': all are disabled, cooling down, "
                       "expired or parked by the daily token limit%s"
                       % (realm, pool_unavailable_detail(realm, model)))


# ---- 跨出口回退：国际版 → 国内版（开关默认关）--------------------------------
#
# 两个出口是两套独立的上游（www.workbuddy.ai / copilot.tencent.com）、两套
# 配额、两套积分。派发路径一直把它们当两个互不相干的池子：realm 在请求入口
# 定死，AccountPool.pick() 只挑同 realm 的账号，跨 realm 的候选会直接跳过。
# 这条规则保证了「这个 Key 只花国际额度」这类部署，但代价是——国际版整池被
# 限流时，明明国内版还有号，请求也只会等 429。
#
# 下面这层是那个缺口的兜底，只在运营者显式打开时生效。

def realm_fallback_enabled():
    """持久化开关，与 remaining_priority_enabled() 同一形状。"""
    return wb_settings.realm_fallback_enabled(ACCOUNTS_DIR) is True


def realm_fallback_target(realm, model):
    """回退的目标 realm；返回 "" 表示这次请求不该回退。

    三个条件同时成立：
      * 当前走的是国际版出口 —— 单向，永不 cn→intl（国内号是免费的/便宜的一侧，
        把流量悄悄挪到国际出口会花掉运营者没打算花的额度）；
      * 模型两个出口都提供（`exclusive_realm` 为空）—— gpt-* / gemini-* 只有
        国际版有，换过去只会把一个 429 变成一个更难解释的 403；
      * 国内版此刻确实有账号能服务这个模型 —— 否则重试只是一次白跑的上游往返，
        还会把「限流」换成一个「没有可用账号」。
    """
    if realm != "intl":
        return ""
    if exclusive_realm(model):
        return ""
    if POOL is None:
        return ""
    try:
        if POOL.count_ready(realm="cn", model=model) <= 0:
            return ""
    except Exception:
        return ""
    return "cn"


def open_upstream_with_fallback(payload, session_key=None, target_realm=None,
                                allow_realm_fallback=True, **kwargs):
    """open_upstream()，外围加一次「国际版限流 → 国内版」的回退。

    只在两条失败上回退：
      * `RateLimited` —— 上游 429、24h 额度窗口用满（code 6004）、网关本地合成
        的「所有账号都在冷却该模型」。共同点是**这条出口的配额线暂时走不通**；
      * `no usable account` 的 RuntimeError —— 该出口的账号全部停用/过期/熔断，
        池子空了。

    别的失败照原样抛出。403 11140（账号被上游风控限制）尤其不能跨出口重打：
    项目对它有一条红线（见 RESTRICTED_403_ROTATE_MAX 附近），换出口继续打同一台
    设备正是它要防的那件事。pinned / 参数错误更不是换出口能修的。

    `allow_realm_fallback=False` 表示这次请求的出口是**显式指定**的（Key 绑定了
    realm，或客户端带了 X-Realm / ?realm=）：那是运营者写下来的意图，兜底不该
    越过它。
    """
    realm = target_realm or detect_model_realm(payload.get("model")) or CURRENT_REALM

    def _backup_or_raise(exc):
        if not allow_realm_fallback or not realm_fallback_enabled():
            raise exc
        backup = realm_fallback_target(realm, payload.get("model"))
        if not backup:
            raise exc
        detail = getattr(exc, "detail", "") or getattr(exc, "message", "") \
            or str(exc)
        log("realm fallback: %s cannot serve '%s' (%s), retrying on %s"
            % (realm, payload.get("model") or "-", detail[:120], backup))
        return backup

    try:
        return open_upstream(payload, session_key=session_key,
                             target_realm=target_realm, **kwargs)
    except RateLimited as exc:
        backup = _backup_or_raise(exc)
    except RuntimeError as exc:
        if not str(exc).startswith("no usable account"):
            raise
        backup = _backup_or_raise(exc)
    # 第二次尝试的失败原样抛出：两边都限流时，客户端该等多久由**国内版**给出的
    # 重置时钟决定，那比国际版那条更接近「下一次能成」的时刻。
    return open_upstream(payload, session_key=session_key,
                         target_realm=backup, **kwargs)

def pool_unavailable_detail(realm, model=None):
    """把该区域每个账号不可用的原因拼成一句，附在「没有可用账号」后面。

    这句话以前只列了四种可能（停用 / 冷却 / 过期 / 日限额），而 v1.6.16 之后
    账号还会被熔断与降权窗口挡住——恰好这两种一个字都没提。于是生产上出现过
    一场很难解释的报错：上游成片丢连接，几个账号各连踩 3 次触发 30 分钟熔断，
    池子瞬间空掉、请求 11ms 就回 503，而看板上那些账号仍然显示「可用」，
    排查的人只能对着「明明有 9 个账号」的池子猜。

    这里不改变判定，只把判定结果说出来：每个不可用账号「八位 UID + 原因」。
    """
    if POOL is None:
        return ""
    accounts = [a for a in POOL.accounts if a.realm == realm]
    if not accounts:
        return "；该区域没有任何账号"
    reasons = []
    for account in accounts:
        try:
            reason = account.unavailable_reason(model=model)
        except Exception:
            reason = ""
        if reason:
            reasons.append("%s %s" % ((account.uid or "?")[:8], reason))
    if not reasons:
        return ""
    return "；各账号：%s" % "，".join(reasons)

#: 「测试」默认用哪条模型：最便宜、任何区域都有的那条。
TEST_MODEL_DEFAULT = "deepseek-v4.1-flash"
#: 一键测试的并发度：每个探针都是一次真实的上游请求，别把池子打成压测。
PROBE_CONCURRENCY = 4

def _is_public_addr(text):
    """是不是一个可以直连的公网地址（拒绝环回/私有/链路本地/保留段）。"""
    try:
        addr = ipaddress.ip_address(str(text).split("%")[0])
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified)

def probe_local_network(realm):
    """本机到该区域上游这条路通不通：DNS 解析 + TCP 握手。

    一键测试里最容易被误判的就是这一项：本机一断网，每个账号都会失败，用户看到的
    却是「账号全挂了」，然后越测越乱。先把这条路单独确认掉，再谈账号。

    目标是各区域写死的上游主机（不是用户给的 URL），并且只接受 https、拒绝解析到
    环回/私有段的结果——配置被改坏时宁可报错，也不去连一个不该连的地址。
    """
    url = str(wb_accounts.get_realm_config(realm).get("chat_upstream") or "")
    info = {"realm": realm, "url": url, "host": "", "ip": "", "ok": False,
            "dns_ms": None, "tcp_ms": None, "error": ""}
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        info["error"] = "上游地址必须是 https + 主机名：%s" % url
        return info
    host = parsed.hostname
    port = parsed.port or 443
    info["host"] = host
    t0 = time.time()
    try:
        addrinfos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except Exception as exc:
        info["error"] = "DNS 解析失败：%s" % exc_detail(exc)
        return info
    info["dns_ms"] = int((time.time() - t0) * 1000)
    for family, socktype, proto, _canon, sockaddr in addrinfos:
        if not _is_public_addr(sockaddr[0]):
            info["error"] = "解析到非公网地址 %s，已拒绝连接" % sockaddr[0]
            return info
        info["ip"] = str(sockaddr[0])
        t1 = time.time()
        try:
            with socket.socket(family, socktype, proto) as sock:
                sock.settimeout(5)
                sock.connect(sockaddr)
        except Exception as exc:
            info["error"] = "TCP 连接失败：%s" % exc_detail(exc)
            return info
        info["tcp_ms"] = int((time.time() - t1) * 1000)
        info["ok"] = True
        return info
    info["error"] = "没有可用的解析结果"
    return info

def probe_account(account, model=None, timeout=30):
    """给一个账号发一条最小请求，返回一行结果；成功即清掉它的本地惩罚。

    单个「测试」与「一键测试」共用这一份：两边各写一套的话，早晚一边忘了清计数、
    另一边忘了认本机断网——而这两件事正是「明明测了能用，它还熔断」的全部来源。
    """
    model = model or TEST_MODEL_DEFAULT
    row = {"uid": account.uid, "nickname": account.nickname or account.uid[:8],
           "realm": account.realm, "model": model, "ok": False, "elapsed_ms": 0,
           "status": None, "reply": "", "error": "", "network": False}
    chat_url = account.chat_base_url() + CHAT_PATH
    test_body = {"model": model, "messages": [{"role": "user", "content": "hi"}],
                 "stream": False}
    probe_body = build_upstream_body(test_body)
    cli_identity = account.is_cli_identity()
    vscode_identity = account.product == wb_identity.PRODUCT_VSCODE
    if cli_identity:
        probe_body = {k: v for k, v in probe_body.items()
                      if k not in wb_identity.CLI_BODY_DROP_FIELDS}
        probe_body["cache_retention"] = wb_identity.CLI_CACHE_RETENTION
    body = encode_upstream_body(probe_body)
    if cli_identity or vscode_identity:
        body = wb_identity.gzip_body(body)
    # 与 open_upstream 同一条出站形态：VSCode 身分的 model 请求带 X-Model-ID；
    # CLI / VSCode 官方主 chat 都默认 gzip。
    probe_headers = account.headers(purpose="chat", model=model)
    if cli_identity or vscode_identity:
        probe_headers["Content-Encoding"] = wb_identity.REQUEST_CONTENT_ENCODING
    req = urllib.request.Request(chat_url, data=body, method="POST",
                                 headers=probe_headers)
    req.wb_exact_headers = list(probe_headers.items())
    t0 = time.time()
    try:
        with wb_accounts.urlopen(req, timeout=timeout, proxy=account.proxy) as resp:
            chat_obj = aggregate_stream(resp, model, None)
            row["elapsed_ms"] = int((time.time() - t0) * 1000)
            choices = chat_obj.get("choices") or []
            msg = (choices[0].get("message") or {}) if choices else {}
            reply = (msg.get("content") or msg.get("reasoning_content") or "OK").strip()
            row["reply"] = reply[:77] + "..." if len(reply) > 80 else reply
            # A served request, so it takes the semantic success path. That clears
            # the account-level streak/breaker/degrade counters: a green panel test
            # used to leave soft_streak standing, and the next 429 resumed the
            # ladder from it. Only the tested model's cooldown is dropped here -
            # sibling models keep theirs, and a 402 balance park keeps its own lift
            # path (revive_balance_cooldown on a credits refresh).
            account.note_success(model=model)
            row["ok"] = True
            return row
    except urllib.error.HTTPError as exc:
        row["elapsed_ms"] = int((time.time() - t0) * 1000)
        try:
            detail = read_http_error(exc, 400).decode("utf-8", "replace")
        except Exception:
            detail = str(exc)
        if looks_like_local_network(exc):
            row["network"] = True
            row["error"] = "本机网络不可用：%s" % exc_detail(exc)
            account.note_error(row["error"], cooldown=0)
            return row
        # The label keeps its 80-char cut; `detail` rides along untouched so the
        # panel can show the whole upstream body on hover.
        row["status"] = exc.code
        row["error"] = "HTTP %d: %s" % (exc.code, detail[:150])
        if exc.code == 403 and restriction_is_account_level(detail):
            # 测试也认账号级风控限制：不能用 60 秒的普通冷却把 2 小时覆盖掉，
            # 更不能用「测试通过」的语义把被限制的账号放回池子（见 #422）。
            # 一并记连坐事件：一键测试正好是发现「整池被清扫」的入口。
            account.note_restricted(detail=detail)
            _persist_restriction(account)
            note_account_restriction(account.realm, account.uid)
            row["restricted"] = True
            row["fails"] = int(account.fails)
            return row
        account.note_error("HTTP %d: %s" % (exc.code, detail[:80]),
                           cooldown=60, detail=detail)
        row["fails"] = int(account.fails)
        return row
    except Exception as exc:
        row["elapsed_ms"] = int((time.time() - t0) * 1000)
        if wb_accounts.is_tls_verify_error(exc):
            row["error"] = wb_accounts.tls_verify_hint(exc)
            return row
        if looks_like_local_network(exc):
            # 断网时上游一个字节都没收到：只报「本机网络不可用」，不给账号记冷却，
            # 否则网络一恢复，池子里全是刚才这轮测试留下的冷却账号。
            row["network"] = True
            row["error"] = "本机网络不可用：%s" % exc_detail(exc)
            account.note_error(row["error"], cooldown=0)
            return row
        row["error"] = "%s: %s" % (type(exc).__name__, exc)
        # 测试是诊断动作：失败要看得见（last_error），但不停用账号——真有问题，
        # 真实流量会照常记账；测试顺手把账号关掉才是「越测越乱」。
        account.note_error(str(exc)[:80], cooldown=0)
        row["fails"] = int(account.fails)
        return row

def extract_session_key(headers, payload):
    key = (
        headers.get("X-Conversation-Id") or
        headers.get("Conversation-Id") or
        headers.get("X-Session-Id") or
        headers.get("Session-Id") or
        payload.get("conversation_id") or
        payload.get("session_id") or
        (payload.get("metadata") or {}).get("conversation_id")
    )
    if key:
        return str(key).strip()
    return None


SSE_HEARTBEAT_SECONDS = 15.0


class SSEHeartbeat:
    """Write SSE comments while the request thread is blocked on the upstream.

    A keepalive checked only before a blocking read cannot run during the very
    silence it is meant to cover. This daemon thread owns the same lock as the
    request's writes, so comments can never split a JSON/SSE frame.
    """

    def __init__(self, write, interval=SSE_HEARTBEAT_SECONDS):
        self._write = write
        self._interval = max(0.05, float(interval))
        self._lock = threading.RLock()
        self._last_write_at = time.monotonic()
        self._stop = threading.Event()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name="wb-sse-heartbeat", daemon=True)
        self._thread.start()

    def write(self, data):
        if not data:
            return
        with self._lock:
            if self._closed:
                return
            self._write(data)
            self._last_write_at = time.monotonic()

    def _run(self):
        tick = min(0.1, self._interval / 4.0)
        while not self._stop.wait(tick):
            with self._lock:
                if self._closed:
                    return
                idle = time.monotonic() - self._last_write_at
                if idle < self._interval:
                    continue
                try:
                    self._write(b": keepalive\n\n")
                    self._last_write_at = time.monotonic()
                except Exception:
                    self._closed = True
                    return

    def close(self):
        with self._lock:
            self._closed = True
        self._stop.set()
        self._thread.join(timeout=max(0.1, self._interval))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def aggregate_stream(raw_iter, model, resp_id):
    """Fold an SSE stream into one non-streaming chat.completion object."""
    content, reasoning, finish = [], [], "stop"
    tool_calls_map = {}
    usage = None
    started = time.time()
    first_chunk_at = None
    saw_done = False
    for line in iter_sse_lines(raw_iter):
        data = strip_data_prefix(line.decode("utf-8", "replace"))
        if not data or data == "[DONE]":
            if data == "[DONE]":
                saw_done = True
            continue
        try:
            chunk = json.loads(data)
        except Exception:
            continue
        if first_chunk_at is None:
            first_chunk_at = time.time()
        if chunk.get("id"):
            resp_id = chunk["id"]
        if chunk.get("model"):
            model = chunk["model"]
        u = chunk.get("usage")
        if u:
            if usage is None or (u.get("total_tokens") or 0) >= (usage.get("total_tokens") or 0):
                usage = u
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning.append(delta["reasoning_content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index")
                if idx is None:
                    idx = len(tool_calls_map)
                fn = tc.get("function") or {}
                call_id = tc.get("id")
                fn_name = fn.get("name") or ""
                fn_args = fn.get("arguments") or ""
                if idx not in tool_calls_map:
                    tool_calls_map[idx] = {
                        "id": call_id or _new_id("call_"),
                        "type": tc.get("type") or "function",
                        "function": {
                            "name": fn_name,
                            "arguments": fn_args,
                        }
                    }
                else:
                    entry = tool_calls_map[idx]
                    if call_id:
                        entry["id"] = call_id
                    if fn_name:
                        entry["function"]["name"] = (entry["function"]["name"] or "") + fn_name
                    if fn_args:
                        entry["function"]["arguments"] = (entry["function"]["arguments"] or "") + fn_args
            fc = delta.get("function_call")
            # PATCH2-BY-OPS: 上游会在流末尾发 function_call:{"name":"","arguments":""}
            # 占位。原判断对空 dict 成立，会凭空生成 tool_call 并伪造 id，
            # 导致 finish_reason 被改成 "tool_calls"（参数全空）→ 严格客户端死等。
            # 故：只要 name 为空即视为无效占位直接跳过。
            fc_is_empty = (not isinstance(fc, dict)) or (not fc.get("name"))
            if fc and isinstance(fc, dict) and not fc_is_empty:
                idx = 0
                if idx not in tool_calls_map:
                    tool_calls_map[idx] = {
                        "id": _new_id("call_"),
                        "type": "function",
                        "function": {
                            "name": fc.get("name") or "",
                            "arguments": fc.get("arguments") or "",
                        }
                    }
                else:
                    entry = tool_calls_map[idx]
                    if fc.get("name") and not entry["function"]["name"]:
                        entry["function"]["name"] = fc["name"]
                    if fc.get("arguments"):
                        entry["function"]["arguments"] += fc["arguments"]
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    message = {"role": "assistant", "content": "".join(content)}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    # A call the model printed instead of emitting arrives as `content`. The
    # Responses path has parsed that shape for a while; this one did not, so the
    # client saw prose, found no call, and ended the turn with nothing to run.
    if not tool_calls_map and repair_leaked_tool_calls(message):
        finish = "tool_calls"
    # PATCH2-BY-OPS: 二次防御——剔除「无函数名且无参数」的空 tool_call。
    # 即使上游以 tool_calls 数组形式发空占位，也不会泄漏给客户端。
    if tool_calls_map:
        tool_calls_map = {
            k: v for k, v in tool_calls_map.items()
            if (v.get("function") or {}).get("name")
        }
    # A stream cut off by max_tokens / a dropped connection leaves the last
    # tool call with half-written JSON. Drop those calls instead of handing
    # the client unparsable arguments; only non-empty unparsable strings
    # count as truncated, so no-argument tools survive.
    if tool_calls_map and (finish == "length" or not saw_done):
        tool_calls_map = {
            k: v for k, v in tool_calls_map.items()
            if not is_truncated_arguments((v.get("function") or {}).get("arguments"))
        }
    if tool_calls_map:
        ordered_tcs = [tool_calls_map[k] for k in sorted(tool_calls_map.keys())]
        message["tool_calls"] = ordered_tcs
        if finish in ("stop", None):
            finish = "tool_calls"
    elif message.get("tool_calls"):
        # A call recovered from `content` counts as a real one, and it also
        # overrides a "length" the upstream reported for the same turn: the call
        # came through whole, so the turn did not really run out of room.
        finish = "tool_calls"
    elif finish == "tool_calls":
        # 占位被全部过滤掉，无实际工具调用，降级为正常结束，防止客户端无限挂起等待
        finish = "stop"
    if usage is None or (usage.get("total_tokens") or 0) == 0:
        full_c = "".join(content)
        full_r = "".join(reasoning)
        if full_c or full_r:
            comp = estimate_tokens(full_c) + estimate_tokens(full_r)
            prompt_est = max(1, comp // 2)
            usage = {
                "prompt_tokens": prompt_est,
                "completion_tokens": comp,
                "total_tokens": prompt_est + comp,
                "completion_tokens_details": {"reasoning_tokens": estimate_tokens(full_r)},
                "prompt_tokens_details": {"cached_tokens": 0},
            }
    out = {
        "id": resp_id or f"chatcmpl-wb-{uuid.uuid4().hex[:16]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }
    if usage:
        normalize_usage_cache_aliases(usage)
        out["usage"] = usage
    out["elapsed_ms"] = int((time.time() - started) * 1000)
    out["first_chunk_at"] = first_chunk_at
    return out

def set_global_api_key(key):
    """Persist the global /v1 key and make this process use it right away."""
    global API_KEY, API_KEY_FILE_SET
    wb_settings.set_api_key(ACCOUNTS_DIR, key)
    API_KEY = key
    API_KEY_FILE_SET = True


def runtime_settings_view():
    """Current panel-visible settings (never returns the password or the key)."""
    key = API_KEY or ""
    if len(key) > 8:
        masked = key[:4] + "*" * 6 + key[-4:]
    else:
        masked = "*" * len(key)
    keys = []
    for entry in configured_keys():
        raw = entry.get("key") or ""
        keys.append({
            "id": entry.get("id") or "",
            "name": entry.get("name") or "",
            "realm": entry.get("realm") or "",
            "models": list(entry.get("models") or []),
            "accounts": list(entry.get("accounts") or []),
            "enabled": entry.get("enabled", True) is not False,
            "masked": (raw[:4] + "*" * 6 + raw[-4:]) if len(raw) > 8 else "*" * len(raw),
            "source": entry.get("source") or "panel",
            "created_at": entry.get("created_at") or "",
        })
    # Deleted keys are read-only history: the secret is gone, so the panel can
    # only list them (name and dates) and must not offer a copy button.
    deleted_keys = []
    for entry in wb_settings.api_keys(ACCOUNTS_DIR, include_deleted=True):
        if not entry.get("deleted_at"):
            continue
        deleted_keys.append({
            "id": entry.get("id") or "",
            "name": entry.get("name") or "",
            "realm": entry.get("realm") or "",
            "created_at": entry.get("created_at") or "",
            "deleted_at": entry.get("deleted_at") or "",
        })
    return {
        "panel_password_is_default": wb_settings.panel_password_is_default(ACCOUNTS_DIR),
        "api_key_set": bool(key),
        "api_key_set_by_panel": API_KEY_FILE_SET,
        "api_key_masked": masked,
        "auth_required": auth_required(),
        "api_keys": keys,
        "deleted_api_keys": deleted_keys,
        "limits": wb_settings.limits_snapshot(ACCOUNTS_DIR),
        "reserve_credits": wb_settings.reserve_credits(ACCOUNTS_DIR),
        "daily_token_limit": wb_settings.daily_token_limit(ACCOUNTS_DIR),
        "daily_credit_limit": wb_settings.daily_credit_limit(ACCOUNTS_DIR),
        "model_daily_token_limit": wb_settings.model_daily_token_limit(ACCOUNTS_DIR),
        "pricing_refresh_minutes": wb_settings.pricing_refresh_minutes(ACCOUNTS_DIR),
        "credits_refresh_hours": wb_settings.credits_refresh_hours(ACCOUNTS_DIR),
        "section_max_height": wb_settings.section_max_height(ACCOUNTS_DIR),
        "schedule": dict(wb_settings.schedule_hours(ACCOUNTS_DIR),
                         daily_chat=wb_settings.daily_chat_times(ACCOUNTS_DIR),
                         daily_chat_effective=wb_settings.daily_chat_effective_times(
                             ACCOUNTS_DIR),
                         daily_chat_random=not bool(
                             wb_settings.daily_chat_times(ACCOUNTS_DIR))),
        "ui_language": wb_settings.ui_language(ACCOUNTS_DIR),
        "pricing_variant_inherit": wb_settings.pricing_variant_inherit(ACCOUNTS_DIR),
        "pricing_enabled": wb_settings.pricing_enabled(ACCOUNTS_DIR),
        "auto_switch_product": wb_settings.auto_switch_product(ACCOUNTS_DIR),
        "auto_switch_products": wb_settings.auto_switch_products(ACCOUNTS_DIR),
        "free_model_credit_fallback": wb_settings.free_model_credit_fallback(ACCOUNTS_DIR),
        "enable_anthropic_messages": wb_settings.enable_anthropic_messages(ACCOUNTS_DIR),
        "filter_models_by_key": wb_settings.filter_models_by_key(ACCOUNTS_DIR),
        "reasoning_effort_map": wb_settings.reasoning_effort_map(ACCOUNTS_DIR),
        "models_only_available": wb_settings.models_only_available(ACCOUNTS_DIR),
        "identity": wb_settings.identity_config(ACCOUNTS_DIR),
        "identity_version_defaults": wb_identity.default_versions(),
        "daily_chat_web": wb_settings.daily_chat_web(ACCOUNTS_DIR),
        "daily_chat_web_timeout":
            wb_settings.daily_chat_web_timeout(ACCOUNTS_DIR,
                                               default=wb_accounts.WEB_TURN_TIMEOUT),
        "daily_chat_web_timeout_default": wb_accounts.WEB_TURN_TIMEOUT,
        "daily_chat_web_timeout_max": wb_settings.DAILY_CHAT_WEB_TIMEOUT_MAX,
        "daily_chat_web_retry_backoff":
            wb_settings.daily_chat_web_retry_backoff(
                ACCOUNTS_DIR, default=wb_accounts.WEB_RETRY_BACKOFF),
        "daily_chat_web_retry_backoff_default": wb_accounts.WEB_RETRY_BACKOFF,
        "daily_chat_web_retry_backoff_max":
            wb_settings.DAILY_CHAT_WEB_RETRY_BACKOFF_MAX,
        "daily_chat_web_random_prompt":
            wb_settings.daily_chat_web_random_prompt(ACCOUNTS_DIR),
        "daily_chat_web_prompts": wb_settings.daily_chat_web_prompts(ACCOUNTS_DIR),
        "daily_chat_web_builtin_prompts": wb_webprompts.builtin_prompts(),
        "daily_chat_web_prompts_max": wb_settings.DAILY_CHAT_WEB_PROMPTS_MAX,
        # 预算模型的三个入口：有序候选序列、单账号总预算、启动补跑。
        # 旧的「最多尝试次数 / 失败重试 / 回退开关 / 回退组合」不再出现在面板上
        # （它们的值仍然被读取，用于把老部署的配置换算成预算与候选序列）。
        "daily_chat_models": wb_settings.daily_chat_models(ACCOUNTS_DIR),
        "daily_chat_models_max": wb_settings.DAILY_CHAT_MODELS_MAX,
        "daily_chat_budget": wb_settings.daily_chat_budget(
            ACCOUNTS_DIR, default=wb_accounts.WEB_BUDGET_DEFAULT),
        "daily_chat_budget_default": wb_accounts.WEB_BUDGET_DEFAULT,
        "daily_chat_budget_max": wb_settings.DAILY_CHAT_BUDGET_MAX,
        "daily_chat_startup_catchup":
            wb_settings.daily_chat_startup_catchup(ACCOUNTS_DIR),
        "daily_chat_web_fallback_activity":
            wb_settings.daily_chat_web_fallback_activity(ACCOUNTS_DIR),
        "local_web_tools": wb_settings.local_web_tools(ACCOUNTS_DIR),
        "remaining_priority_enabled":
            wb_settings.remaining_priority_enabled(ACCOUNTS_DIR),
        "egress_backoff_enabled":
            wb_settings.egress_backoff_enabled(ACCOUNTS_DIR),
        "realm_fallback_enabled":
            wb_settings.realm_fallback_enabled(ACCOUNTS_DIR),
        "account_max_concurrent":
            wb_settings.account_max_concurrent(ACCOUNTS_DIR),
        "account_max_concurrent_max":
            wb_settings.ACCOUNT_MAX_CONCURRENT_MAX,
        "account_inflight": account_inflight_snapshot(),
        "affinity_stable_key_enabled":
            wb_settings.affinity_stable_key_enabled(ACCOUNTS_DIR),
        "log_db_enabled": wb_settings.log_db_enabled(ACCOUNTS_DIR),
        "log_db_max_rows": wb_settings.log_db_max_rows(ACCOUNTS_DIR),
        "log_db_max_days": wb_settings.log_db_max_days(ACCOUNTS_DIR),
        "log_db_max_bytes": wb_settings.log_db_max_bytes(ACCOUNTS_DIR),
        "accounts_collapsed": wb_settings.accounts_collapsed(ACCOUNTS_DIR),
        "accounts_separate_tab": wb_settings.accounts_separate_tab(ACCOUNTS_DIR),
        "key_before_hidden": wb_settings.key_before_hidden(ACCOUNTS_DIR),
        "hidden_pages": wb_settings.hidden_pages(ACCOUNTS_DIR),
        "update_check_enabled": wb_settings.update_check_enabled(ACCOUNTS_DIR),
        "keep_backups": wb_settings.keep_backups(ACCOUNTS_DIR),
        "keep_backups_max": wb_settings.MAX_KEEP_BACKUPS,
        "upstream": wb_settings.upstream_config(ACCOUNTS_DIR),
        "usage": wb_settings.usage_config(ACCOUNTS_DIR),
        "prompt": wb_settings.prompt_config(ACCOUNTS_DIR),
        "window_probe": wb_settings.window_probe(ACCOUNTS_DIR),
        "quota_scan_models": wb_settings.quota_scan_models(ACCOUNTS_DIR),
        "quota_scan_interval_hours": quota_scan_interval_hours(),
        "quota_scans": quota_scan_snapshot(),
        "accounts_dir": ACCOUNTS_DIR,
        "usage_dir": USAGE_DIR,
        "settings_file": wb_settings.settings_path(ACCOUNTS_DIR),
        "version": "1.8.0",
    }
def current_account():
    """Account used for display purposes (health / usage summaries)."""
    return POOL.representative() if POOL else None

def quota_scan_interval_hours():
    """The polling period the quota scanner runs on, for the panel caption."""
    try:
        import wb_quota_scan
        return int(getattr(wb_quota_scan, "SCAN_INTERVAL", 6 * 3600) // 3600)
    except Exception:
        return 6

def quota_scan_snapshot(limit=12):
    """Recent quota-scan rows, or [] when the module cannot answer.

    A broken state file must never take the settings page down with it.
    """
    try:
        import wb_quota_scan
        return wb_quota_scan.snapshot(ACCOUNTS_DIR, limit=limit)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------
def _if_none_match_hit(header_value, etag):
    """If-None-Match 头是否命中给定的 ETag。

    RFC 7232 §3.2：字段值是一个逗号分隔的 entity-tag 列表，且 If-None-Match
    用弱比较——W/ 前缀忽略，所以 W/"x" 与 "x" 等同；"*" 匹配任何已存在的表示。
    """
    for token in header_value.split(","):
        token = token.strip()
        if token == "*":
            return True
        if token.startswith("W/"):
            token = token[2:].strip()
        if token and token == etag:
            return True
    return False


def _cache_etag(kind, key, built_at):
    """给带 TTL 缓存的只读接口派生一个强校验符（ETag）。

    由 (接口名, 缓存键, 缓存条目的构建时刻) 三者派生，**不是**对响应字节做哈希：

      - 构建时刻在两次重建之间是恒定的，所以"数字没变"的轮询回 304；缓存一重建
        它必然变化，客户端立刻拿到新数据。哈希响应字节恰好相反——payload 里有
        time.time() 派生的字段（/usage 的 started/since、/usage/timeseries 的
        桶边界与 until），同一份缓存被序列化两次都不逐字节相同，tag 会永远不命中，
        每次轮询照样重传 10~100KB，还白搭一次全量序列化 + 哈希的 CPU。
      - (键, 时刻) 都是现成的，一次 sha1 只哈希几十字节的键文本；哈希整个响应体
        则要先把 JSON 序列化出来，在 5 秒一轮的热路径上反而更贵。

    键文本进摘要而不是直接拼进 tag：缓存键里含调用方给的 realm，它可能带引号 /
    反斜杠（`?realm=a"b` 一路原样进键），拼出来会发出一个畸形的 ETag 头；摘要
    则永远是合法的 etagc 字符。摘要只取 16 个十六进制位：它只需要在"同一实例的
    少量缓存条目"之间区分，不是安全边界。
    """
    stamp = "%x" % int(built_at * 1_000_000)
    digest = hashlib.sha1(("%s\x1f%s" % (kind, key)).encode("utf-8")).hexdigest()[:16]
    return '"%s-%s"' % (stamp, digest)


def _anthropic_messages_enabled():
    if wb_settings.enable_anthropic_messages(ACCOUNTS_DIR):
        return True
    return os.environ.get("WB_ENABLE_ANTHROPIC_MESSAGES", "").strip().lower() in (
        "1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# Per-key concurrency tracking (#359)
# ---------------------------------------------------------------------------
_key_active_concurrency = {}
_key_concurrency_lock = threading.Lock()

def _acquire_key_slot(key_entry):
    """Try to acquire a concurrency slot for key_entry. Returns (ok, current, limit)."""
    if not isinstance(key_entry, dict):
        return True, 0, 0
    k_id = key_entry.get("id")
    if not k_id:
        return True, 0, 0
    limit = int(key_entry.get("concurrency_limit") or 0)
    if limit <= 0:
        return True, 0, 0
    with _key_concurrency_lock:
        current = _key_active_concurrency.get(k_id, 0)
        if current >= limit:
            return False, current, limit
        _key_active_concurrency[k_id] = current + 1
        return True, current + 1, limit

def _release_key_slot(key_entry):
    """Release a concurrency slot for key_entry."""
    if not isinstance(key_entry, dict):
        return
    k_id = key_entry.get("id")
    if not k_id:
        return
    limit = int(key_entry.get("concurrency_limit") or 0)
    if limit <= 0:
        return
    with _key_concurrency_lock:
        current = _key_active_concurrency.get(k_id, 0)
        if current > 1:
            _key_active_concurrency[k_id] = current - 1
        else:
            _key_active_concurrency.pop(k_id, None)

# 面板上「不带参数」的 POST 按钮：按钮会带一个 `{}` body，而路由从不解析它。
# 这些 body 必须在分发前排空 —— 否则那 2 个字节留在 socket 里，同一条 keep-alive
# 连接上的下一个请求会以 "{}GET /usage?..." 进场 → 501，面板那一轮轮询白丢（日志里
# 是 `code 501, message Unsupported method ('{}GET')`，紧跟在一条 200 的 POST
# 之后一两秒）。集中一处是因为按路由各写一遍已经漏过两回：/console/toggle 与
# /shutdown 修好之后，/updates/check、/updates/install、/quota-scan/run 又漏了。
# 新增这类「不读 body」的面板路由，请加进这个元组，别在路由里另写一份。
BODYLESS_PANEL_POSTS = ("/console/toggle", "/shutdown", "/updates/check",
                        "/updates/install", "/quota-scan/run")

class Handler(SettingsRoutes, AccountRoutes, AgentRoutes, OpsRoutes,
              BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # 关掉 Nagle（对连接设 TCP_NODELAY）：响应头和响应体是两次独立 write，Nagle
    # 会把第二次小写攒在手里等第一次的 ACK，碰上客户端 delayed ACK 就是每个小响应
    # 白付 ~40ms（路由器实测同一 keep-alive 连接的连续小响应：中位数 50.0ms ->
    # 1.41ms；面板几乎所有 <64KB 的 JSON 都中招，Tailscale 常驻连接上每笔都付
    # 一次）。StreamRequestHandler.setup() 原生支持这个开关，werkzeug/uvicorn
    # 同做法，一行生效。
    disable_nagle_algorithm = True
    # Which configured API key the caller used, set by _key_ok(). Its bound
    # realm decides the upstream exit for this request alone.
    key_entry = None
    # 本次 _json() 响应要附带的校验符（由 _json_cached() 设置）。放成实例属性
    # 而不是参数，是因为测试里大量 Handler 桩只覆写了 _json(code, obj) 这两个
    # 位置参数——加参数会让它们全部报错，而属性默认 None 对它们完全无感。
    _json_etag = None
    # The stdlib default caps the request line at 64KB and answers an opaque
    # bare "414 Request-URI Too Long" for anything longer. Raise it and reply in
    # the normal JSON error shape so an over-long URL is diagnosable.
    max_request_line = 1024 * 1024
    def handle_one_request(self):
        # Reset per-request auth state. HTTP/1.1 keeps the connection alive, so
        # one Handler instance serves many requests; a request that authenticates
        # via the panel token never reassigns key_entry, and without this reset
        # it inherited the realm binding of whatever API key used the connection
        # before it - sending that request to the wrong upstream exit.
        self.key_entry = None
        # 校验符同理：留着上一轮的 tag，下一次 _json() 就会把别的请求的 ETag
        # 发出去（keep-alive 上同一个 Handler 实例服务一整条连接）。
        self._json_etag = None
        # Body-tracking state must also start clean for every request, otherwise
        # a later drain would skip a body that has not been read yet.
        self._body_consumed = False
        try:
            self.raw_requestline = self.rfile.readline(self.max_request_line + 1)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            self.close_connection = True
            return
        except Exception:
            self.close_connection = True
            return
        if len(self.raw_requestline) > self.max_request_line:
            self.requestline = ''
            self.request_version = ''
            self.command = ''
            # The cap is enforced by reading at most max_request_line + 1
            # bytes, so the rest of the oversized line is still in the socket.
            # Replying and then closing with unread data pending makes the OS
            # send an RST, which discards the buffered reply - the client sees
            # a reset and no error at all. Drain a bounded amount first so the
            # 414 actually arrives.
            self._drain_oversized_request_line()
            try:
                self._error(414, "request line too long (limit %d bytes); "
                                 "put long content in the POST body, not the URL"
                            % self.max_request_line, "invalid_request_error")
            except Exception:
                pass
            self.close_connection = True
            return
        if not self.raw_requestline:
            self.close_connection = True
            return
        if not self.parse_request():
            return
        mname = 'do_' + self.command
        if not hasattr(self, mname):
            self.send_error(501, "Unsupported method (%r)" % self.command)
            return
        getattr(self, mname)()
        self.wfile.flush()
    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
    def finish(self):
        try:
            super().finish()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
    server_version = "wb-proxy/1.8.0"
    def log_message(self, fmt, *args):
        # 静默过滤前端看板高频定时心跳的正常 200 GET 请求（/logs、/usage、/accounts 轮询等）
        # 避免自增死循环刷屏与日志污染。遇 4xx/5xx 异常或所有非 GET 业务操作依然如实记录。
        try:
            status_code = int(args[1]) if len(args) > 1 and str(args[1]).isdigit() else 200
            if status_code < 400 and getattr(self, "command", "GET") == "GET":
                req_path = (getattr(self, "path", None) or (args[0] if args else "")).split("?")[0]
                quiet_prefixes = (
                    "/logs", "/usage", "/accounts", "/scheduler",
                    "/health", "/panel/status", "/realm", "/favicon.ico"
                )
                if any(req_path == p or req_path.startswith(p + "/") for p in quiet_prefixes):
                    return
        except Exception:
            pass
        log(fmt % args)
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 只有 _json_cached() 覆盖的只读接口才有校验符；其余调用点保持原样，
        # 一个头都不多发。
        etag = self._json_etag
        if etag is not None:
            # no-cache 而不是 no-store：允许浏览器存一份，但每次使用前必须回源
            # 校验。配合 ETag，校验命中就是一次 304（无 body），而不是重传
            # 10~100KB 的 JSON；no-store 会让浏览器连存都不存，条件请求无从谈起。
            self.send_header("Cache-Control", "no-cache")
            self.send_header("ETag", etag)
        # self.path is unset when parse_request() never ran (an over-long
        # request line is rejected before it), so fall back to "".
        if cors_origin_allowed(getattr(self, "path", "") or ""):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
        # Flush here rather than relying on the caller: with HTTP/1.1
        # keep-alive the client blocks until the response is actually on the
        # wire, and an error reply only flushed at the end of the handler looks
        # like a hung request.
        try:
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
    def _json_cached(self, code, data_fn, etag_fn):
        """带 TTL 缓存的只读接口：_json() + 条件请求（命中 If-None-Match 回 304）。

        data_fn() 取数、etag_fn() 取"当前缓存条目的校验符"，两者在这里配对调用，
        而且是**取数前后各取一次戳**：只有两次相同，才敢把这个 tag 发出去。这个
        相等就是"这份响应体确实出自该 tag 所描述的那条缓存条目"的证明——

          - 两次读取之间若发生了一次重建，戳就会变，于是这一次不发校验符，走普通
            200；客户端下一轮就重新同步（下一轮取数前取到的就是新条目的戳，前后
            一致，tag 生效）。代价是每次重建后多一次全量响应，换来的是绝不会发出
            一个不描述本次响应体的 tag。
          - 反过来（先取数、后取戳）才有真问题：拿到的是新条目的戳配旧条目的数据，
            客户端会带着这个 tag 回来，命中 304，于是把过期数字一直显示到下一次
            重建为止。

        两个可调用对象都无参，参数由调用方用闭包带上；顺序由这里保证，调用方不
        必（也不该）自己拼这个顺序。任何解析/取值异常都退化成"没有校验符"的普通
        200，绝不让响应路径崩掉。
        """
        try:
            before = etag_fn()
        except Exception:
            before = None
        data = data_fn()
        try:
            after = etag_fn()
        except Exception:
            after = None
        etag = after if (after is not None and after == before) else None
        if etag is not None:
            try:
                inm = self.headers.get("If-None-Match")
            except Exception:
                # 没有 headers 对象（测试桩 / 未走 parse_request）时按不匹配处理。
                inm = None
            if inm is not None and _if_none_match_hit(inm, etag):
                # 304 不带 body，也不带 Content-Length：RFC 7230 §3.3.3 规定 304
                # 在空行处结束，再报一个全量长度反而会诱使客户端 / 代理去等一个
                # 永远不会来的 body。校验符和 Cache-Control 必须原样重发
                # （RFC 7232 §4.1），否则缓存会丢掉状态、下次又整份重取。
                self.send_response(304)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self._json_etag = etag
        try:
            return self._json(code, data)
        finally:
            # 无论 _json() 是否抛异常，都不能把这次请求的 tag 留给下一次。
            self._json_etag = None

    # ---- 面板出口的脱敏封装 ----------------------------------------------
    # 刻意**不**在 _json() 里挂钩子：/v1/chat/completions、/v1/responses、
    # /v1/messages 的非流式返回体也走 _json()，那是一份模型输出，一个字节都不
    # 许动（若其中出现 "nickname": "13800138000"，脱敏就是篡改）。
    # 所以改成语义化命名：面板展示端点显式调用 _json_panel*，LLM 路由保持 _json。
    def _json_panel(self, code, obj):
        return self._json(code, _redact_account_names(obj))
    def _json_cached_panel(self, code, data_fn, etag_fn):
        # 脱敏只发生在 data_fn 的返回值上；ETag 由 (kind, key, built_at) 决定、
        # 与响应体无关，所以掩码不会造成「缓存体与校验符对不上」。
        return self._json_cached(code,
                                 lambda: _redact_account_names(data_fn()),
                                 etag_fn)
    def _discard_body(self):
        """Drain the request body so the connection stays in sync.

        A POST rejected before its body is read (401, 404, a panel route) leaves
        the payload sitting in the socket. On a keep-alive connection the next
        request then starts by parsing that leftover JSON as the request line,
        which surfaces as a bogus "414 Request-URI Too Long" - with an empty
        request line in the log - on an otherwise healthy connection.

        Handles both Content-Length and Transfer-Encoding: chunked, since
        clients switch to the latter for large bodies.
        """
        if getattr(self, "_body_consumed", False):
            # The handler already read the body (e.g. an error raised after
            # _read_payload). Reading Content-Length bytes again would block
            # until the client gives up, turning an instant reply into a hang.
            return
        # No parsed request means no headers object and nothing buffered to
        # drain: the over-long request line is rejected before parse_request()
        # ever runs. Reading self.headers here would raise out of _error() and
        # leave the client with no reply at all.
        headers = getattr(self, "headers", None)
        if headers is None:
            return
        # 排空即视为「body 已取走」：分发处排空过一次之后，路由里的 _error() 还会
        # 再调一次，重复读同一段 Content-Length 字节会一直等到客户端放弃。
        self._body_consumed = True
        transfer_encoding = (headers.get("Transfer-Encoding") or "").lower()
        try:
            if "chunked" in transfer_encoding:
                self._drain_chunked_body()
                return
            length = int(headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length <= 0:
            return
        if length > MAX_PAYLOAD_BYTES:
            # The client announced a body we refuse (413). Reading it would
            # block until it finishes sending gigabytes, so close instead and
            # let it see the reply plus the disconnect.
            self.close_connection = True
            return
        remaining = length
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except Exception:
            # A short read means the peer went away; nothing left to align.
            pass
    def _drain_chunked_body(self):
        """Consume a chunked body (terminated by a zero-length chunk)."""
        try:
            while True:
                line = self.rfile.readline(65536)
                if not line:
                    return
                size_field = line.split(b";", 1)[0].strip()
                if not size_field:
                    continue
                size = int(size_field, 16)
                if size == 0:
                    # Optional trailers, then the final blank line.
                    while True:
                        trailer = self.rfile.readline(65536)
                        if not trailer or trailer in (b"\r\n", b"\n"):
                            return
                remaining = size
                while remaining > 0:
                    data = self.rfile.read(min(remaining, 65536))
                    if not data:
                        return
                    remaining -= len(data)
                self.rfile.read(2)  # trailing CRLF after each chunk
        except Exception:
            self.close_connection = True
    # How much of an over-long request line to read before giving up. The peer
    # is already misbehaving; this only needs to be enough that a normal client
    # (which sent one line and is waiting for an answer) sees the reply.
    OVERSIZED_DRAIN_LIMIT = 8 * 1024 * 1024

    def _drain_oversized_request_line(self):
        """Consume the rest of a too-long request line, within a budget.

        Without this the reply is lost to an RST (see the caller). The newline
        ends the line; past the budget the peer is clearly not going to stop,
        so give up and let the connection close.
        """
        budget = self.OVERSIZED_DRAIN_LIMIT
        try:
            while budget > 0:
                chunk = self.rfile.readline(min(budget, 65536))
                if not chunk:
                    return
                budget -= len(chunk)
                if chunk.endswith(b"\n"):
                    return
        except Exception:
            pass

    def _handle_expect_continue(self):
        """Answer 'Expect: 100-continue' before deciding to reject a body.

        Clients that send this header wait for the interim response before
        transmitting a large payload. Rejecting outright (or draining first)
        made both sides wait on each other until the socket timed out.
        """
        # No parsed request means no headers object; there is no interim
        # response to send, and touching self.headers here would raise out of
        # the error reply the caller is trying to produce.
        headers = getattr(self, "headers", None)
        if headers is None:
            return
        expect = (headers.get("Expect") or "").lower()
        if "100-continue" not in expect:
            return
        try:
            self.send_response_only(100)
            self.end_headers()
            self.wfile.flush()
        except Exception:
            pass
    def _error(self, code, message, err_type="server_error"):
        # Every early rejection funnels through here, so draining the body in
        # one place covers all of them. Unblock any client still waiting on
        # "Expect: 100-continue" first, otherwise it never sends the body and
        # the drain below waits for data that will never arrive.
        self._handle_expect_continue()
        self._discard_body()
        error = {"message": message, "type": err_type, "code": code}
        hint = gateway_hint(code, message)
        if hint:
            error["gateway_hint"] = hint
        self._json(code, {"error": error})
    def _anthropic_error(self, code, message, err_type=None):
        """Reply with the Anthropic JSON error envelope, not OpenAI's."""
        self._handle_expect_continue()
        self._discard_body()
        return self._json(code, anthropic_error_obj(code, message, err_type))
    def _rate_limited(self, exc):
        """429 with Retry-After, so clients back off instead of hammering.

        The upstream body names the reset time; when it does not, fall back to
        the shortest model cooldown we know about.
        """
        wait = max(1, int(getattr(exc, "wait", 60) or 60))
        # A 429 raised without an upstream call (the pool is parked by the
        # daily token guard) carries its own text; everything else keeps the
        # upstream wording.
        custom = getattr(exc, "message", "")
        text = custom or (
            "upstream rate limit reached for this model; retry in %ds" % wait)
        # The upstream detail only decorates the upstream wording; a local
        # message would only repeat itself.
        detail = ""
        if exc.detail and not custom:
            detail = " - " + exc.detail[:200]
        # 429 can be answered before the body is read (the model cooldown is
        # checked on the way in), so drain it exactly like _error does.
        self._handle_expect_continue()
        self._discard_body()
        body = json.dumps({
            "error": {
                "message": text + detail,
                "type": "rate_limit_error",
                "code": 429,
                "retry_after": wait,
            }
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(429)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Retry-After", str(wait))
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
    def _anthropic_rate_limited(self, exc):
        """429 with Retry-After in the Anthropic error envelope."""
        wait = max(1, int(getattr(exc, "wait", 60) or 60))
        text = (getattr(exc, "message", "") or
                "upstream rate limit reached for this model; retry in %ds" % wait)
        if getattr(exc, "detail", "") and not getattr(exc, "message", ""):
            text += " - " + str(exc.detail)[:200]
        self._handle_expect_continue()
        self._discard_body()
        body = json.dumps(anthropic_error_obj(429, text, "rate_limit_error"),
                          ensure_ascii=False).encode("utf-8")
        self.send_response(429)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Retry-After", str(wait))
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _download(self, filename, obj):
        """Send a JSON document as a browser download.
        Content-Disposition is quoted because the filename is generated from
        user-controlled parts (the realm filter) and could otherwise break the
        header or allow a response-splitting attempt.
        """
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        safe = re.sub(r'[^A-Za-z0-9._-]', "_", str(filename))[:120] or "export.json"
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % safe)
        self.send_header("Cache-Control", "no-store")
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
    def _supplied_key(self):
        """The key the caller presented.

        Accepts the spellings clients actually send: the Authorization header
        with or without the "Bearer" scheme, the x-api-key / api-key headers
        used by several OpenAI-compatible clients, and the ?key= query the
        dashboard falls back to when it cannot set headers.
        """
        # The auth scheme is case-insensitive per RFC 7235, so "bearer sk-x"
        # and "BEARER sk-x" must strip just like "Bearer sk-x". The old
        # removeprefix("Bearer ") left the scheme attached for other casings
        # and the whole "bearer sk-x" string was then compared as a key.
        header = (self.headers.get("Authorization") or "").strip()
        supplied = ""
        if header:
            scheme, _, value = header.partition(" ")
            if scheme.lower() == "bearer":
                supplied = value.strip()
            else:
                supplied = header
            # Tolerate a quoted credential, which some SDKs add.
            if len(supplied) >= 2 and supplied[0] == supplied[-1] and supplied[0] in "\"'":
                supplied = supplied[1:-1].strip()
        if supplied:
            return supplied
        for name in ("x-api-key", "api-key", "x-auth-token"):
            value = (self.headers.get(name) or "").strip()
            if value:
                return value
        # Browsers cannot set headers on a top-level navigation, so accept the
        # key as a query parameter too - the dashboard uses this when opened
        # from another device.
        try:
            query = parse_qs(urlparse(self.path).query)
            for name in ("key", "api_key", "api-key"):
                value = (query.get(name) or [""])[0].strip()
                if value:
                    return value
            return ""
        except Exception:
            return ""
    def _key_ok(self):
        """True when the request carries a right key (or no key is needed)."""
        # An authenticated panel session also unlocks the management APIs,
        # so the browser never has to keep the API key in localStorage.
        if self._panel_ok():
            return True
        self.key_entry = identify_key(self._supplied_key())
        if self.key_entry:
            return True
        if not auth_required():
            return True
        return False
    def _key_realm(self):
        """Realm bound to the key this request used, or "" when unbound."""
        return (self.key_entry or {}).get("realm") or ""
    def _key_id(self):
        """Settings id of the key that paid for this request, or None.

        None covers a panel session and a deployment that runs without any key
        configured - both are real, and the usage log keeps them apart from
        rows written before the key field existed.
        """
        return (self.key_entry or {}).get("id") or None
    def _cross_realm_error(self, model, realm):
        """Explain a model/exit mismatch instead of letting upstream reject it.
        Sending gpt-6-astra to the domestic exit (or deepseek-v4-pro to the
        international one) earns an opaque 403 from upstream, so catch it here
        and say which key is bound where.
        """
        if not realm or not model:
            return ""
        owner = exclusive_realm(model)
        if not owner or owner == realm:
            return ""
        name = (self.key_entry or {}).get("name") or "当前 Key"
        served = "国内版" if owner == "cn" else "国际版"
        used = "国内版" if realm == "cn" else "国际版"
        return ("模型 %s 只在%s提供，但「%s」绑定的是%s出口。"
                "请改用对应出口的 Key，或把该 Key 的出口改为「跟随面板切换」。"
                % (model, served, name, used))
    def _banned_model_error(self, model):
        """被封锁的模型直接报错，不碰上游、不扣任何点数。"""
        if not is_model_banned(model):
            return ""
        return banned_model_message(model)

    def _key_model_error(self, model):
        """Per-key model restriction: reject before the request reaches upstream.

        A key that lists no models stays unrestricted, so this is a no-op
        unless the operator asked for a limit.
        """
        entry = self.key_entry
        if not entry:
            return ""
        if wb_settings.key_allows_model(entry, model):
            return ""
        return key_model_message(entry, model)

    def _key_allowed_accounts(self):
        """The account ids this request's key may use, or None when unrestricted."""
        return wb_settings.key_allowed_accounts(self.key_entry)

    def _key_account_error(self, account):
        """Refuse a pinned account the requesting key may not use."""
        entry = self.key_entry
        if not entry or account is None:
            return ""
        if wb_settings.key_allows_account(entry, account.uid):
            return ""
        return ("API Key「%s」的账号限制不允许账号 %s。"
                "请在看板「设置」页修改这个 Key 的允许账号，或改用允许该账号的 Key。"
                % ((entry or {}).get("name") or "未命名", account.uid))

    def _request_realm(self, explicit=None):
        """Pick the upstream exit for this request.
        Priority: an explicit ?realm= argument, then the realm bound to the
        API key, then the X-Realm header / ?realm= query, and finally the
        global switch. Returning None lets open_upstream() fall back to
        model-based detection.
        """
        if explicit:
            return explicit
        bound = self._key_realm()
        if bound:
            return bound
        header = self.headers.get("X-Realm")
        if header:
            return header
        try:
            return parse_qs(urlparse(self.path).query).get("realm", [None])[0]
        except Exception:
            return None
    def _authorized(self):
        if self._key_ok():
            return True
        # Say how a key must be presented, so a key that merely looks identical
        # (masked copy, trailing whitespace) is diagnosable straight from the
        # client error. Deliberately does not echo key names or values.
        hint = ("send it as 'Authorization: Bearer <key>' or '?key=<key>'; "
                "copy the value from the panel's 设置 page")
        try:
            if not any(k.get("enabled") for k in configured_keys()) and not API_KEY:
                hint = ("no key is configured - open the dashboard and add one, "
                        "or restart with --api-key")
        except Exception:
            pass
        self._error(401, "invalid api key - " + hint, "invalid_request_error")
        return False
    # ---- web panel access ----
    def _panel_token(self):
        """Session token from the X-Panel-Token header.
        Deliberately header-only: a token in the query string leaks through
        browser history, the Referer header and any reverse-proxy access log.
        """
        token = (self.headers.get("X-Panel-Token") or "").strip()
        return token
    def _panel_ok(self):
        return PANEL.valid(self._panel_token())
    @staticmethod
    def _is_panel_route(path):
        """Management endpoints shown in the web panel.
        Model listings stay reachable with the API key alone so that plain
        OpenAI clients can keep discovering models.
        """
        if path.startswith("/accounts"):
            return True
        if path.startswith("/usage") or path.startswith("/v1/usage"):
            return True
        if path.startswith("/activity"):
            return True
        if path.startswith("/tasks") or path.startswith("/scheduler"):
            return True
        if path.startswith("/settings"):
            return True
        if path.startswith("/updates"):
            return True
        if path.startswith("/logs"):
            return True
        # 控制台窗口显隐 / 服务关闭是本机操作者的面板功能，与 /updates、
        # /logs 同属 panel-only。
        if path.startswith("/console"):
            return True
        # The pricing table is the panel's own view of the estimate, not part
        # of the OpenAI-compatible surface: its writes (/pricing/refresh,
        # /pricing/mapping) were already panel-only, and the read side returns
        # the same management state plus resolved project-local file paths.
        if path.startswith("/pricing"):
            return True
        if path.startswith("/agents"):
            return True
        return False
    def _strip_pinned_prefix_only(self):
        """Strip /u/<ref> from self.path; returns False for a malformed ref."""
        _PINNED.ref = None
        _PINNED.account = None
        parsed = urlparse(self.path)
        path = parsed.path
        if not path.startswith("/u/"):
            return True
        rest = path[3:]
        ref, sep, tail = rest.partition("/")
        ref = urllib.parse.unquote(ref).strip()
        if not ref or not sep:
            return False
        _PINNED.ref = ref
        self.path = "/" + tail + (("?" + parsed.query) if parsed.query else "")
        return True

    def _apply_pinned_route(self):
        """Validate the pinned ref and resolve it to exactly one account."""
        if not self._strip_pinned_prefix_only():
            self._error(404, "pinned account route must be /u/<ref>/v1/...",
                        "invalid_request_error")
            return False
        ref = pinned_ref()
        if not ref:
            return True
        if POOL is None:
            self._error(503, "pinned account '%s' is not usable: account pool is not ready" % ref,
                        "invalid_request_error")
            return False
        account, why = POOL.find_unique(ref)
        if account is None:
            status = 409 if why == "is ambiguous" else 404
            self._error(status, "pinned account '%s' %s" % (ref, why),
                        "invalid_request_error")
            return False
        _PINNED.account = account
        return True

    def do_OPTIONS(self):
        self._strip_pinned_prefix_only()
        self.send_response(204)
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()
    def do_GET(self):
        if not self._apply_pinned_route():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        # The standalone configurator, for clients on machines that do not run
        # the gateway. Served before the panel gate and deliberately without a
        # session: the machines that need it are exactly the ones that cannot
        # pass the loopback check, and the body is public source code (the
        # same wb_agents.py shipped in the repository, no secrets inside).
        if path == "/agents/cli":
            return self._get_agents_cli()
        if self._is_panel_route(path) and not self._panel_ok():
            return self._error(401, "panel password required", "invalid_request_error")
        if path in ("/", "/dashboard", "/ui"):
            return self._get_dashboard()
        if path.startswith("/assets/"):
            return self._get_asset(path)
        if path == "/panel/status":
            return self._get_panel_status()
        if path == "/console/state":
            return self._get_console_state()
        if path == "/health":
            return self._get_health()
        if path == "/realm":
            return self._get_realm()
        if path == "/v1/balance-summary":
            return self._get_v1_balance_summary()
        if path in ("/v1/models", "/models"):
            return self._get_v1_models()
        if path in ("/usage", "/v1/usage"):
            return self._get_v1_usage(query)
        if path == "/usage/recent":
            return self._get_usage_recent(query)
        if path == "/accounts/credits/detail":
            return self._get_account_credits_detail(query)
        if path == "/accounts/credits":
            return self._get_accounts_credits()
        if path == "/accounts/credits/grants":
            return self._get_account_credits_grants()
        if path == "/accounts":
            return self._get_accounts(query)
        if path == "/accounts/export":
            return self._get_accounts_export(query)
        if path == "/accounts/login/poll":
            return self._get_accounts_login_poll(query)
        if path == "/usage/analytics":
            return self._get_usage_analytics(query)
        if path == "/usage/by-account":
            return self._get_usage_by_account()
        if path == "/usage/remaining":
            return self._get_usage_remaining()
        if path == "/usage/perf":
            return self._get_usage_perf(query)
        if path == "/usage/timeseries":
            return self._get_usage_timeseries(query)
        if path == "/activity/history":
            return self._get_activity_history(query)
        if path == "/tasks":
            return self._get_tasks(query)
        if path == "/scheduler":
            return self._get_scheduler()
        if path == "/pricing":
            return self._get_pricing()
        if path == "/settings":
            return self._get_settings()
        if path == "/updates":
            return self._get_updates()
        if path == "/updates/backups":
            return self._get_update_backups()
        if path == "/proxy/slots":
            if not self._panel_ok():
                return self._error(
                    401, "panel password required", "invalid_request_error"
                )
            return self._json(200, {"slots": proxy_slots_view()})
        if path == "/logs":
            return self._get_logs(query)
        if path == "/logs/export":
            return self._get_logs_export()
        if path == "/settings/reveal":
            return self._get_settings_reveal(query)
        if path == "/agents":
            return self._get_agents()
        if path == "/agents/model-selection":
            return self._get_agents_model_selection()
        if path == "/agents/available":
            # 看板启动时用这个廉价判定决定要不要显示入口：不 import
            # wb_agents、不做任何探测（issue #246）。
            return self._json(200, {"enabled": self._agents_client_allowed()})
        return self._error(404, "not found", "invalid_request_error")
    def _get_dashboard(self):
        return self._dashboard()

    def _get_asset(self, path):
        """One file under assets/css or assets/js, revalidated like the page.

        Public, like the page itself: the panel gate is drawn by these files,
        and they carry no secrets. The name is checked against a strict shape
        (no dot segments, no separators) instead of normalising a user path.
        """
        parts = path[len("/assets/"):].split("/")
        if len(parts) != 2 or parts[0] not in ("css", "js") \
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.(css|js)", parts[1]):
            return self._error(404, "not found", "invalid_request_error")
        ext = os.path.splitext(parts[1])[1]
        file_path = os.path.join(DASHBOARD_ASSETS, parts[0], parts[1])
        try:
            st = os.stat(file_path)
            with open(file_path, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._error(404, "not found", "invalid_request_error")
        etag = '"%x-%x"' % (st.st_mtime_ns, st.st_size)
        inm = self.headers.get("If-None-Match")
        if inm is not None and _if_none_match_hit(inm, etag):
            self.send_response(304)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("ETag", etag)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ASSET_TYPES[ext])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)

    def _get_panel_status(self):
        # Answer without a token: the dashboard needs to know whether to
        # show the login screen before it can hold a session.
        info = {
            "panel_password_required": True,
            "panel_password_is_default": wb_settings.panel_password_is_default(ACCOUNTS_DIR),
            "authenticated": self._panel_ok(),
        }
        # Whether a key exists is not a secret; its value never leaves the
        # process, and the settings endpoint only reports a masked form.
        info["api_key_set"] = bool(API_KEY)
        return self._json(200, info)

    def _get_console_state(self):
        # 按钮不再 spawn 进程，而是在新窗口打开运行日志页。这样 Windows /
        # macOS / 容器走同一条路，不存在「当前环境开不了窗口」的伪状态（#427）。
        return self._json(200, {"supported": True, "mode": "web", "visible": False})

    def _route_console_toggle(self):
        # 按钮的 `{}` body 已在 do_POST 分发前排空（见 BODYLESS_PANEL_POSTS）。
        # 兼容旧面板：新面板直接 window.open，不再请求这里。
        return self._json(200, {"ok": True, "mode": "web",
                                "url": "/dashboard?tab=logs", "visible": False})

    def _route_shutdown(self):
        # 同 toggle：按钮的 `{}` body 已在 do_POST 分发前排空（见 BODYLESS_PANEL_POSTS）。
        # 回响应、延迟退出：0.4s 让 _json 的 body 先 flush 到 socket；端口
        # 随进程释放，下次启动走原来的启动脚本。
        def _bye():
            time.sleep(0.4)
            try:
                sys.stdout.flush()
            except Exception:
                pass
            try:
                flush_log_channel(0.5)   # 已入队的行尽力落盘，卡住也不拖延退出
            except Exception:
                pass
            os._exit(0)
        threading.Thread(target=_bye, daemon=True).start()
        return self._json(200, {"ok": True, "shutting_down": True})

    def _get_health(self):
        # Always answer (the launcher uses this to detect a running copy),
        # but only expose account identity to an authorised caller.
        rep = current_account()
        info = {
            "ok": True,
            # Report the realm actually in use; this used to be the
            # literal "intl" and drifted from the panel switch.
            "realm": CURRENT_REALM,
            "accounts": len(POOL.accounts) if POOL else 0,
            "accounts_ready": POOL.count_ready() if POOL else 0,
            "api_key_required": auth_required(),
        }
        if self._key_ok():
            info.update({
                "uid": rep.uid if rep else None,
                "domain": rep.domain if rep else None,
                "issuer": wb_accounts.jwt_issuer(rep.access_token) if rep else None,
                "credential_file": os.path.basename(rep.path) if rep and rep.path else None,
                "expires_at": rep.expires_at if rep else None,
            })
        return self._json(200, info)
    # Accept the conventional /v1 prefix and the bare path, because clients
    # differ in whether they append "/v1" themselves.

    def _get_realm(self):
        return self._json(200, {"current": CURRENT_REALM, "options": ["intl", "cn"]})

    def _get_v1_models(self):
        if not self._authorized():
            return
        req_realm = self._request_realm() or CURRENT_REALM
        try:
            entries = fetch_models(realm=req_realm)
        except Exception as exc:
            return self._error(502, str(exc))
        # Level 4 is best effort: warm the local cache in the background at
        # most once per cooldown; offline deployments just keep the fallback.
        try:
            wb_modelsdev.refresh_async(ACCOUNTS_DIR, log=log)
        except Exception:
            pass
        data = [model_entry(mid, meta) for mid, meta in entries]
        pinned = pinned_account()
        if pinned is not None:
            # 定址路由宣告的是「这个账号实际能接的模型」：该账号禁用的模型
            # 不出现在清单里，客户端按账号拆 provider 时才不会选到必失败的项。
            data = [m for m in data if not pinned.model_disabled(m.get("id"))]
        # 可选：清单只宣告当前 Key 能用的模型。默认关闭时保持原来的全量
        # 目录语义；打开后，配了模型限制的 Key 不会再看到会被本地 400 拒掉
        # 的项，自动发现型客户端也就不会选到必失败的模型。
        if wb_settings.filter_models_by_key(ACCOUNTS_DIR) and self.key_entry:
            data = [m for m in data
                    if wb_settings.key_allows_model(self.key_entry, m.get("id"))]
        # 可选：清单只宣告当前账号池里至少有一个账号能服务的模型。默认关闭
        # 保持全量目录；PRO ONLY 模型只有在池里有付费账号时才列出。
        if wb_settings.models_only_available(ACCOUNTS_DIR):
            data = [m for m in data
                    if models_available_for(m.get("id"),
                                            req_realm or CURRENT_REALM)]
        # #505: 单向标注——只说「免费账号不可用」，从不宣称可用。数据源就是
        # 调度器拦人用的那份 tier map（上游目录的 `modelTiers`），所以标注与
        # 实际拦截不可能互相矛盾：被门控的模型在本地就被拒，请求根本发不到
        # 上游——这也是「撞到 11158 才写标」那条旧通道后来一直空着的原因。
        # 没有标注 = 未知，不等于可用。
        label_realm = req_realm or CURRENT_REALM
        gate_at = wb_tiers.ingested_at(label_realm)
        for m in data:
            if wb_tiers.requires_pro(m.get("id"), label_realm):
                m["entitlement"] = {
                    "source": "catalog",
                    "required": "standard",
                    "at": gate_at,
                }
        payload = {"object": "list", "data": data,
                   "realm": req_realm or CURRENT_REALM}
        # 来源说明（面板展示用）：这批清单是上游 API 实时取的，还是读的
        # 本机桌面缓存文件/内置快照，以及各自的时间戳。
        meta = catalog_meta(req_realm)
        if meta:
            payload["catalog"] = meta
        return self._json(200, payload)

    def _get_v1_balance_summary(self):
        if not self._authorized():
            return
        realm = self._request_realm() or CURRENT_REALM
        return self._json(200, credit_balance_summary(realm))

    def _get_v1_usage(self, query):
        if not self._authorized():
            return
        req_realm = query.get('realm', [None])[0] or self.headers.get('X-Realm') or CURRENT_REALM
        req_range, req_since, req_until = range_query(query)
        # 取数与取校验符必须看到同一套参数（否则 tag 落到另一条缓存条目上），
        # 但"先取戳、再取数、再取一次戳"的配对顺序由 _json_cached 保证。
        return self._json_cached_panel(
            200,
            lambda: usage_snapshot(realm=req_realm, range=req_range,
                                   since=req_since, until=req_until),
            lambda: usage_snapshot_etag(realm=req_realm, range=req_range,
                                        since=req_since, until=req_until))

    def _get_usage_recent(self, query):
        if not self._authorized():
            return
        try:
            limit = max(1, min(1000, int((query.get("limit") or ["100"])[0])))
        except ValueError:
            limit = 100
        try:
            page = max(1, int((query.get("page") or ["1"])[0]))
        except ValueError:
            page = 1
        req_realm = query.get('realm', [None])[0] or self.headers.get('X-Realm') or CURRENT_REALM
        return self._json_panel(200, recent_usage(limit, realm=req_realm, page=page))

    def _get_activity_history(self, query):
        """账号每日活动的结构化历史（issue #34）。只读，供面板的签到记录用。

        筛选条件写错时返回 400 而不是静默忽略：调用方拿着一个被忽略的
        result=failed 会以为「这几天没有失败」，实际上它拿回的是全部结果。
        """
        if not self._authorized():
            return

        def first(name):
            values = query.get(name) or [""]
            return (values[0] if values else "") or ""

        try:
            payload = wb_activity.query(range_key=first("range") or None,
                                        uid=first("uid"),
                                        task=first("task"),
                                        result=first("result"),
                                        limit=first("limit") or None)
        except ValueError as exc:
            return self._error(400, str(exc), "invalid_request_error")
        return self._json_panel(200, payload)

    def _get_usage_analytics(self, query):
        if not self._authorized():
            return
        req_realm = query.get("realm", [None])[0] or None
        req_range, req_since, req_until = range_query(query)
        return self._json_cached_panel(
            200,
            lambda: compute_usage_analytics(realm=req_realm, range=req_range,
                                            since=req_since, until=req_until),
            lambda: usage_analytics_etag(realm=req_realm, range=req_range,
                                         since=req_since, until=req_until))

    def _get_usage_by_account(self):
        if not self._authorized():
            return
        return self._json_cached_panel(200,
                                 lambda: {"accounts": usage_by_account()},
                                 usage_by_account_etag)

    def _get_usage_remaining(self):
        if not self._authorized():
            return
        # 载荷挂短 TTL（WB_REMAINING_TTL，默认 15 秒）+ ETag：面板 5 秒一轮的
        # 轮询在 TTL 内命中同一条缓存 → 校验符不变 → 304，重建与重传都省掉。
        return self._json_cached_panel(200, remaining_usage, remaining_usage_etag)

    def _get_usage_timeseries(self, query):
        if not self._authorized():
            return
        req_realm = (query.get('realm', [None])[0]
                     or self.headers.get('X-Realm') or CURRENT_REALM)
        req_range, req_since, req_until = range_query(query)
        bucket = (query.get("bucket") or [None])[0]
        try:
            bucket_seconds = int(bucket) if bucket else None
        except (TypeError, ValueError):
            bucket_seconds = None
        return self._json_cached_panel(
            200,
            lambda: usage_timeseries(realm=req_realm, range=req_range,
                                     since=req_since, until=req_until,
                                     bucket_seconds=bucket_seconds),
            lambda: usage_timeseries_etag(realm=req_realm, range=req_range,
                                          since=req_since, until=req_until,
                                          bucket_seconds=bucket_seconds))

    def _get_usage_perf(self, query):
        if not self._authorized():
            return
        try:
            sample = max(10, min(20000, int((query.get("sample") or ["5000"])[0])))
        except ValueError:
            sample = 5000
        req_realm = query.get('realm', [None])[0] or self.headers.get('X-Realm') or CURRENT_REALM
        req_range, req_since, req_until = range_query(query)
        return self._json_cached_panel(
            200,
            lambda: perf_stats(sample, realm=req_realm, range=req_range,
                               since=req_since, until=req_until),
            lambda: perf_stats_etag(sample, realm=req_realm, range=req_range,
                                    since=req_since, until=req_until))

    def _get_scheduler(self):
        if not self._authorized():
            return
        return self._json_panel(200, SCHEDULER.status() if SCHEDULER else {"enabled": False, "msg": "未运行"})

    def _get_pricing(self):
        if not self._authorized():
            return
        if PRICING:
            return self._json(200, PRICING.status())
        return self._json(200, {
            "interval_minutes": 0.0, "enabled": False, "running": False,
            "master_enabled": wb_settings.pricing_enabled(ACCOUNTS_DIR),
            "policies": 0, "models": 0, "current": {}, "logs": [],
            "gaps": [], "gap_summary": {"total": 0, "or_missing": 0,
                                        "variant_unmatched": 0, "aliases": 0,
                                        "variants_enabled":
                                            wb_settings.pricing_variant_inherit(ACCOUNTS_DIR)},
            "variant_inherit": wb_settings.pricing_variant_inherit(ACCOUNTS_DIR),
            "overrides_file": wb_pricing.overrides_path(),
            "policies_file": wb_pricing.policies_path(),
            "timeline": wb_pricing.timeline_path(), "msg": "未运行",
        })

    def _get_logs(self, query):
        if not self._authorized():
            return
        try:
            limit = int(query.get("limit", ["200"])[0])
        except (ValueError, TypeError):
            limit = 200
        level = query.get("level", [""])[0]
        tag = query.get("tag", [""])[0]
        search = query.get("search", [""])[0]
        try:
            since_id = int(query.get("since_id", ["0"])[0])
        except (ValueError, TypeError):
            since_id = 0
        result = get_logs(limit=limit, level=level, tag=tag, search=search, since_id=since_id)
        # 面板要据 mode 决定顶部文案（内存保留 vs 落库保留）与是否显示库容量，
        # 所以把模式与库统计一并带回；统计失败不该让整个日志请求失败。
        result["mode"] = "db" if _log_db_active() else "memory"
        if _log_db_active():
            try:
                stats = LOG_STORE.stats() or {}
            except Exception:
                stats = {}
            result["stored"] = stats.get("rows", 0)
            result["db_bytes"] = stats.get("bytes", 0)
        return self._json_panel(200, result)

    def _get_logs_export(self):
        if not self._authorized():
            return
        log_data = get_logs(limit=5000)
        # 导出走的是 send_response/wfile，不经过 _json_panel，所以掩码得自己来：
        # 新写入的条目在 add_log_entry 里已掩码，但落库打开之前的历史行仍是明文，
        # 直接拼进下载文件就把「出口脱敏」这条从侧门绕过去了。
        lines = [f"[{item['ts']}] [{item['level']}] [{item['tag']}] {_mask_text(str(item['msg']))}"
                 for item in log_data["logs"]]
        text_content = "\n".join(lines).encode("utf-8")
        filename = f"wb-proxy-{time.strftime('%Y%m%d-%H%M%S')}.log"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(text_content)))
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(text_content)
        return

    def _dashboard(self):
        try:
            with open(DASHBOARD_HTML, "rb") as fh:
                body = fh.read()
        except Exception as exc:
            return self._error(500, f"dashboard.html unavailable: {exc}")
        # The static file carries a placeholder; replace it with the instance
        # default so the first paint already uses the right language.
        language = wb_settings.ui_language(ACCOUNTS_DIR)
        body = body.replace(
            b'data-ui-language="__WB_UI_LANGUAGE__"',
            ('data-ui-language="%s"' % language).encode("utf-8"),
        )
        # 面板首页是纯静态资源（服务端逐字节原样发出、不含任何机密），所以可以
        # 允许浏览器存储副本、每次打开再回源校验：校验命中回 304（无 body），
        # 省下整页（约 470KB）的重复下载，手机 / Tailscale 远程访问体感最明显。
        #
        # 校验符必须覆盖**所有**影响响应体的输入，不只是文件本身：body 还取决于
        # 上面注入的 ui_language——用户改一次界面语言，文件没动、body 却变了；
        # 若 tag 只看文件，客户端带旧 tag 回来会拿到 304 + 旧语言的页面。所以 tag
        # 由 (文件 mtime_ns, size, language) 三者派生：前两个代表文件字节（本文件
        # 只在应用更新时被整体替换、从不原地修改），第三个就是本次实际注入的值，
        # 三者组合变化 ⇔ 响应字节变化。mtime 用纳秒精度，同一秒内的两次替换也能
        # 得到不同 tag。
        etag = None
        try:
            st = os.stat(DASHBOARD_HTML)
            # 强校验符（不带 W/ 前缀）：响应是「文件字节 + 本次语言」的精确副本，
            # tag 变 ⇔ 字节变。
            etag = '"%x-%x-%s"' % (st.st_mtime_ns, st.st_size, language)
        except Exception:
            # stat 取不到（或时间戳无法表示）不是致命错误：退化为一律按普通
            # 200 处理，只是这一次没有条件请求支持，绝不让面板页本身打不开。
            etag = None
        # 只认 If-None-Match，不发送、也不理会 If-Modified-Since。Last-Modified
        # 只能描述文件的 mtime，而响应体还取决于语言：日期无法表达这个输入，
        # 一旦发布出去，偏好日期的客户端就会拿它校验，语言一变就拿到过期的
        # 304。ETag 覆盖全部输入、单靠它就足够完备，所以干脆不提供日期——
        # 不发布它，客户端就没有用它的理由（RFC 7232 §2.2 里 Last-Modified 只是
        # SHOULD，响应体并非单一文件、没有可一致表达的修改日期，只发 ETag 完备）。
        if etag is not None:
            inm = self.headers.get("If-None-Match")
            if inm is not None and _if_none_match_hit(inm, etag):
                # 304 不带 body：浏览器手里已有一份，一个字节都不用再传。也不带
                # Content-Length：304 按 RFC 7230 §3.3.3 在空行处结束，再报全量
                # 长度反而会诱使客户端 / 代理等待一个永远不会来的 body。校验符和
                # Cache-Control 必须原样重发（RFC 7232 §4.1），否则缓存会丢掉状态。
                self.send_response(304)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("ETag", etag)
                self.end_headers()
                return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # no-cache 而不是 no-store：允许存储，但每次使用前必须回源校验；配合
        # 上面的校验符，校验命中的代价是一次 304 而不是整页约 470KB。不加
        # max-age：没有它浏览器每次打开都会校验，应用更新后新版页面立即生效。
        self.send_header("Cache-Control", "no-cache")
        if etag is not None:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)
    def _read_chunked_body(self, max_bytes=MAX_PAYLOAD_BYTES):
        """Decode a Transfer-Encoding: chunked body into bytes.

        Some OpenAI-compatible clients stream large requests with chunked
        encoding instead of a Content-Length. Reading only Content-Length saw an
        empty body and answered 400 invalid JSON.
        """
        chunks = []
        total = 0
        while True:
            line = self.rfile.readline(65536)
            if not line:
                break
            size_field = line.split(b";", 1)[0].strip()
            if not size_field:
                continue
            try:
                size = int(size_field, 16)
            except ValueError:
                raise BadJSON()
            if size == 0:
                # Consume optional trailers up to the terminating blank line.
                while True:
                    trailer = self.rfile.readline(65536)
                    if not trailer or trailer in (b"\r\n", b"\n"):
                        break
                break
            total += size
            if total > max_bytes:
                # Keep draining so the connection stays aligned, then refuse.
                self._drain_chunked_body()
                raise BodyTooLarge(total)
            remaining = size
            while remaining > 0:
                data = self.rfile.read(min(remaining, 65536))
                if not data:
                    raise BadJSON()
                chunks.append(data)
                remaining -= len(data)
            self.rfile.read(2)  # CRLF after the chunk data
        self._body_consumed = True
        return b"".join(chunks)
    def _read_payload(self, max_bytes=MAX_PAYLOAD_BYTES, allow_list=False):
        """Parse the request body into a dict (or a list when allow_list).
        Raises BodyTooLarge / BadJSON so every caller handles both cases the
        same way instead of each remembering to check for None.
        """
        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        try:
            if "chunked" in transfer_encoding:
                raw_bytes = self._read_chunked_body(max_bytes=max_bytes)
                data = json.loads(raw_bytes.decode("utf-8", "replace") or "{}")
                if isinstance(data, dict):
                    return data
                if allow_list and isinstance(data, list):
                    return data
                return {}
            length = int(self.headers.get("Content-Length") or 0)
        except (BodyTooLarge, BadJSON):
            raise
        except Exception:
            raise BadJSON()
        if length > max_bytes:
            raise BodyTooLarge(length)
        if length < 0:
            raise BadJSON()
        try:
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            # Mark the body as taken so a later error reply does not try to
            # drain the same bytes again (that read would block forever).
            self._body_consumed = True
            data = json.loads(raw or "{}")
        except Exception:
            raise BadJSON()
        if isinstance(data, dict):
            return data
        if allow_list and isinstance(data, list):
            # The account-import endpoint accepts a bare array of accounts,
            # which is the most natural shape for a hand-written file.
            return data
        return {}
    def _payload_or_error(self, allow_list=False):
        """Read the body, replying with the right error and returning None."""
        try:
            return self._read_payload(allow_list=allow_list)
        except BodyTooLarge as exc:
            message = ("payload too large (%d bytes > %d limit)"
                       % (exc.length, MAX_PAYLOAD_BYTES))
            if self._anthropic_route():
                self._anthropic_error(413, message, "invalid_request_error")
            else:
                self._error(413, message, "invalid_request_error")
            return None
        except BadJSON:
            if self._anthropic_route():
                self._anthropic_error(400, "invalid JSON body", "invalid_request_error")
            else:
                self._error(400, "invalid JSON body", "invalid_request_error")
            return None
    def _anthropic_route(self):
        """True when the request path is one of the Anthropic Messages routes.

        Errors raised before the route dispatcher runs (body read, size cap)
        have to answer in the Anthropic envelope, or a Claude Code client sees
        an OpenAI-shaped error it cannot parse.
        """
        return self.path.split("?")[0] in (
            "/v1/messages", "/messages",
            "/v1/messages/count_tokens", "/messages/count_tokens",
        )

    # ---- one-click agent integration (wb_agents) ----

    def _handle_panel(self, path):
        """Panel login, logout and the settings screen (password + API key)."""
        payload = self._payload_or_error()
        if payload is None:
            return
        if path == "/panel/login":
            peer_ip = self.client_address[0] if hasattr(self, "client_address") and self.client_address else "127.0.0.1"
            # 限流键不能直接取对端地址：反代后对端是代理本身，见 login_rate_limit_key
            client_ip = login_rate_limit_key(peer_ip, getattr(self, "headers", None))
            now = time.time()
            with _login_lock:
                _prune_login_attempts(now)
                attempts = [t for t in _login_attempts.get(client_ip, []) if now - t < 60]
                _login_attempts[client_ip] = attempts
                if len(attempts) >= 5:
                    wait_sec = int(60 - (now - attempts[0]))
                    return self._error(429, f"too many login attempts, please wait {max(1, wait_sec)}s", "rate_limit_error")
            password = str(payload.get("password") or "")
            if not wb_settings.verify_panel_password(ACCOUNTS_DIR, password):
                with _login_lock:
                    _login_attempts.setdefault(client_ip, []).append(now)
                # Small backoff delay to mitigate automated brute force
                time.sleep(0.5)
                return self._error(401, "invalid panel password", "invalid_request_error")
            with _login_lock:
                _login_attempts.pop(client_ip, None)
            token = PANEL.create()
            return self._json(200, {
                "ok": True,
                "token": token,
                "using_default_password": wb_settings.panel_password_is_default(ACCOUNTS_DIR),
            })
        if path == "/panel/logout":
            PANEL.revoke(self._panel_token())
            return self._json(200, {"ok": True})
        # Everything past this point requires an authenticated panel session.
        if not self._panel_ok():
            return self._error(401, "panel password required", "invalid_request_error")
        if path == "/panel/password":
            current = str(payload.get("current") or "")
            new = str(payload.get("new") or "")
            if not wb_settings.verify_panel_password(ACCOUNTS_DIR, current):
                return self._error(401, "current password is wrong", "invalid_request_error")
            if len(new) < 4:
                return self._error(400, "new password must be at least 4 characters", "invalid_request_error")
            wb_settings.set_panel_password(ACCOUNTS_DIR, new)
            log("panel      : password changed")
            if new != wb_settings.DEFAULT_PANEL_PASSWORD:
                # Rotating the password invalidates every other browser session.
                PANEL.revoke_all()
            token = PANEL.create()
            return self._json(200, {
                "ok": True, "token": token,
                "using_default_password":
                    new == wb_settings.DEFAULT_PANEL_PASSWORD,
            })
        return self._error(404, "not found", "invalid_request_error")

    def _route_scheduler_trigger(self, payload):
        if SCHEDULER:
            return self._json_panel(200, SCHEDULER.trigger_now())
        return self._json(200, {"ok": False, "msg": "调度器未初始化"})

    def _route_quota_scan_run(self):
        """Check the watched quota lines right now, ignoring the 6h interval.

        Still refuses anything the scanner considers off limits: a line already
        carrying a 429 window belongs to wb_window_probe, and forcing another
        request at it would only confirm what the panel already shows.
        """
        if POOL is None:
            return self._error(503, "account pool unavailable")
        try:
            import wb_quota_scan
            sent = wb_quota_scan.run_due(POOL, log=lambda msg: add_log_entry(
                "[额度查询] %s" % msg, tag="scheduler"), force=True)
        except Exception as exc:
            return self._error(500, "quota scan failed: %s" % exc,
                               "server_error")
        return self._json(200, {
            "ok": True,
            "sent": sent,
            "msg": ("已查询 %d 条配额线" % sent) if sent
                   else "没有需要查询的配额线（未勾选模型或都已到下一次查询时刻）",
        })

    def _route_scheduler_toggle(self, payload):
        if SCHEDULER:
            SCHEDULER.enabled = not SCHEDULER.enabled
            SCHEDULER.log(f"用户切换调度器状态为: {'启用' if SCHEDULER.enabled else '暂停'}")
            return self._json_panel(200, SCHEDULER.status())
        return self._json(200, {"ok": False, "msg": "调度器未初始化"})

    def _route_pricing_refresh(self, payload):
        # Fetching takes tens of seconds, so it runs on its own thread and the
        # panel polls /pricing for the outcome.
        if not PRICING:
            return self._json(200, {"ok": False, "msg": "价格刷新未运行"})
        if not wb_settings.pricing_enabled(ACCOUNTS_DIR):
            return self._json(200, {"ok": False, "msg": "价估算已关闭，请先启用"})
        threading.Thread(target=PRICING.run_once, daemon=True,
                         name="price-refresh-manual").start()
        out = PRICING.status()
        out["ok"] = True
        out["msg"] = "已开始抓取，稍候刷新查看结果"
        return self._json(200, out)

    def _route_catalog_refresh(self, payload):
        """面板「刷新模型清单」：强制向上游拉一次指定版本的目录。

        版本（realm）由面板显式传当前视图的 intl/cn：每个版本只用自己的
        代表账号与该版本的端点，绝不跨区。拉取同步执行（通常 1~3 秒；
        上游丢包时两次 10s 尝试封顶），同一版本有 30s 冷却；无论成败都
        以 200 + ok 标志回答，msg 给面板直接展示（与 /pricing/refresh
        同一条面板惯例）。
        """
        realm = str((payload or {}).get("realm") or "").strip().lower()
        return self._json(200, refresh_catalog(realm))

    def _route_logs_clear(self, payload):
        clear_logs()
        return self._json(200, {"ok": True})

    def _route_pricing_mapping(self, payload):
        """面板手填一条「hub 模型 → OpenRouter id」映射。

        写进 usage/pricing-overrides.json（运行期覆盖，不改源码里的
        OVERRIDES，也不随镜像升级丢失），随后立刻触发一次取价，让这条映射
        在几秒内生效。or_id 为空表示删除该映射。写入只认形状像 OpenRouter
        id 的值，并在快照里有该条目时予以确认（没有也接受，但要如实说明，
        因为上游随时可能刚上架而本地清单还没刷新）。
        """
        if PRICING is None:
            return self._json(200, {"ok": False, "msg": "价格刷新未运行"})
        if not wb_settings.pricing_enabled(ACCOUNTS_DIR):
            return self._json(200, {"ok": False, "msg": "价估算已关闭，请先启用"})
        model = str(payload.get("model") or "").strip()
        or_id = str(payload.get("or_id") or "").strip()
        if not model:
            return self._error(400, "model is required", "invalid_request_error")
        if or_id and not re.match(r"^[A-Za-z0-9._\-]+/[A-Za-z0-9._\-:]+$", or_id):
            return self._error(400, "or_id must look like 'vendor/model'",
                               "invalid_request_error")
        or_models, _by_norm = wb_pricing.live_index()
        known = bool(or_id) and or_id in (or_models or {})
        wb_pricing.save_runtime_override(model, or_id)
        PRICING.invalidate_gaps()
        threading.Thread(target=PRICING.run_once, daemon=True,
                         name="price-refresh-mapping").start()
        if not or_id:
            msg = "已删除 %s 的手填映射，下次取价起按自动匹配" % model
        elif known:
            msg = "已记录 %s → %s，正在重新取价" % (model, or_id)
        else:
            msg = ("已记录 %s → %s；本次快照里还没看到该条目，"
                   "取价后仍可能显示未定价" % (model, or_id))
        add_log_entry("[定价] 面板手填映射：%s → %s%s"
                      % (model, or_id or "(清除)",
                         "" if (known or not or_id) else "（快照暂无此条目）"),
                      tag="pricing")
        return self._json(200, {"ok": True, "model": model, "or_id": or_id,
                                "known": known, "msg": msg})

    def _route_realm(self, payload):
        # Changing the exit affects every key that is not realm-bound, so
        # it is an admin action: the panel session is required. GET /realm
        # stays open to API keys because it only reports the current exit.
        if not self._panel_ok():
            return self._error(403, "changing the upstream exit requires the "
                                    "panel session, not an API key",
                               "invalid_request_error")
        new_realm = payload.get("realm")
        if new_realm in ("intl", "cn"):
            save_persisted_realm(new_realm)
        return self._json(200, {"ok": True, "current": CURRENT_REALM, "persisted": True})

    def _route_accounts_daily_chat(self, payload):
        uid = payload.get("uid")
        uids = payload.get("uids")
        if uid:
            targets = [POOL.get(uid)]
        elif isinstance(uids, list):
            # 「未活跃打卡一键打卡」：面板把今天还没打过卡的国际版账号列成名单送进来，
            # 这里只按名单精确执行——已经打过的那批不再打第二遍（重复打卡不给积分，
            # 只白烧账号额度），名单外的账号也不碰。
            #
            # 空名单就是「一个都不打」：名单是调用方的明确意图，不能悄悄回落成
            # 「全部账号」——那正好和这个按钮要的效果相反。要打全部就别传 uids。
            wanted = {str(item) for item in uids if item}
            targets = [a for a in POOL.accounts
                       if a.realm == "intl" and a.task_active() and str(a.uid) in wanted]
        else:
            targets = POOL.task_accounts("intl")
        results = []
        for account in targets:
            if account is None:
                continue
            res = account.daily_chat(trigger="manual")
            # 与网页通道打卡那条一样把结果写进运行日志：msg 里带着网页通道是
            # completed 还是失败原因，面板上光看 toast 的「成功」看不出来。
            nick = account.nickname or account.uid[:8]
            log(f"[任务福利] 账号 [{nick}] 每日活跃打卡: {res.get('msg') if res.get('ok') else res.get('error')}",
                level="INFO" if res.get("ok") else "WARN", tag="tasks")
            results.append({"uid": account.uid, "nickname": account.nickname, **res})
        return self._json_panel(200, {"results": results, "accounts": account_views()})

    def _route_accounts_daily_chat_web(self, payload):
        """网页通道打卡：只建网页端会话，不发桌面端那条轻量对话。

        手动触发用。刻意不写 lastDailyChat——那是「今天已经打过卡」的闸门，
        手动补一次不该让定时巡检跳过当天的正常流程。
        """
        uid = payload.get("uid")
        if uid:
            targets = [POOL.get(uid)]
        else:
            targets = POOL.task_accounts("intl")
        results = []
        for account in targets:
            if account is None:
                continue
            res = account.daily_chat_web(trigger="manual")
            nick = account.nickname or account.uid[:8]
            log(f"[任务福利] 账号 [{nick}] 网页通道打卡: {res.get('conversation') if res.get('ok') else res.get('error')}",
                level="INFO" if res.get("ok") else "WARN", tag="tasks")
            results.append({"uid": account.uid, "nickname": account.nickname, **res})
        return self._json_panel(200, {"results": results, "accounts": account_views()})

    def _handle_responses(self, payload):
        """Serve /v1/responses by translating to chat completions upstream."""
        # A legacy `/responses/compact` call carries the summarization prompt in
        # the body, not a `compaction_trigger`, so it counts as compaction too.
        is_compact_path = self.path.split("?")[0].rstrip("/").endswith("/responses/compact")
        compaction = is_compact_path or _has_compaction_trigger(payload)
        # The gateway is stateless: it keeps no store of previous responses,
        # so it cannot replay a prior turn. Silently ignoring the field would
        # answer a follow-up as if it were a fresh conversation - the client
        # gets a normal-looking reply with the context missing. Say so instead.
        # 拒绝 namespace 工具，逼 Codex fallback 成 flat 工具清单。
        # 不这样做的话，MCP／外挂工具全部会被 app 判定为不可执行。
        if payload.get("previous_response_id") and not compaction:
            return self._error(
                400,
                "previous_response_id is not supported: this gateway does not "
                "store response state. Send the full conversation in 'input' "
                "instead, or use a stateless client.",
                "invalid_request_error")
        session_key = extract_session_key(self.headers, payload)
        # 工具可能在顶层，也可能整份在 input 的 additional_tools 条目里。回程
        # 的两处取值（custom_names 判定条目类型、request_meta 回显工具清单）
        # 必须和 responses_to_chat() 出站合并后的清单一致，否则两边看到的
        # 是不同工具集合，回程类型判错 → 客户端匹配不到执行器。
        all_tools = payload.get("tools")
        all_tools = list(all_tools) if isinstance(all_tools, list) else []
        all_tools.extend(additional_tools_from_input(payload))
        custom_names = custom_tool_names(all_tools)
        chat_req = responses_to_chat(payload, compaction=compaction)
        ns_map = chat_req.pop("_namespace_map", None)
        # Echo these back on the response object; see chat_to_response.
        request_meta = {
            "tools": all_tools,
            "tool_choice": payload.get("tool_choice", "auto"),
            "parallel_tool_calls": payload.get("parallel_tool_calls", True),
        }
        model = payload.get("model") or "deepseek-v4.1-flash"
        want_stream = bool(payload.get("stream"))
        t_start = time.time()
        fp = prompt_fingerprint(chat_req.get("messages"))
        log(
            "responses: model=%s stream=%s msgs=%d effort=%r tools=%d/%d custom_tools=%s compaction=%s key=%s"
            % (model, want_stream, len(chat_req.get("messages") or []),
               chat_req.get("reasoning_effort"),
               len(chat_req.get("tools") or []), len(all_tools),
               sorted(custom_names) or "-", compaction, key_label(self._key_id()) or "-")
        )
        try:
            explicit_realm = self._request_realm()
            req_realm = explicit_realm or CURRENT_REALM
            blocked = self._cross_realm_error(chat_req.get("model"), req_realm)
            if blocked:
                return self._error(400, blocked, "invalid_request_error")
            banned = self._banned_model_error(chat_req.get("model"))
            if banned:
                return self._error(400, banned, "invalid_request_error")
            key_blocked = self._key_model_error(chat_req.get("model"))
            if key_blocked:
                return self._error(400, key_blocked, "invalid_request_error")
            upstream, account, effort = open_upstream_with_fallback(
                chat_req, session_key=session_key, target_realm=req_realm,
                allow_realm_fallback=not explicit_realm,
                **key_open_kwargs(getattr(self, "key_entry", None)))
        except PinnedAccountError as exc:
            record_error(model, exc.status, exc.message[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=exc.account_uid, key=self._key_id())
            return self._error(exc.status, exc.message, "invalid_request_error")
        except ContentRejected as exc:
            record_error(model, 403, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._error(403, "upstream 403: %s" % (exc.detail or "content rejected"),
                               "invalid_request_error")
        except LocalNetworkError as exc:
            record_error(model, 503, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._error(503, "本机网络不可用：%s - 请检查本机网络/代理后重试"
                                    "（本次不计入账号熔断）" % (exc.detail or "DNS/路由失败"),
                               "invalid_request_error")
        except wb_accounts.TLSVerifyError as exc:
            record_error(model, 502, str(exc)[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         key=self._key_id())
            return self._error(502, str(exc), "invalid_request_error")
        except RateLimited as exc:
            t = time.time() - t_start
            record_error(model, 429, exc.detail[:200], elapsed_ms=int(t * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._rate_limited(exc)
        except urllib.error.HTTPError as exc:
            detail = read_http_error(exc, 600).decode("utf-8", "replace")
            record_error(model, exc.code, detail,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._error(exc.code, f"upstream {exc.code}: {detail}")
        except Exception as exc:
            message = str(exc)
            record_error(model, 502, message,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            if message.startswith("no usable account"):
                return self._error(503, message +
                                   " - add or enable one at the dashboard (/)")
            return self._error(502, f"upstream unreachable: {exc}")
        with upstream:
            if compaction:
                return self._responses_compaction_response(
                    upstream, model, fp, account, t_start, want_stream,
                    session_key=session_key, realm=req_realm)
            if want_stream:
                return self._responses_stream_response(
                    upstream, model, custom_names, request_meta, fp, account, t_start, ns_map,
                    base_body=chat_req, session_key=session_key, realm=req_realm,
                    effort=effort)
            return self._responses_nonstream_response(
                upstream, model, custom_names, request_meta, fp, account, t_start, ns_map,
                base_body=chat_req, session_key=session_key, realm=req_realm,
                effort=effort)

    def _responses_compaction_response(self, upstream, model, fp, account, t_start,
                                       want_stream, session_key=None, realm=None):
        """Answer Codex remote compaction v2 with one `compaction` item.

        Codex sends the full retained history plus `{"type":"compaction_trigger"}`
        and requires exactly one compaction output item in the stream; the
        provider's own compaction summary is opaque to it. The upstream is a
        plain Chat Completions API, so the gateway asks for the handoff summary
        itself (see COMPACTION_PROMPT) and hands it back in `encrypted_content`.
        Returning ordinary text here makes Codex report `got 0 compaction output
        items` and keeps the uncompacted history, which is how a long thread
        walks past the model's window.
        """
        try:
            chat_obj = aggregate_stream(upstream, model, None)
        except Exception as exc:
            record_error(model, 502, "compaction failed: %s" % exc,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=account.uid, key=self._key_id())
            return self._error(502, "upstream stream error: %s" % exc)
        choice = (chat_obj.get("choices") or [{}])[0]
        summary = str((choice.get("message") or {}).get("content") or "").strip()
        if not summary:
            record_error(model, 502, "compaction produced no summary",
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=account.uid, usage=chat_obj.get("usage"),
                         stream=False, fp=fp, outcome="failed", key=self._key_id())
            return self._error(
                502, "compaction produced no summary (the model returned no text); retry")
        wall = int((time.time() - t_start) * 1000)
        record_upstream_usage(upstream, model, chat_obj.get("usage"), stream=False,
                              elapsed_ms=wall, fp=fp, account=account.uid,
                              key=self._key_id())
        item = {
            "id": _new_id("cmp_"),
            "type": "compaction",
            "encrypted_content": _encode_compaction_summary(summary),
        }
        usage = _responses_usage(chat_obj.get("usage")) or {}
        if not want_stream:
            obj = {
                "id": _new_id("resp_"),
                "object": "response",
                "created_at": int(time.time()),
                "status": "completed",
                "model": model,
                "output": [item],
                "output_text": "",
                "metadata": {},
                "usage": usage,
            }
            return self._json(200, obj)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        resp_id = _new_id("resp_")
        seq = 0

        def ev(etype, payload):
            nonlocal seq
            seq += 1
            data = {"type": etype, "sequence_number": seq}
            data.update(payload)
            body = json.dumps(data, ensure_ascii=False)
            return ("event: " + etype + chr(10) + "data: " + body
                    + chr(10) + chr(10)).encode("utf-8")

        base = {
            "id": resp_id,
            "object": "response",
            "created_at": int(time.time()),
            "model": model,
            "output": [item],
            "output_text": "",
            "metadata": {},
        }
        try:
            self.wfile.write(ev("response.created", {"response": dict(base, status="in_progress", output=[])}))
            self.wfile.write(ev("response.in_progress", {"response": dict(base, status="in_progress", output=[])}))
            # Codex's remote-compaction collector only reads output_item.done;
            # an `added` frame for this item would hand it an empty summary.
            self.wfile.write(ev("response.output_item.done", {"output_index": 0, "item": item}))
            self.wfile.write(ev("response.completed", {
                "response": dict(base, status="completed",
                                 usage=usage or {"input_tokens": 0, "output_tokens": 0}),
            }))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def _responses_stream_response(self, upstream, model, custom_names, request_meta, fp, account, t_start, namespace_map=None, base_body=None, session_key=None, realm=None, effort=None):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        holder = {"usage": None, "custom_names": custom_names,
                  "request_meta": request_meta,
                  "namespace_map": namespace_map,
                  "base_body": base_body,
                  "base_messages": (base_body or {}).get("messages"),
                  "session_key": session_key,
                  "realm": realm}
        first_ms = None
        heartbeat = SSEHeartbeat(
            lambda data: (self.wfile.write(data), self.wfile.flush()))
        try:
            with heartbeat:
                # 一轮跑完如果模型要的是 web_search / web_fetch，就由反代
                # 执行、把结果喂回去再跑一轮。客户端从头到尾只看到一则连续的回应。
                rounds = 0
                total_usage = None
                while True:
                    holder.pop("internal_calls", None)
                    holder.pop("suppress_completion", None)
                    holder["suppress_lifecycle"] = rounds > 0
                    for frame in stream_responses_events(upstream, model, holder):
                        if first_ms is None:
                            first_ms = int((time.time() - t_start) * 1000)
                        heartbeat.write(clean_responses_frame(frame))
                    # 每一轮的 token 都是真的花掉的，记帐要加总
                    total_usage = sum_usage(total_usage, holder.get("usage"))
                    internal = holder.get("internal_calls") or []
                    if not internal:
                        break
                    rounds += 1
                    # 用完就收回工具，让模型自己收尾；这里不合成任何事件。
                    give_up = rounds > wb_webtools.MAX_WEB_ROUNDS
                    try:
                        upstream.close()
                    except Exception:
                        pass
                    upstream, account, _ = follow_up_with_tool_results(
                        internal, holder, model, session_key, t_start, drop_tools=give_up,
                        **key_open_kwargs(getattr(self, "key_entry", None)))
                if total_usage:
                    holder["usage"] = total_usage
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            wall = int((time.time() - t_start) * 1000)
            record_upstream_usage(upstream, model, holder.get("usage"), stream=True, elapsed_ms=wall,
                         ttft_ms=first_ms,
                         gen_ms=(wall - first_ms) if first_ms is not None else None,
                         fp=fp, account=account.uid,
                         outcome="client_aborted", key=self._key_id(), effort=effort)
            return
        except Exception as exc:
            wall = int((time.time() - t_start) * 1000)
            record_error(model, 502, "stream aborted: %s" % exc,
                         elapsed_ms=wall, account=account.uid,
                         usage=holder.get("usage"), stream=True,
                         ttft_ms=first_ms,
                         gen_ms=(wall - first_ms) if first_ms is not None else None,
                         fp=fp, outcome="upstream_aborted", key=self._key_id())
            try:
                self.wfile.write(responses_stream_error_frame(
                    "upstream stream aborted: %s" % exc))
                self.wfile.flush()
            except Exception:
                pass
            return
        finally:
            # 代跑多轮时 upstream 会被换掉，外层的 with 只认得最开始那一条，
            # 最后一条要在这里收掉。
            try:
                upstream.close()
            except Exception:
                pass
        wall = int((time.time() - t_start) * 1000)
        record_upstream_usage(upstream, model, holder.get("usage"), stream=True, elapsed_ms=wall,
                     ttft_ms=first_ms,
                     gen_ms=(wall - first_ms) if first_ms is not None else None,
                     fp=fp, account=account.uid, key=self._key_id(), effort=effort,
                     outcome=("failed" if holder.get("empty_reasoning") else "completed"))
        return

    def _responses_nonstream_response(self, upstream, model, custom_names, request_meta, fp, account, t_start, namespace_map=None, base_body=None, session_key=None, realm=None, effort=None):
        # 跟串流那条一样：客户端宣告 web_search / web_fetch 时由反代代跑。
        # 中间那几轮对客户端不可见，最后才组成一个 Responses 物件回传；不这样
        # 做的话 web_search 的 function_call 会直接漏给客户端，客户端只会回
        # 一句 unsupported call。
        sources = []
        rounds = 0
        # 开关关闭时不拦同名呼叫：那是客户端自己的工具。
        web_tools = web_tools_active(base_body)
        while True:
            try:
                chat_obj = aggregate_stream(upstream, model, None)
            except Exception as exc:
                record_error(model, 502, str(exc),
                             elapsed_ms=int((time.time() - t_start) * 1000),
                             account=account.uid, key=self._key_id())
                return self._error(502, f"upstream stream error: {exc}")
            calls = internal_calls_from_chat(chat_obj, web_tools=web_tools)
            if not calls:
                break
            rounds += 1
            give_up = rounds > wb_webtools.MAX_WEB_ROUNDS
            try:
                upstream.close()
            except Exception:
                pass
            holder = {"base_messages": (base_body or {}).get("messages"),
                      "base_body": base_body, "realm": realm,
                      "web_sources": sources}
            try:
                upstream, account, _ = follow_up_with_tool_results(
                    calls, holder, model, session_key, t_start, drop_tools=give_up,
                    **key_open_kwargs(getattr(self, "key_entry", None)))
            except Exception as exc:
                record_error(model, 502, "web tool follow-up failed: %s" % exc,
                             elapsed_ms=int((time.time() - t_start) * 1000),
                             account=account.uid, key=self._key_id())
                return self._error(502, "web tool follow-up failed: %s" % exc)
            sources = holder.get("web_sources") or sources
        wall = int((time.time() - t_start) * 1000)
        result = chat_to_response(chat_obj, model, custom_names, request_meta, namespace_map,
                                  sources=sources)
        if reasoning_only_response(result):
            record_error(model, 502, "upstream returned reasoning without output text",
                         elapsed_ms=wall, account=account.uid,
                         usage=chat_obj.get("usage"), stream=False,
                         fp=fp, outcome="empty_reasoning", key=self._key_id())
            return self._error(
                502, "upstream returned reasoning without output text; retry")
        record_upstream_usage(upstream, model, chat_obj.get("usage"), stream=False, elapsed_ms=wall, fp=fp,
                     account=account.uid, key=self._key_id(), effort=effort)
        return self._json(200, result)

    def _handle_messages_count_tokens(self, payload):
        """Best-effort Anthropic count_tokens endpoint.

        The WorkBuddy upstream has no Anthropic tokenizer. This estimate uses
        the same CJK-aware estimator as the gateway's own accounting and keeps
        the native response shape; it is intentionally not presented as an
        official model-token count.
        """
        try:
            chat = messages_to_chat(payload)
        except ValueError as exc:
            return self._anthropic_error(400, str(exc), "invalid_request_error")
        return self._json(200, {"input_tokens": _anthropic_estimate_chat_tokens(chat)})

    def _handle_messages(self, payload):
        """Serve an Anthropic Messages request through the chat pipeline."""
        try:
            chat_req = messages_to_chat(payload)
        except ValueError as exc:
            return self._anthropic_error(400, str(exc), "invalid_request_error")
        model = chat_req.get("model") or "unknown"
        want_stream = bool(chat_req.get("stream"))
        session_key = extract_session_key(self.headers, chat_req)
        t_start = time.time()
        fp = prompt_fingerprint(chat_req.get("messages"))
        try:
            client_ip = self.client_address[0] if self.client_address else ""
        except Exception:
            client_ip = ""
        log("messages: model=%s stream=%s msgs=%d effort=%r tools=%d key=%s"
            % (model, want_stream, len(chat_req.get("messages") or []),
               chat_req.get("reasoning_effort"), len(chat_req.get("tools") or []),
               key_label(self._key_id()) or "-"))
        try:
            explicit_realm = self._request_realm()
            req_realm = explicit_realm or CURRENT_REALM
            blocked = self._cross_realm_error(chat_req.get("model"), req_realm)
            if blocked:
                return self._anthropic_error(400, blocked, "invalid_request_error")
            banned = self._banned_model_error(chat_req.get("model"))
            if banned:
                return self._anthropic_error(400, banned, "invalid_request_error")
            key_blocked = self._key_model_error(chat_req.get("model"))
            if key_blocked:
                return self._anthropic_error(400, key_blocked, "invalid_request_error")
            upstream, account, effort = open_upstream_with_fallback(
                chat_req, session_key=session_key, target_realm=req_realm,
                allow_realm_fallback=not explicit_realm,
                **key_open_kwargs(getattr(self, "key_entry", None)))
        except PinnedAccountError as exc:
            record_error(model, exc.status, exc.message[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=exc.account_uid, key=self._key_id())
            return self._anthropic_error(exc.status, exc.message)
        except ContentRejected as exc:
            record_error(model, 403, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._anthropic_error(403, "upstream 403: %s" %
                                          (exc.detail or "content rejected"),
                                          "permission_error")
        except LocalNetworkError as exc:
            record_error(model, 503, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._anthropic_error(503, "本机网络不可用：%s - 请检查本机网络/代理后重试"
                                              "（本次不计入账号熔断）"
                                              % (exc.detail or "DNS/路由失败"),
                                         "api_error")
        except wb_accounts.TLSVerifyError as exc:
            record_error(model, 502, str(exc)[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         key=self._key_id())
            return self._anthropic_error(502, str(exc), "api_error")
        except RateLimited as exc:
            record_error(model, 429, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._anthropic_rate_limited(exc)
        except urllib.error.HTTPError as exc:
            try:
                detail = read_http_error(exc, 600).decode("utf-8", "replace")
            except Exception:
                detail = str(exc)
            record_error(model, exc.code, detail,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._anthropic_error(exc.code,
                                         "upstream %s: %s" % (exc.code, detail))
        except Exception as exc:
            message = str(exc)
            low = message.lower()
            status = 503 if ("no account" in low or "no enabled account" in low) else 502
            record_error(model, status, message,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            if status == 503:
                message += " - add or enable one at the dashboard (/)"
            return self._anthropic_error(status, message)
        with upstream:
            if want_stream:
                return self._messages_stream_response(
                    upstream, model, fp, account, t_start,
                    base_body=chat_req, session_key=session_key,
                    realm=req_realm, effort=effort)
            return self._messages_nonstream_response(
                upstream, model, fp, account, t_start,
                base_body=chat_req, session_key=session_key,
                realm=req_realm, effort=effort)

    def _messages_nonstream_response(self, upstream, model, fp, account, t_start,
                                     base_body=None, session_key=None, realm=None,
                                     effort=None):
        try:
            chat_obj = aggregate_stream(upstream, model, None)
            result = chat_to_messages(chat_obj)
            wall = int((time.time() - t_start) * 1000)
            record_upstream_usage(upstream, model, chat_obj.get("usage"), stream=False, elapsed_ms=wall,
                         fp=fp, account=account.uid, key=self._key_id(), effort=effort)
            return self._json(200, result)
        except Exception as exc:
            wall = int((time.time() - t_start) * 1000)
            record_error(model, 502, "messages upstream error: %s" % exc,
                         elapsed_ms=wall, account=account.uid, key=self._key_id())
            return self._anthropic_error(502, "upstream stream error: %s" % exc)
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    def _messages_stream_response(self, upstream, model, fp, account, t_start,
                                  base_body=None, session_key=None, realm=None,
                                  session_meta=None, effort=None):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        holder = {"usage": None}
        first_ms = None
        heartbeat = SSEHeartbeat(
            lambda data: (self.wfile.write(data), self.wfile.flush()))
        try:
            with heartbeat:
                for frame in stream_messages_events(upstream, model, holder):
                    if first_ms is None:
                        first_ms = int((time.time() - t_start) * 1000)
                    heartbeat.write(frame)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            wall = int((time.time() - t_start) * 1000)
            record_upstream_usage(upstream, model, holder.get("usage"), stream=True, elapsed_ms=wall,
                         ttft_ms=first_ms,
                         gen_ms=(wall - first_ms) if first_ms is not None else None,
                         fp=fp, account=account.uid,
                         outcome="client_aborted", key=self._key_id(), effort=effort)
            return
        except Exception as exc:
            wall = int((time.time() - t_start) * 1000)
            record_error(model, 502, "messages stream aborted: %s" % exc,
                         elapsed_ms=wall, account=account.uid,
                         usage=holder.get("usage"), stream=True,
                         ttft_ms=first_ms, fp=fp, outcome="upstream_aborted",
                         key=self._key_id())
            try:
                heartbeat.write(anthropic_sse_frame("error", {
                    "error": {"type": "api_error", "message": str(exc)}}))
            except Exception:
                pass
            return
        finally:
            try:
                upstream.close()
            except Exception:
                pass
        wall = int((time.time() - t_start) * 1000)
        record_upstream_usage(upstream, model, holder.get("usage"), stream=True, elapsed_ms=wall,
                     ttft_ms=first_ms,
                     gen_ms=(wall - first_ms) if first_ms is not None else None,
                     fp=fp, account=account.uid, key=self._key_id(), effort=effort)
        return

    def do_POST(self):
        if not self._apply_pinned_route():
            return
        path = self.path.split("?")[0]
        if path in BODYLESS_PANEL_POSTS:
            # 这一组路由不解析 body，但面板按钮确实会发一个 `{}`：不在这里排空，
            # 残留字节就会成为同一条连接上下一个请求的请求行（见 BODYLESS_PANEL_POSTS
            # 的注释）。顺序与 _error() 相同：先应对 `Expect: 100-continue`，否则客户端
            # 根本不发 body，排空会一直等一个永远不会到的载荷。
            self._handle_expect_continue()
            self._discard_body()
        if path == "/console/toggle":
            # Panel-only management write: 收起/唤出本进程自己的控制台窗口，
            # 与设置、定价那几个写入同一个门。按钮带 `{}` body，
            # 路由内负责排空（见 _route_console_toggle）。
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            return self._route_console_toggle()
        if path == "/shutdown":
            # 面板顶栏的「关闭服务」：优雅退出（先回响应再 os._exit），
            # 端口随进程释放；再次启动走原来的启动脚本。
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            return self._route_shutdown()
        if path == "/settings/save":
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            return self._handle_settings_save()
        if path == "/updates/check":
            # Panel-only management write, like the pricing ones. No body to
            # read: the action takes no parameters, so a button that posts
            # nothing must not be answered with "invalid JSON body".
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            return self._route_update_check()
        if path == "/updates/install":
            # The panel session can only enqueue a version tag. The separate
            # root-owned updater rechecks the fixed repository and release
            # assets before it writes any managed application file.
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            return self._route_update_install()
        if path == "/updates/rollback":
            # Same gate as the install button: the panel session may only queue
            # a version that already has a complete local backup, and the
            # executor re-checks everything before touching a file.
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            payload = self._payload_or_error()
            if payload is None:
                return
            return self._route_update_rollback(payload)
        if path in ("/pricing/refresh", "/pricing/mapping"):
            # Panel-only management writes (the pricing table is the panel's
            # own view of the estimate). Registered here because the generic
            # dispatcher below only routes chat and account paths - POST
            # /pricing/refresh used to 404, which made the panel's "立即取价"
            # button fail silently behind its toast.
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            payload = self._payload_or_error()
            if payload is None:
                return
            if path == "/pricing/mapping":
                return self._route_pricing_mapping(payload)
            return self._route_pricing_refresh(payload)
        if path == "/catalog/refresh":
            # Panel-only management write, like the pricing ones: it spends
            # one upstream /v3/config call and republishes that realm's
            # catalogue, so it answers to the panel session rather than to
            # an API key.
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            payload = self._payload_or_error()
            if payload is None:
                return
            return self._route_catalog_refresh(payload)
        if path == "/quota-scan/run":
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            return self._route_quota_scan_run()
        if path.startswith("/proxy/"):
            if not self._panel_ok():
                return self._error(
                    401, "panel password required", "invalid_request_error"
                )
            payload = self._payload_or_error()
            if payload is None:
                return
            return self._handle_proxy_slots(path, payload)
        if path in ("/agents/apply", "/agents/restore",
                    "/agents/model-selection"):
            if not self._panel_ok():
                return self._error(
                    401, "panel password required", "invalid_request_error"
                )
            payload = self._payload_or_error()
            if payload is None:
                return
            # 勾选清单只是数据、不碰本机配置目录，所以远程看板也能保存；
            # apply / restore 改的是网关所在机器的配置文件，只允许本机操作。
            if path == "/agents/model-selection":
                return self._handle_agents_model_selection(payload)
            if not self._agents_client_allowed():
                return self._error(
                    403,
                    "agent config is only available from the machine running the gateway",
                    "invalid_request_error",
                )
            if path == "/agents/apply":
                return self._handle_agents_apply(payload)
            return self._handle_agents_restore(payload)
        if path in ("/panel/login", "/panel/logout", "/panel/password"):
            return self._handle_panel(path)
        if self._is_panel_route(path) and not self._panel_ok():
            return self._error(401, "panel password required", "invalid_request_error")
        is_messages_route = path in (
            "/v1/messages", "/messages",
            "/v1/messages/count_tokens", "/messages/count_tokens",
        )
        is_account_route = (
            path.startswith("/accounts/")
            or path == "/realm"
            or path.startswith("/tasks")
            or path.startswith("/scheduler")
            or path.startswith("/logs")
        )
        if not is_account_route and not is_messages_route and path not in (
                "/v1/chat/completions", "/chat/completions",
                "/v1/completions", "/completions",
                "/v1/responses", "/responses",
                "/v1/responses/compact", "/responses/compact"):
            return self._error(404, "not found", "invalid_request_error")
        if is_messages_route:
            if not self._key_ok():
                return self._anthropic_error(
                    401, "missing or invalid API key", "authentication_error")
        elif not self._authorized():
            return
        # Fail closed until the Anthropic-to-Chat bridge has verified prompt-cache
        # parity. Reject before forwarding or recording any paid usage.
        if is_messages_route and not _anthropic_messages_enabled():
            return self._anthropic_error(
                400,
                "Anthropic Messages is disabled by default: this gateway "
                "translates requests to Chat Completions and ignores "
                "cache_control. This can cause excessive credit consumption. "
                "Enable it in the Settings panel or set WB_ENABLE_ANTHROPIC_MESSAGES=1 "
                "to opt in at your own risk (see issue #367).",
                "invalid_request_error")
        payload = self._payload_or_error(allow_list=(path == "/accounts/import"))
        if payload is None:
            return
        if is_account_route:
            return self._handle_accounts(path, payload)
        try:
            aliases = wb_settings.model_aliases(ACCOUNTS_DIR)
        except Exception:
            aliases = {}
        _apply_model_aliases(payload, aliases)
        _apply_background_model_alias(payload)
        # Check per-key concurrency limit (#359)
        key_ok, cur_key_conc, max_key_conc = _acquire_key_slot(self.key_entry)
        if not key_ok:
            key_name = (self.key_entry or {}).get("name") or self._key_id() or "当前 Key"
            message = ("Rate limit reached: API key '%s' reached its concurrency limit (%d in flight); retry shortly"
                       % (key_name, max_key_conc))
            record_error(payload.get("model") or "unknown", 429, message,
                         stream=payload.get("stream"), key=self._key_id())
            if is_messages_route:
                return self._anthropic_error(429, message, "rate_limit_error")
            return self._error(429, message, "rate_limit_error")

        try:
            if is_messages_route:
                if path.endswith("/count_tokens"):
                    return self._handle_messages_count_tokens(payload)
                if not _chat_slots.acquire(timeout=CHAT_SLOT_WAIT_SECONDS):
                    message = ("gateway is at its concurrent chat limit "
                               "(%d in flight); retry shortly" % MAX_CONCURRENT_CHAT)
                    record_error(payload.get("model") or "unknown", 503, message,
                                 stream=payload.get("stream"), key=self._key_id())
                    return self._anthropic_error(503, message, "overloaded_error")
                try:
                    return self._handle_messages(payload)
                finally:
                    _chat_slots.release()
            # Both OpenAI-shaped routes below can hold a thread for up to 600s.
            # Take a slot for the duration; release it in finally so every early
            # return (including client disconnects) gives the slot back.
            if not _chat_slots.acquire(timeout=CHAT_SLOT_WAIT_SECONDS):
                return self._error(503, "gateway is at its concurrent chat limit "
                                        "(%d in flight); retry shortly" % MAX_CONCURRENT_CHAT)
            try:
                return self._dispatch_chat_post(path, payload)
            finally:
                _chat_slots.release()
        finally:
            _release_key_slot(self.key_entry)

    def _dispatch_chat_post(self, path, payload):
        # 先挡背景请求：Codex 自己发的（记忆整理／环境建议／自动复核）
        # 不算「使用者实际使用」，一律本地拒绝，不碰上游。
        if BLOCK_BACKGROUND_REQUESTS:
            reason = background_request_reason(payload)
            if reason:
                try:
                    log("background request blocked: model=%s trigger=(%s)"
                        % (payload.get("model"), reason), level="INFO")
                except Exception:
                    pass
                return self._error(400, background_request_message(reason),
                                   "invalid_request_error")
        if path in ("/v1/responses", "/responses",
                    "/v1/responses/compact", "/responses/compact"):
            return self._handle_responses(payload)
        # Diagnostics: what the client actually asked for, and what we forward.
        # Only the knobs that change behaviour are logged - never message text.
        forwarded = build_upstream_body(payload)
        given = client_effort_of(payload) or payload.get("reasoning") \
            or payload.get("thinking") or payload.get("enable_thinking")
        log(
            "chat: model=%s client_effort=%r -> upstream_effort=%r stream=%s msgs=%d key=%s"
            % (
                payload.get("model"),
                given,
                upstream_effort_of(forwarded, payload.get("model")),
                bool(payload.get("stream")),
                len(forwarded.get("messages") or []),
                key_label(self._key_id()) or "-",
            )
        )
        session_key = extract_session_key(self.headers, payload)
        fp = prompt_fingerprint(forwarded.get("messages"))
        want_stream = bool(payload.get("stream"))
        model = payload.get("model") or "hy4-preview"
        t_start = time.time()
        try:
            explicit_realm = self._request_realm()
            req_realm = explicit_realm or CURRENT_REALM
            blocked = self._cross_realm_error(payload.get("model"), req_realm)
            if blocked:
                return self._error(400, blocked, "invalid_request_error")
            banned = self._banned_model_error(payload.get("model"))
            if banned:
                return self._error(400, banned, "invalid_request_error")
            key_blocked = self._key_model_error(payload.get("model"))
            if key_blocked:
                return self._error(400, key_blocked, "invalid_request_error")
            upstream, account, effort = open_upstream_with_fallback(
                payload, session_key=session_key, target_realm=req_realm,
                allow_realm_fallback=not explicit_realm,
                prebuilt_body=forwarded, defer_egress_success=True,
                **key_open_kwargs(getattr(self, "key_entry", None)))
        except PinnedAccountError as exc:
            record_error(model, exc.status, exc.message[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=exc.account_uid, key=self._key_id())
            return self._error(exc.status, exc.message, "invalid_request_error")
        except ContentRejected as exc:
            record_error(model, 403, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._error(403, "upstream 403: %s" % (exc.detail or "content rejected"),
                               "invalid_request_error")
        except LocalNetworkError as exc:
            record_error(model, 503, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._error(503, "本机网络不可用：%s - 请检查本机网络/代理后重试"
                                    "（本次不计入账号熔断）" % (exc.detail or "DNS/路由失败"),
                               "invalid_request_error")
        except wb_accounts.TLSVerifyError as exc:
            record_error(model, 502, str(exc)[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         key=self._key_id())
            return self._error(502, str(exc), "invalid_request_error")
        except RateLimited as exc:
            record_error(model, 429, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._rate_limited(exc)
        except urllib.error.HTTPError as exc:
            detail = read_http_error(exc, 600).decode("utf-8", "replace")
            record_error(model, exc.code, detail,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._error(exc.code, f"upstream {exc.code}: {detail}")
        except Exception as exc:
            message = str(exc)
            record_error(model, 502, message, elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            if message.startswith("no usable account"):
                # Only a genuinely empty/cooling pool is a 503. A throttled model
                # is reported as 429 by _rate_limited above instead.
                return self._error(503, message +
                                   " - add or enable one at the dashboard (/)")
            return self._error(502, f"upstream unreachable: {exc}")
        with upstream:
            def reopen_stream():
                # Same account, same egress: only the TCP connection is new.
                return open_upstream(
                    payload, session_key=session_key, target_realm=req_realm,
                    prebuilt_body=forwarded, pin_account=account,
                    defer_egress_success=True,
                    **key_open_kwargs(getattr(self, "key_entry", None)))
            if want_stream:
                return self._chat_stream_response(
                    upstream, model, fp, account, t_start, effort=effort,
                    reopen=reopen_stream)
            return self._chat_nonstream_response(
                upstream, model, fp, account, t_start, effort=effort)

    def _chat_stream_response(self, upstream, model, fp, account, t_start, effort=None,
                              reopen=None, headers_sent=False, retried=False):
            if not headers_sent:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                if cors_origin_allowed(self.path):
                    self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
            # A retry reuses the headers already on the wire; the client has not
            # seen a single byte, so a fresh chunk id is safe.
            stream_chunk_id = f"chatcmpl-wb-{uuid.uuid4().hex[:16]}"
            emitted = False
            last_usage = None
            first_ms = None
            streamed_text = []
            leak_filter = LeakedToolCallStreamFilter()
            synthesized_calls = False
            pending_finish = None
            # 写出缓冲：一个读取块里的事件合成一次 write。
            # wfile 是 wbufsize=0 的**无缓冲**文件（StreamRequestHandler 默认），
            # 每个 chunk 直接落成一次 sendall —— 一次 syscall 加一次 GIL 交还。
            # 真机 10 秒 5043 次 sendto，对应约一万次 futex，占全部 futex 的一成。
            # 攒到「下一个可能阻塞的读之前」再写，不改变任何延迟：块边界就是
            # socket 里当前已经有的全部字节（见 iter_sse_lines 的 before_read）。
            out_buf = []
            heartbeat = SSEHeartbeat(
                lambda data: (self.wfile.write(data), self.wfile.flush()))
            # Egress accounting is settled when the read finishes, not when the
            # headers arrive: a stream that dies mid-body must leave a streak
            # behind (issue #522). retried means this request already had a
            # failed read; a successful retry does not erase that signal.
            egress_on = wb_settings.egress_backoff_enabled(ACCOUNTS_DIR)
            egress_key = None
            if egress_on:
                egress_key = wb_upstream_pool.egress_key(
                    account.chat_base_url() + CHAT_PATH, account.proxy)

            def flush_out():
                if out_buf:
                    heartbeat.write(b"".join(out_buf))
                    del out_buf[:]

            def write_chunk(payload):
                nonlocal emitted, first_ms
                if first_ms is None:
                    first_ms = int((time.time() - t_start) * 1000)
                emitted = True
                out_buf.append(f"data: {payload}\n\n".encode("utf-8"))

            def release(actions):
                nonlocal synthesized_calls
                for action in actions:
                    if action[0] == "text":
                        if action[1]:
                            write_chunk(chat_text_chunk(model, action[1], chunk_id=stream_chunk_id))
                    else:
                        synthesized_calls = True
                        for payload in chat_tool_call_chunks(model, action[1], chunk_id=stream_chunk_id):
                            write_chunk(payload)

            def before_read():
                flush_out()

            try:
                with heartbeat:
                    for line in iter_sse_lines(upstream, before_read=before_read):
                        data = strip_data_prefix(line.decode("utf-8", "replace"))
                        if not data or data == "[DONE]" or data.startswith(":"):
                            continue
                        maybe = None
                        try:
                            maybe = json.loads(data)
                        except Exception:
                            maybe = None
                        if isinstance(maybe, dict):
                            u = maybe.get("usage")
                            if u:
                                if last_usage is None or (u.get("total_tokens") or 0) >= (last_usage.get("total_tokens") or 0):
                                    last_usage = u
                                # Usage belongs in a trailing choices-less frame (the
                                # include_usage contract), not on a chunk that also
                                # carries choices: strict clients and downstream
                                # gateways reject the in-band block, and the stream
                                # then ends without billable usage. Re-emitted once
                                # below, after the finish frame.
                                maybe.pop("usage", None)
                            for ch in (maybe.get("choices") or []):
                                delta = ch.get("delta") or {}
                                if delta.get("reasoning_content"):
                                    streamed_text.append(delta["reasoning_content"])
                                piece = delta.get("content")
                                if piece:
                                    streamed_text.append(piece)
                                    # Hold the text back: it may be a call the model
                                    # printed instead of emitting, and a client that
                                    # reads it as prose ends the turn with nothing to
                                    # run ("it keeps stopping by itself").
                                    delta.pop("content", None)
                                    release(leak_filter.feed(piece))
                                if ch.get("finish_reason"):
                                    # Hold the terminator too: whether this turn ends
                                    # in tool_calls is only known once the text has
                                    # been flushed at end of stream.
                                    pending_finish = ch["finish_reason"]
                                    ch["finish_reason"] = None
                        # One response must carry one id. Upstream frames keep the
                        # upstream id while the text frames this gateway rewrites
                        # carry its own `chatcmpl-wb-*` one, so a single reply went
                        # out as two interleaved streams. Clients that key stream
                        # state by id then see the reasoning half (upstream id,
                        # never a finish_reason) and the answer half separately,
                        # and the turn hangs on "still generating" forever even
                        # though both halves arrived. Normalise before emitting.
                        if isinstance(maybe, dict) and maybe.get("id"):
                            maybe["id"] = stream_chunk_id
                        cleaned = clean_chunk(
                            json.dumps(maybe, ensure_ascii=False)
                            if isinstance(maybe, dict) else data)
                        if not cleaned:
                            continue
                        write_chunk(cleaned)
                    release(leak_filter.flush())
                    if synthesized_calls or pending_finish is not None:
                        write_chunk(chat_finish_chunk(
                            model, "tool_calls" if synthesized_calls else pending_finish, chunk_id=stream_chunk_id))
                    if last_usage is not None:
                        write_chunk(chat_usage_chunk(
                            model, last_usage, chunk_id=stream_chunk_id))
                    flush_out()
                    if not emitted:
                        err = json.dumps({"error": {
                            "message": "empty upstream stream",
                            "type": "server_error"}})
                        heartbeat.write(f"data: {err}\n\n".encode("utf-8"))
                    heartbeat.write(b"data: [DONE]\n\n")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # Client hung up; still account for what upstream produced.
                wall = int((time.time() - t_start) * 1000)
                record_upstream_usage(upstream, model, last_usage, stream=True,
                             elapsed_ms=wall, ttft_ms=first_ms,
                             gen_ms=(wall - first_ms) if first_ms is not None else None,
                             fp=fp, account=account.uid,
                             outcome="client_aborted", key=self._key_id(), effort=effort)
                return
            except Exception as exc:
                # Upstream quit mid-stream (timeout, incomplete read, ...).
                # The client would otherwise get a truncated stream with no
                # terminal marker, and the traceback reached the HTTP layer.
                if egress_on and egress_key:
                    # The egress did not finish the read; record it here instead
                    # of at header time so the streak can actually grow.
                    wb_egress.note_transient(egress_key)
                if not emitted and reopen is not None:
                    # Nothing reached the client yet, so a transparent retry on
                    # the same account/egress looks like a TCP reconnect. One
                    # retry only; the recursive call reuses the sent headers.
                    try:
                        upstream.close()
                    except Exception:
                        pass
                    new_upstream = None
                    try:
                        new_upstream, new_account, new_effort = reopen()
                    except Exception:
                        new_upstream = None
                    if new_upstream is not None:
                        try:
                            return self._chat_stream_response(
                                new_upstream, model, fp, new_account, t_start,
                                effort=new_effort, reopen=None,
                                headers_sent=True, retried=True)
                        finally:
                            try:
                                new_upstream.close()
                            except Exception:
                                pass
                wall = int((time.time() - t_start) * 1000)
                record_error(model, 502, "stream aborted: %s" % exc,
                             elapsed_ms=wall, account=account.uid,
                             usage=last_usage, stream=True, ttft_ms=first_ms,
                             gen_ms=(wall - first_ms) if first_ms is not None else None,
                             fp=fp, outcome="upstream_aborted", key=self._key_id())
                try:
                    release(leak_filter.flush())
                    if out_buf:
                        self.wfile.write(b"".join(out_buf))
                        self.wfile.flush()
                        del out_buf[:]
                    self.wfile.write(chat_stream_error_frame(
                        "upstream stream aborted: %s" % exc))
                    self.wfile.flush()
                except Exception:
                    pass
                return
            if egress_on and egress_key and emitted and not retried:
                # A full read (not a retried one) ends the streak.
                wb_egress.note_success(egress_key)
            wall = int((time.time() - t_start) * 1000)
            if last_usage is None or (last_usage.get("total_tokens") or 0) == 0:
                full_s = "".join(streamed_text)
                if full_s:
                    comp = estimate_tokens(full_s)
                    last_usage = {
                        "prompt_tokens": max(1, comp // 2),
                        "completion_tokens": comp,
                        "total_tokens": max(1, comp // 2) + comp,
                        "completion_tokens_details": {"reasoning_tokens": 0},
                        "prompt_tokens_details": {"cached_tokens": 0},
                    }
            outcome = "failed" if pending_finish == "error" else "completed"
            if outcome == "failed":
                record_error(getattr(upstream, "_wb_billed_model", model), 502, "upstream stream finished with error",
                             elapsed_ms=wall, account=account.uid,
                             usage=last_usage, stream=True, ttft_ms=first_ms,
                             gen_ms=(wall - first_ms) if first_ms is not None else None,
                             fp=fp, outcome="failed", key=self._key_id())
            else:
                record_upstream_usage(upstream, model, last_usage, stream=True,
                             elapsed_ms=wall, ttft_ms=first_ms,
                             gen_ms=(wall - first_ms) if first_ms is not None else None,
                             fp=fp, account=account.uid, key=self._key_id(), effort=effort)
            return

    def _chat_nonstream_response(self, upstream, model, fp, account, t_start, effort=None):
        egress_on = wb_settings.egress_backoff_enabled(ACCOUNTS_DIR)
        egress_key = None
        if egress_on:
            egress_key = wb_upstream_pool.egress_key(
                account.chat_base_url() + CHAT_PATH, account.proxy)
        try:
            result = aggregate_stream(upstream, model, None)
        except Exception as exc:
            if egress_on and egress_key:
                wb_egress.note_transient(egress_key)
            record_error(model, 502, str(exc), elapsed_ms=int((time.time() - t_start) * 1000),
                         account=account.uid, key=self._key_id())
            return self._error(502, f"upstream stream error: {exc}")
        if egress_on and egress_key:
            wb_egress.note_success(egress_key)
        wall = int((time.time() - t_start) * 1000)
        first_at = result.get("first_chunk_at")
        # Measured from request arrival so streaming and non-streaming are comparable.
        first_ms = int((first_at - t_start) * 1000) if first_at else None
        record_upstream_usage(upstream, model, result.get("usage"), stream=False,
                     elapsed_ms=wall, ttft_ms=first_ms,
                     gen_ms=(wall - first_ms) if first_ms is not None else None,
                     fp=fp, account=account.uid, key=self._key_id(), effort=effort)
        return self._json(200, result)


def running_version():
    """The version this process reports, e.g. "1.6.19".

    tests/_test_release_engineering.py pins the two version literals and asserts
    they agree, so the update checker reads the running one from the handler
    instead of becoming a third copy that could drift away from both.
    """
    return Handler.server_version.split("/", 1)[-1]

def main():
    args = _parse_cli_args()
    _apply_cli_overrides(args)
    if args.probe_only:
        # 启动脚本先在本机窗口里跑一次这个检查再拉起了。静默模式下服务进程是
        # pythonw，stdout/stderr 全指向空设备，"已有一个反代在运行、所以什么都不
        # 用起"这句决定用户永远看不到，双击只表现为"启动失败"。
        # 退出码就是给 cmd.exe 的判断条件：0 = 已有实例（或端口被别的服务占着，
        # 那条错误由 _probe_running_instance 自己打印），3 = 端口是空的，可以起。
        raise SystemExit(0 if _probe_running_instance(args) else 3)
    if _probe_running_instance(args):
        return
    api_key_generated = _bootstrap_runtime(args)
    if _report_first_run(args):
        return
    _log_startup_summary(args, api_key_generated)
    _serve_forever(args)

def _parse_cli_args():
    ap = argparse.ArgumentParser(description="WorkBuddy (workbuddy.ai) -> OpenAI-compatible proxy")
    ap.add_argument("--info", help="path to the WorkBuddy *.info credential file")
    ap.add_argument("--host", default=os.environ.get("HOST") or "127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT") or "8788"))
    ap.add_argument("--lan", action="store_true",
                    help="listen on every interface so other devices on the LAN can "
                         "reach it (implies --host 0.0.0.0 and forces an api key)")
    ap.add_argument("--api-key", default=os.environ.get("API_KEY") or os.environ.get("WB_PROXY_KEY") or None,
                    help="require this bearer token on /v1/* (optional)")
    ap.add_argument("--system-prompt", default=DEFAULT_SYSTEM_PROMPT,
                    help="system message injected when the request has none (required upstream)")
    ap.add_argument("--user-agent", default=None,
                    help="override the upstream User-Agent (default: mirror the official "
                         "WorkBuddy AI client)")
    ap.add_argument("--usage-dir", default=None,
                    help="where to store usage.jsonl (default: ./usage)")
    ap.add_argument("--accounts-dir", default=os.environ.get("ACCOUNTS_DIR") or None,
                    help="where the per-account credential files live (default: ./accounts)")
    ap.add_argument("--import-desktop", action="store_true",
                    help="import the desktop app credential as an account, then exit")
    ap.add_argument("--panel-password", default=None,
                    help="set the web panel password on startup (default: admin)")
    ap.add_argument("--probe-only", action="store_true",
                    help="only check whether a proxy already listens on the port and "
                         "print what the launcher should say, then exit "
                         "(0 = one is running, 3 = the port is free)")
    args = ap.parse_args()
    return args

def _apply_cli_overrides(args):
    global USAGE_DIR, USAGE_LOG, LIMIT_EVENTS_FILE, ESTIMATE_DAILY_FILE, \
        RECENT_INDEX_FILE
    # LAN mode binds every interface. The key is generated below, once
    # ACCOUNTS_DIR is resolved, so it can be persisted and reused.
    if args.lan and args.host == "127.0.0.1":
        args.host = "0.0.0.0"
    if args.user_agent:
        wb_accounts.USER_AGENT = args.user_agent.strip()
        log("user-agent : %s (override)" % wb_accounts.USER_AGENT)
    if args.usage_dir:
        USAGE_DIR = os.path.abspath(args.usage_dir)
        USAGE_LOG = os.path.join(USAGE_DIR, "usage.jsonl")
        # 事件日志（remaining 预算样本的来源）必须跟着 --usage-dir 走：这个
        # 常量在 import 时按默认目录算过，漏掉它真机上会一直写 ENOENT
        # （2026-10-10 实测 /usr/lib/workbuddy2api/usage/limit-events.jsonl）。
        LIMIT_EVENTS_FILE = os.path.join(USAGE_DIR, "limit-events.jsonl")
        # 按天归档的估算数据同理（少了它，跨天归档会一直写默认目录/ENOENT）。
        ESTIMATE_DAILY_FILE = os.path.join(USAGE_DIR, "estimate-daily.json")
        # recent 的行偏移索引同理（漏掉它，换目录后索引写到旧目录，
        # 新目录每次冷启动都要重扫一遍日志）。
        RECENT_INDEX_FILE = os.path.join(USAGE_DIR, "usage-recent-index.json")
    # 账号活动历史与用量日志同目录，并且要在任何后台线程起来之前定下来：调度器
    # 的第一轮巡检不能落在 --usage-dir 生效之前，否则记录会写进默认目录。
    wb_activity.set_data_dir(USAGE_DIR)

def _probe_running_instance(args):
    # Refuse to start a second copy. On Windows SO_REUSEADDR lets two sockets
    # bind the same port, which silently splits incoming connections between
    # them - confusing and hard to diagnose.
    try:
        probe = urllib.request.urlopen(
            f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{args.port}/health",
            timeout=2,
        )
        existing = json.loads(probe.read().decode("utf-8"))
    except Exception:
        existing = None  # nothing answering /health - let the bind below decide
    if isinstance(existing, dict):
        # Only OUR /health carries the account-pool fields ("accounts"). Other
        # services can occupy the same port and also answer /health with JSON
        # (a dev proxy, another gateway); treating that as "already running" made this
        # launcher exit silently while the port belonged to someone else - the
        # dashboard then showed a foreign UI and API calls failed with 401/404.
        foreign = existing.get("service") or "accounts" not in existing
        if foreign:
            who = existing.get("service") or "an unknown HTTP service"
            print()
            print(f"  [ERROR] port {args.port} is already taken by another program: {who}")
            print("          wb-proxy itself is NOT running - nothing was started.")
            print()
            print("  Fix: start wb-proxy on a different port, e.g.")
            print("          %s" % launcher_hint(args.port + 1))
            print(f"          python3 wb_proxy.py --port {args.port + 1}")
            print()
            print("  Check who owns the port:  %s" % port_owner_hint(args.port))
            print()
            raise SystemExit(1)
        print()
        print(f"  [已有一个反代在 {args.port} 端口运行，无需重复启动]")
        print(f"  账号: {existing.get('uid', '?')} @ {existing.get('domain', '?')}")
        print(f"  看板: http://127.0.0.1:{args.port}/")
        print()
        print("  如果要重启: 先把原来那个窗口关掉（或结束 python 进程），再运行本程序。")
        print()
        # Return True so main() stops here. A bare return gives None, which
        # main() reads as "no running copy" and it would carry on to bind the
        # port that is already taken.
        return True
    return False

def _bootstrap_runtime(args):
    global POOL, ACCOUNTS_DIR, API_KEY, SYSTEM_PROMPT
    global API_KEY_FILE_SET, SCHEDULER, LOG_STORE
    api_key_generated = False
    API_KEY = args.api_key
    SYSTEM_PROMPT = args.system_prompt
    if args.accounts_dir:
        ACCOUNTS_DIR = os.path.abspath(args.accounts_dir)
    # 日志落库（默认关闭）：在账号池之前初始化，好让「池加载失败」这类最需要
    # 事后留档的启动日志也能进库。关闭时这里只造一个 enabled=False 的空壳，
    # 不建文件、不改日志路径。
    LOG_STORE = wb_logstore.LogStore(os.path.join(USAGE_DIR, "logs.sqlite3"))
    apply_log_db_settings()
    # LAN mode must not ship a known key: the gateway spends the account's own
    # upstream quota, so a guessable default lets anyone on the network drain
    # it. Generate one on first use, persist it, and reuse it afterwards.
    if args.lan and not API_KEY:
        API_KEY, api_key_generated = wb_settings.ensure_launcher_key(ACCOUNTS_DIR)
    # A key saved from the panel wins over an auto-generated LAN key so a
    # change made in the browser survives a restart of the .bat file. An
    # explicit --api-key on the command line still takes precedence.
    global API_KEY_FILE_SET
    saved_key, key_from_panel = wb_settings.api_key_override(ACCOUNTS_DIR)
    if key_from_panel and not args.api_key:
        API_KEY = saved_key
        API_KEY_FILE_SET = True
    if args.panel_password:
        wb_settings.set_panel_password(ACCOUNTS_DIR, args.panel_password)
        log("panel      : password set from --panel-password")
    elif wb_settings.panel_password_is_default(ACCOUNTS_DIR):
        log("panel      : password is still the default 'admin' - change it in the panel")
    POOL = wb_accounts.AccountPool(ACCOUNTS_DIR, log=log)
    POOL.load()
    POOL.apply_proxy_slots()
    POOL.apply_reserve_credits()
    # WB_MAX_CONCURRENT_CHAT=auto: size the chat ceiling from the pool that was
    # just loaded. Runs before the listener exists, so no request can hold a
    # slot yet and the semaphore swap is safe.
    resize_chat_slots(POOL.count_ready())
    apply_daily_guards()
    load_persisted_realm()
    global SCHEDULER
    from wb_scheduler import Scheduler
    SCHEDULER = Scheduler(POOL)
    SCHEDULER.start()
    # 目录兜底：每天至少从上游 API 取一次（活跃使用的部署里 300s 缓存早就
    # 刷得更新，这个线程只兜长时间没人访问 / 一直吃缓存文件或内置快照）。
    start_catalog_daily()
    global PRICING
    # The policy table and its timeline live beside the usage log, so one
    # volume carries both and the request references resolve locally.
    wb_pricing.set_data_dir(USAGE_DIR)
    # The panel's pricing switches (master switch, variant inheritance) live in
    # settings.json; point the pricing side at the same file the panel writes.
    wb_pricing.set_settings_dir(ACCOUNTS_DIR)
    # The refresher always exists so /pricing can report its state; with the
    # master switch off its loop simply parks without fetching.
    PRICING = wb_pricing.PriceRefresher(
        wb_settings.pricing_refresh_minutes(ACCOUNTS_DIR))
    PRICING.start()
    # Credit balances back the expiring-credits dispatch preference, and only
    # the sign-in / daily-activity tasks used to refresh them. The refresher
    # keeps them current in the background so no request pays for a lookup.
    global CREDITS_REFRESHER
    CREDITS_REFRESHER = wb_accounts.CreditsRefresher(POOL)
    CREDITS_REFRESHER.start()
    global UPDATES
    # Release discovery only: the checker asks GitHub what the newest stable
    # release is and reports it. It never downloads, replaces or restarts
    # anything, and with the daily switch off (the default) it sends nothing.
    UPDATES = wb_updates.UpdateChecker(
        current_version=running_version(),
        settings_dir=ACCOUNTS_DIR,
        log=lambda msg: add_log_entry("[更新] %s" % msg, tag="update"),
    )
    UPDATES.start()
    return api_key_generated

def _report_first_run(args):
    if args.info:
        account = POOL.import_desktop_credential(args.info, source="file")
        log("imported account %s from %s" % (account.uid[:8], args.info))
    first_run = not POOL.accounts
    if first_run:
        # Never adopt the desktop client's login silently: just report what is
        # available and let the user import it from the dashboard.
        detected = desktop_credential_scan()
        usable = [d for d in detected if d.get("valid")]
        if usable:
            log("no accounts yet - detected %d desktop credential(s), NOT importing" % len(usable))
            for d in usable:
                log("  available: %s  %s  %s" % (
                    (d.get("uid") or "?")[:8], d.get("nickname") or "(no name)",
                    d.get("realmName") or d.get("realm")))
            log("open the dashboard and click [Scan desktop app] to import")
        else:
            log("no accounts yet - no desktop credentials found on this machine")
    if first_run and not POOL.accounts:
        # Do NOT exit here: the dashboard has to stay reachable so a new
        # account can be added through the browser login flow.
        log("still no accounts - starting anyway so you can log in via the dashboard")
    if args.import_desktop:
        for account in POOL.accounts:
            # 控制台也是「显示」：真机核验时会直接看这条输出。
            # 用文本级掩码而非值级，覆盖「老王(13812345678)」这类昵称内嵌号码的形态，
            # 与 log() 的 stderr 出口、_redact_account_names 的 nickname 规则保持一致。
            print("  %s  %s  %s" % (account.uid[:8], _mask_text(account.nickname), account.domain))
        return

def _log_startup_summary(args, api_key_generated):
    rep = current_account()
    log("accounts   : %d total, %d usable" % (len(POOL.accounts), POOL.count_ready()))
    for account in POOL.accounts:
        log("  - %s  %s  %s  %s" % (account.uid[:8], account.nickname or "(no name)",
                                    account.domain, wb_accounts._human_delta(
                                        (account.expires_at or 0) - time.time()) or "?"))
    log("store      : %s" % ACCOUNTS_DIR)
    log(f"credential : {rep.path if rep else chr(45)}")
    if os.path.exists(PRODUCT_CONFIG_CACHE):
        log(f"catalog    : {PRODUCT_CONFIG_CACHE}")
    else:
        log("catalog    : app cache not found - will use the model API instead")
    log(f"account    : {rep.uid if rep else chr(45)} @ {rep.domain if rep else chr(45)}")
    log(f"issuer     : {wb_accounts.jwt_issuer(rep.access_token) if rep else chr(45)}")
    log("realm      : %s (%s)" % (
        CURRENT_REALM,
        "www.workbuddy.ai" if CURRENT_REALM == "intl"
        else urllib.parse.urlsplit(wb_identity.CN_CHAT_BASE).netloc))
    if wb_identity.CN_CHAT_BASE != wb_identity.CN_CHAT_BASE_DEFAULT:
        log("cn upstream: %s (WB_CN_CHAT_UPSTREAM)" % wb_identity.CN_CHAT_BASE)
    elif wb_identity.CN_CHAT_BASE_IGNORED:
        log("cn upstream: ignored WB_CN_CHAT_UPSTREAM=%r (not an http(s) URL), using %s" % (
            wb_identity.CN_CHAT_BASE_IGNORED, wb_identity.CN_CHAT_BASE_DEFAULT))
    log("user-agent : %s" % wb_accounts.USER_AGENT)
    if args.host == "0.0.0.0":
        ips = local_ip_addresses() or ["<this-pc-ip>"]
        print()
        print("  " + "=" * 62)
        print("  LAN MODE - reachable from other devices")
        print()
        for ip in ips:
            print("    API       : http://%s:%s/v1" % (ip, args.port))
            print("    Dashboard : http://%s:%s/" % (ip, args.port))
        print()
        print("    API Key   : %s" % API_KEY)
        if api_key_generated:
            print("                (newly generated & saved to accounts/settings.json)")
        else:
            print("                (reused from accounts/settings.json)")
        print()
        print("    Open the dashboard (key already included):")
        print("      http://%s:%s/?key=%s" % (ips[0], args.port, API_KEY))
        print()
        print("    Clients: Base URL = the API address above, then paste the key.")
        print()
        if IS_WINDOWS:
            print("    If nothing can connect, allow python through the")
            print("    firewall: run allow-firewall.bat once as administrator.")
        elif sys.platform == "darwin":
            print("    If other devices cannot connect, allow incoming")
            print("    connections for Python (macOS asks automatically the")
            print("    first time it listens; on macOS 15+ also allow Local")
            print("    Network access for your terminal). Helper script:")
            print("    ./allow-firewall.command")
        else:
            print("    If other devices cannot connect, open the port in")
            print("    your firewall (ufw / firewalld) for the LAN subnet.")
        print("  " + "=" * 62)
        print()
        sys.stdout.flush()
    if not POOL.accounts:
        print()
        print("  " + "=" * 62)
        print("  NO ACCOUNTS YET")
        print()
        print("  Open the dashboard and click [Login new account]:")
        print("      http://127.0.0.1:%s/" % args.port)
        print()
        print("  The browser flow adds the account automatically.")
        print("  This window must stay open.")
        print("  " + "=" * 62)
        print()
        sys.stdout.flush()

class GatewayServer(ThreadingHTTPServer):
    """主服务的 HTTP 服务类：只为把 listen backlog 从 stdlib 默认的 5 提到 128。

    backlog=5 在突发并发下（面板打开一页就是 ~7 个并发请求，多标签更糟）会让
    内核来不及 accept 的 SYN 被丢弃、客户端按 1s 粒度重传，表现为一部分请求整整
    慢 1 秒（路由器实测：64 并发突发 43/64 卡 >=1s、最长 2.8s；backlog=128 后
    0/64、最长 0.18s）。listen() 在 TCPServer.__init__ 里就被调用，所以必须是
    类属性——实例化之后再改就晚了。
    """
    request_queue_size = 128

def _serve_forever(args):
    try:
        server = GatewayServer((args.host, args.port), Handler)
    except OSError as exc:
        # Port stolen between the probe above and this bind, or held by
        # something that does not answer /health: report it in plain words
        # instead of dumping a raw socketserver traceback.
        print()
        print(f"  [ERROR] failed to listen on {args.host}:{args.port} - {exc}")
        print("          the port is reserved or held by another program;")
        print("          wb-proxy did NOT start.")
        print()
        print("  Fix: stop the program holding the port, or pick another port:")
        print("          %s" % port_owner_hint(args.port))
        print("          %s" % launcher_hint(args.port + 1))
        print()
        raise SystemExit(1)
    # Only claim the address once the socket really exists, so a failed bind
    # never prints a "listening" line that contradicts the error below.
    # Report the state the request path actually enforces: the panel can turn
    # key checking on after startup, so reading API_KEY alone printed "off"
    # while every /v1 call was still being rejected with 401.
    if auth_required():
        _panel_keys = [k for k in configured_keys() if k.get("enabled")]
        _key_state = ("on (%d key(s) from the panel)" % len(_panel_keys)) if _panel_keys else "on (--api-key)"
    else:
        _key_state = "off"
    log(f"listening  : http://{args.host}:{args.port}/v1  (api key: {_key_state})")
    log(f"dashboard  : http://{args.host}:{args.port}/")
    # Keep the handler referenced for the process lifetime: SetConsoleCtrlHandler
    # stores a raw pointer, so a collected callback would crash on close.
    _ctrl_handler = install_console_close_handler()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("bye")
    finally:
        try:
            server.server_close()
        except Exception:
            pass

if __name__ == "__main__":
    # pythonw (GUI subsystem) starts with sys.stdout/sys.stderr == None; any
    # print/log would raise. Redirect to /dev/null so the process runs headless
    # with no console window (nothing in the taskbar), while the in-memory log
    # buffer the panel "运行日志" page reads stays fully intact.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    try:
        # Keep console output readable regardless of the active code page.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
