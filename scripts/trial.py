#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""國際版帳號激活 / trial 加油包 CLI（M2 B7，panel global_register.go + trial.go）。

用法：
  python scripts/trial.py --list
  python scripts/trial.py --uid <uid> --register
  python scripts/trial.py --uid <uid> --claim
  python scripts/trial.py --all --register --claim

讀取與網關相同的 accounts 目錄（--accounts-dir 或 ACCOUNTS_DIR 環境變數，
預設為 repo 下的 accounts/）。純標準庫；所有動作冪等，可重複執行。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_global


def main(argv=None):
    default_dir = os.environ.get("ACCOUNTS_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "accounts")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts-dir", default=default_dir)
    parser.add_argument("--uid", default="", help="single account uid")
    parser.add_argument("--all", action="store_true", help="every international account")
    parser.add_argument("--register", action="store_true",
                        help="complete region/registration (idempotent)")
    parser.add_argument("--claim", action="store_true",
                        help="claim the one-shot trial top-up (idempotent)")
    parser.add_argument("--list", action="store_true", help="list international accounts")
    args = parser.parse_args(argv)

    pool = wb_accounts.AccountPool(args.accounts_dir)
    pool.load()
    targets = [a for a in pool.accounts if a.realm == "intl"]
    if args.uid:
        targets = [a for a in targets if a.uid == args.uid]
    if not targets:
        print("no matching international accounts", file=sys.stderr)
        return 1

    if args.list or not (args.register or args.claim):
        for account in targets:
            print("%s  %s" % (account.uid, account.nickname or ""))
        return 0

    failed = 0
    for account in targets:
        print("== %s (%s)" % (account.uid[:8], account.nickname or "?"))
        if args.register:
            try:
                activated, detail = wb_global.complete_registration(account)
                print("   register: %s (%s)" % ("activated" if activated else "pending", detail))
            except Exception as exc:
                failed += 1
                print("   register FAILED: %s" % exc)
        if args.claim:
            try:
                claimed = wb_global.claim_trial(account)
                print("   trial: %s" % ("claimed" if claimed else "already claimed"))
            except Exception as exc:
                failed += 1
                print("   trial FAILED: %s" % exc)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
