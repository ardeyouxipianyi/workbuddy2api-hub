#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真實輸出上限探測（M5 E2，panel scripts/probe_max_tokens.py 的 hub 版）。

對本機網關送一次「要求超長輸出」的請求：若 finish_reason == "length"，
本次 completion_tokens 就是上游靜默鉗制的真實上限；結果寫入
accounts/output_probes.json，/v1/models 會標注 output_clamp。

用法：
  python scripts/probe_max_tokens.py --model deepseek-v4.1-flash
  python scripts/probe_max_tokens.py --model m1 m2 --base-url http://127.0.0.1:8789
  python scripts/probe_max_tokens.py --model m1 --dry-run

金鑰預設讀 accounts/settings.json 的 launcher key；也可用 --api-key 指定。
純標準庫。
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_probes
import wb_settings


def main(argv=None):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", nargs="+", required=True,
                        help="one or more model ids to probe")
    parser.add_argument("--base-url",
                        default=os.environ.get("WB_PROBE_BASE")
                        or "http://127.0.0.1:8789")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--accounts-dir",
                        default=os.environ.get("ACCOUNTS_DIR")
                        or os.path.join(repo, "accounts"))
    parser.add_argument("--output-dir", default="",
                        help="where output_probes.json lives (default accounts dir)")
    parser.add_argument("--max-tokens", type=int, default=1000000)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    api_key = args.api_key
    if not api_key and not args.dry_run:
        try:
            api_key, _generated = wb_settings.ensure_launcher_key(args.accounts_dir)
        except Exception:
            api_key = ""
    out_dir = args.output_dir or args.accounts_dir
    failed = 0
    for model in args.model:
        try:
            result = wb_probes.probe_model(
                args.base_url, api_key, model, max_tokens=args.max_tokens,
                timeout=args.timeout, dry_run=args.dry_run)
        except Exception as exc:
            failed += 1
            print(json.dumps({model: {"error": str(exc)[:300]}}, ensure_ascii=False))
            continue
        if not args.dry_run:
            wb_probes.save_probe(out_dir, model, result)
        print(json.dumps({model: result}, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
