#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模型权益探测：逐个模型发一次 1-token 请求，把上游的「账号类型不支持」记下来。

免费账号（personal_free_user）请求 GPT / Grok / Gemini 系模型时上游回
400 `code: 11158`，但 /v1/models 的目录来自桌面端产品目录，照样列着它们。
这个脚本把整张目录跑一遍：

  - 上游 200  -> 从黑名单里清掉（升级成付费账号后复查即可恢复）
  - 上游 11158 -> 记进 accounts/model_entitlements.json，/v1/models 不再列出

运行中的网关自己也会记（成功一次就清），脚本只是一次性把全表对齐。

用法：
  python scripts/probe_model_entitlements.py
  python scripts/probe_model_entitlements.py --model hy3 gpt-6-astra
  python scripts/probe_model_entitlements.py --include-hidden   # 连已隐藏的一起复查
  python scripts/probe_model_entitlements.py --dry-run

金钥预设读 accounts/settings.json 的 launcher key；也可用 --api-key 指定。
纯标准库。
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_entitlements
import wb_settings


def _get(url, key, timeout):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _chat(base_url, key, model, timeout):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
            return 200, ""
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return exc.code, raw
    except Exception as exc:  # 网络层错误：不改黑名单，只报告
        return 0, str(exc)


def main(argv=None):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", nargs="+", default=None,
                        help="one or more model ids (default: the gateway catalog)")
    parser.add_argument("--base-url",
                        default=os.environ.get("WB_PROBE_BASE")
                        or "http://127.0.0.1:8788")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--accounts-dir",
                        default=os.environ.get("ACCOUNTS_DIR")
                        or os.path.join(repo, "accounts"))
    parser.add_argument("--realm", default="",
                        help="realm to record under (default: what /v1/models reports)")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--include-hidden", action="store_true",
                        help="probe the hidden models too (ask for the full catalog)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    api_key = args.api_key
    if not api_key and not args.dry_run:
        try:
            api_key, _generated = wb_settings.ensure_launcher_key(args.accounts_dir)
        except Exception:
            api_key = ""

    models = args.model
    realm = args.realm
    if not models:
        suffix = "/v1/models" + ("?include_hidden=1" if args.include_hidden else "")
        payload = _get(args.base_url.rstrip("/") + suffix, api_key, args.timeout)
        models = [item.get("id") for item in payload.get("data") or [] if item.get("id")]
        realm = realm or payload.get("realm") or "intl"
    realm = realm or "intl"

    if args.dry_run:
        print(json.dumps({"realm": realm, "models": models}, ensure_ascii=False, indent=2))
        return 0

    allowed, denied, other = [], [], []
    for model in models:
        status, detail = _chat(args.base_url, api_key, model, args.timeout)
        if status == 200:
            wb_entitlements.note_allowed(args.accounts_dir, realm, model)
            allowed.append(model)
        elif status == 400 and wb_entitlements.is_denial(detail):
            wb_entitlements.note_denied(args.accounts_dir, realm, model, detail)
            denied.append(model)
        else:
            other.append({"model": model, "status": status,
                          "detail": detail[:160]})
        if args.delay:
            time.sleep(args.delay)

    print(json.dumps({
        "realm": realm,
        "allowed": allowed,
        "denied": denied,
        "other": other,
    }, ensure_ascii=False, indent=2))
    return 0 if not other else 1


if __name__ == "__main__":
    sys.exit(main())
