# -*- coding: utf-8 -*-
"""CodeArts OAuth 回调的会话识别：华为云把 `ticket_id` 改名成 `state`。

背景（issue #734）：授权链接里带 `ticket_id=<32 hex>`，而华为云回调回来的是
`state=<同一个 hex>.pkg`。旧实现只认 `ticket_id` / `secret`，于是每一个真实
浏览器回调都会撞上「回调缺少会话标识」，用户必须自己拼 URL 才能登录。

本套件钉住三件事：`state` 能认出会话、`ticket_id` / `secret` 两种老形态不受影响、
以及「没有会话标识或用陌生标识」时仍然严格拒绝（不猜最近的 pending 会话）。
"""
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-codearts-oauth-state-")
ACCOUNTS = os.environ["ACCOUNTS_DIR"]

import wb_codearts as C  # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


def reset_sessions():
    """清掉上一个用例留下的会话，避免串味。"""
    with C._OAUTH_LOCK:
        C._OAUTH_SESSIONS.clear()


def new_session():
    cfg = C.load_config(ACCOUNTS)
    started = C.oauth_start(cfg, port=8789)
    with C._OAUTH_LOCK:
        session = C._OAUTH_SESSIONS[started["session_id"]]
    return cfg, started, session


def call(cfg, query_string):
    """模拟浏览器 GET /oauth/callback?<query_string>，返回 (status, html)。"""
    query = urllib.parse.parse_qs(query_string, keep_blank_values=True)
    return C.oauth_callback_get(cfg, query)


print("CodeArts OAuth 回调：state 作为 ticket_id 别名")

# --- 1. state -> 候选 ticket_id 的展开 --------------------------------------
check("`<hex>.pkg` 展开出裸 hex 与原文两种候选",
      C._state_candidates("abc123.pkg") == ["abc123", "abc123.pkg"],
      C._state_candidates("abc123.pkg"))
check("裸 hex 只有一种候选",
      C._state_candidates("cc7e7f9ea201009221489b09a8fe2fcc")
      == ["cc7e7f9ea201009221489b09a8fe2fcc"])
check("空串没有候选", C._state_candidates("") == [])
check("None 没有候选", C._state_candidates(None) == [])

# --- 2. 华为云的真实回调形态：只有 state + code ------------------------------
reset_sessions()
cfg, started, session = new_session()
status, html = call(cfg, "code=FAKE_CODE&state=%s.pkg" % session.ticket_id)
check("state 形态命中会话（不再报缺少会话标识）",
      "缺少会话标识" not in html, html[:120])
check("state 形态走到兑换分支（假 code 被上游拒，说明会话已认出）",
      status == 400 and "登录失败" in html, (status, html[:160]))
check("会话状态被标成 error 而不是停在 pending", session.status == "error")

# --- 3. state 原样回传（不带 .pkg）也能命中 ---------------------------------
reset_sessions()
cfg, started, session = new_session()
status, html = call(cfg, "code=FAKE_CODE&state=%s" % session.ticket_id)
check("state 裸 hex 也能命中",
      "缺少会话标识" not in html and "登录失败" in html, (status, html[:160]))

# --- 4. 仍然兼容原始的 ticket_id 形态 ---------------------------------------
reset_sessions()
cfg, started, session = new_session()
status, html = call(cfg, "code=FAKE_CODE&ticket_id=%s" % session.ticket_id)
check("ticket_id 形态照旧可用",
      "缺少会话标识" not in html and "登录失败" in html, (status, html[:160]))

# --- 5. 陌生 state 必须拒绝，绝不猜最近的 pending 会话 ----------------------
reset_sessions()
cfg, started, session = new_session()
status, html = call(cfg, "code=FAKE_CODE&state=deadbeefdeadbeefdeadbeefdeadbeef.pkg")
check("陌生 state 被拒（不会登进别人的会话）",
      status == 400 and "会话不存在" in html, (status, html[:160]))
check("陌生 state 不影响原会话状态",
      session.status == "pending", session.status)

# --- 6. 完全没有会话标识时必须报错，不能凭空造一个 --------------------------
reset_sessions()
cfg, started, session = new_session()
status, html = call(cfg, "code=FAKE_CODE")
check("有 pending 会话但无任何标识时仍拒绝（不猜）",
      status == 400 and "缺少会话标识" in html, (status, html[:160]))
check("拒绝后会话仍是 pending", session.status == "pending", session.status)

# --- 7. 零会话时同样报「缺少会话标识」 --------------------------------------
reset_sessions()
cfg = C.load_config(ACCOUNTS)
status, html = call(cfg, "code=FAKE_CODE")
check("无 pending 会话时也报「缺少会话标识」",
      status == 400 and "缺少会话标识" in html, (status, html[:160]))

# --- 8. secret 形态不受影响 --------------------------------------------------
reset_sessions()
cfg, started, session = new_session()
redirect = urllib.parse.quote("https://codearts.huaweicloud.com/portal/x", safe="")
status, html = call(cfg, "secret=%s&redirect=%s" % (session.secret, redirect))
check("secret + redirect 形态照旧走「继续授权」",
      status == 200 and "正在继续华为云授权" in html, (status, html[:160]))

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
