# -*- coding: utf-8 -*-
"""wb_prompt.py — 網關自有系統提示詞模式（panel prompt.go / degrade.go 語義）。

客戶端（Codex / Claude Code 等）在 system prompt 裡注入固定模板句時，上游內容
審核可能按逐字匹配誤殺合法流量。本模組讓網關在出站前改寫 system/developer
訊息，從源頭消滅 system 來源的指紋誤報；user/assistant/tool 訊息不動（既有
sanitize 指紋清洗照舊，兩層互不替代）。

模式（settings.json 的 prompt.mode，預設 passthrough）：
  - passthrough：透傳客戶端 system/developer（hub 原本的行為）；
  - custom：刪除所有 system/developer，換成一條網關提示詞；
  - append：在開頭連續 system/developer 區塊之後插入一條網關提示詞，
    客戶端既有訊息逐字保留。

降級（passthrough / append）：內容審核攔截（403）多半是 system 來源的指紋
誤殺。此時切到最小中性提示詞，直到次日 00:00 CST 重置，並把同一個請求用
中性提示詞重試一次（panel degrade.go）。custom 模式不進降級路徑。
"""

import threading
import time

# 內置默認提示詞：用於 custom / append 模式（可用 prompt.file 覆蓋）。
DEFAULT_PROMPT = """你是一名工程助手，幫助用戶完成軟件工程任務。

- 先理解代碼與上下文再動手，遵循既有模式與約定。
- 最小改動：只改必要部分，不做無關重構。
- 改動後要驗證：跑測試或構建確認結果，不假設「應該沒問題」。
- 對不確定的事保持誠實：說明不確定並給出驗證路徑，不編造。
- 跟隨用戶語言；簡潔直接，技術術語精確。
- 如實報告失敗與邊界，不掩蓋、不粉飾。
"""

# 降級提示詞：刻意極簡中性，只用於繞開 system 來源的審核誤報。
DEGRADED_PROMPT = ("You are a helpful assistant. Respond in the user's language, "
                   "follow the user's instructions, and be direct and concise.")

VALID_MODES = ("passthrough", "custom", "append")


def normalize_mode(value):
    """未知模式回退 passthrough（保守默認，不意外改寫客戶端提示詞）。"""
    mode = str(value or "").strip().lower()
    return mode if mode in VALID_MODES else "passthrough"


def load_prompt(mode, file_path=""):
    """讀取網關提示詞：file 非空則讀檔（失敗拋出，調用方 fail open），否則內置默認。"""
    path = str(file_path or "").strip()
    if not path:
        return DEFAULT_PROMPT
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def rewrite(messages, system_prompt):
    """刪除所有 system/developer，頭部插入一條網關 system 訊息。"""
    if not system_prompt:
        return messages
    kept = [m for m in (messages or [])
            if not (isinstance(m, dict)
                    and m.get("role") in ("system", "developer"))]
    return [{"role": "system", "content": system_prompt}] + kept


def append(messages, system_prompt):
    """在開頭連續 system/developer 區塊之後插入網關提示詞，其餘訊息不動。"""
    if not system_prompt:
        return messages
    msgs = list(messages or [])
    insert_at = 0
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") not in ("system", "developer"):
            break
        insert_at += 1
    return (msgs[:insert_at]
            + [{"role": "system", "content": system_prompt}]
            + msgs[insert_at:])


def apply_mode(messages, mode, text, degraded=False):
    """按模式路由一次請求的 messages；degraded 只影響 passthrough/append。"""
    mode = normalize_mode(mode)
    if mode == "custom":
        return rewrite(messages, text)
    if mode == "append":
        if degraded:
            return rewrite(messages, DEGRADED_PROMPT)
        return append(messages, text)
    if degraded:
        return rewrite(messages, DEGRADED_PROMPT)
    return messages


def next_midnight_cst(now=None):
    """now 之後最近的 Asia/Shanghai 00:00（epoch 秒）。

    用固定 +08:00 計算，不依賴宿主機時區（容器/宿主時區不確定）。
    00:00 整點 → 次日 00:00；23:59 → 幾秒後的次日 00:00。
    """
    now = time.time() if now is None else float(now)
    offset = 8 * 3600
    day = int((now + offset) // 86400)
    return float((day + 1) * 86400 - offset)


class DegradeGate(object):
    """進程內存降級窗口：觸發後到次日 00:00 CST 為止（不續期）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._until = 0.0

    def active(self, now=None):
        now = time.time() if now is None else float(now)
        with self._lock:
            return now < self._until

    def trigger(self, now=None):
        now = time.time() if now is None else float(now)
        with self._lock:
            if now >= self._until:
                self._until = next_midnight_cst(now)

    def reset(self):
        with self._lock:
            self._until = 0.0
