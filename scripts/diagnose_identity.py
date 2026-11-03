#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline outbound-identity diagnostic for WB Proxy.

Prints the effective chat headers for each loaded account with credentials
redacted. It makes no network request, so it is safe to run against an
offline or idle gateway and is useful when comparing a Windows and Linux
installation that both hit upstream code 11-128.

Examples:
  python scripts/diagnose_identity.py
  python scripts/diagnose_identity.py --product cli
  python scripts/diagnose_identity.py --realm cn --uid <uid-prefix>
  python scripts/diagnose_identity.py --accounts-dir accounts --json

The `x-stainless-*` family belongs to the official CLI / OpenAI Node SDK
profile.  It is not sent by the WorkBuddy desktop or VSCode profiles, so
`WB_IDENTITY_OS` and `WB_IDENTITY_ARCH` only affect the CLI profile.
"""

import argparse
import json
import os
import platform
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import wb_accounts
import wb_identity


REDACTED = "[REDACTED]"
SECRET_HEADERS = frozenset((
    "authorization",
    "x-api-key",
    "x-device-token",
    "x-user-id",
    "x-enterprise-id",
    "x-tenant-id",
    "x-department-info",
    "cookie",
    "set-cookie",
))
ENV_KEYS = ("WB_IDENTITY_OS", "WB_IDENTITY_ARCH",
            "WB_IDENTITY_NODE_VERSION", "WB_DESKTOP_CHAT_COMPAT")


def redact_headers(headers):
    """Return a printable copy of a header map without credentials."""
    out = {}
    for name, value in (headers or {}).items():
        text = "" if value is None else str(value)
        if name.lower() in SECRET_HEADERS:
            if name.lower() == "authorization":
                scheme = text.split(None, 1)[0] if text else ""
                out[name] = (scheme + " " + REDACTED).strip()
            else:
                out[name] = REDACTED
        else:
            out[name] = text
    return out


def identity_environment():
    """Describe the process-wide identity switch inputs."""
    return {
        "platform": platform.system() or "",
        "machine": platform.machine() or "",
        "python": platform.python_version(),
        "effective": {
            "os": wb_identity._identity_os(),
            "arch": wb_identity._identity_arch(),
            "node": wb_identity.node_version(),
            "sdk": wb_identity.sdk_version(),
            "desktop_chat_compat": wb_identity.desktop_chat_compat_enabled(),
        },
        "overrides": {key: str(os.environ.get(key) or "") for key in ENV_KEYS},
        "versions": wb_identity.default_versions(),
    }


def _synthetic_conversation(account, product, session_key):
    """Return the conversation id a real request would use for this product."""
    if product == wb_identity.PRODUCT_CLI and session_key:
        return wb_identity.conversation_id_for(account.uid, session_key)
    return None


def inspect_account(account, product=None, model="deepseek-v4.1-flash"):
    """Build one account's effective chat headers without sending a request."""
    actual = account.product
    target = wb_identity.normalize_product(product or actual)
    old = account.product
    try:
        account.product = target
        session_key = "diagnose-identity"
        turn_request_id = wb_identity.new_turn_request_id()
        conversation_id = _synthetic_conversation(account, target, session_key)
        headers = account.headers(
            purpose="chat", session_key=session_key,
            turn_request_id=turn_request_id, model=model,
            conversation_id=conversation_id)
        endpoint = wb_identity.endpoint_for(account.realm, target)[0]
    finally:
        account.product = old
    stainless = sorted(name for name in headers if name.lower().startswith("x-stainless-"))
    return {
        "uid_prefix": (account.uid or "")[:8],
        "realm": account.realm,
        "product": target,
        "product_override": bool(product and product != actual),
        "endpoint": endpoint,
        "stainless_headers": stainless,
        "headers": redact_headers(headers),
    }


def collect(accounts_dir, uid=None, realm=None, product=None,
            model="deepseek-v4.1-flash"):
    """Load accounts and return the complete diagnostic payload."""
    pool = wb_accounts.AccountPool(accounts_dir)
    pool.load()
    accounts = list(pool.accounts)
    wanted_uid = str(uid or "").strip()
    if wanted_uid:
        accounts = [a for a in accounts
                    if wanted_uid == a.uid or (a.uid or "").startswith(wanted_uid)]
    if realm:
        accounts = [a for a in accounts if a.realm == realm]
    rows = [inspect_account(a, product=product, model=model) for a in accounts]
    notes = []
    if product == wb_identity.PRODUCT_CLI or any(
            row["product"] == wb_identity.PRODUCT_CLI for row in rows):
        notes.append("x-stainless-* is sent by the CLI identity only.")
        notes.append("On CLI, WB_IDENTITY_OS / WB_IDENTITY_ARCH override the SDK host fields.")
    else:
        notes.append("The selected identities do not send x-stainless-*; "
                     "WB_IDENTITY_OS / WB_IDENTITY_ARCH do not change these headers.")
    return {
        "ok": True,
        "environment": identity_environment(),
        "accounts": rows,
        "notes": notes,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts-dir",
                        default=os.environ.get("ACCOUNTS_DIR")
                        or os.path.join(ROOT, "accounts"))
    parser.add_argument("--uid", default="", help="account uid or uid prefix")
    parser.add_argument("--realm", choices=("intl", "cn"), default=None)
    parser.add_argument("--product", choices=wb_identity.VALID_PRODUCTS,
                        default=None, help="preview another outbound identity")
    parser.add_argument("--model", default="deepseek-v4.1-flash")
    parser.add_argument("--json", action="store_true",
                        help="print compact JSON instead of indented JSON")
    args = parser.parse_args(argv)
    data = collect(args.accounts_dir, uid=args.uid, realm=args.realm,
                   product=args.product, model=args.model)
    json.dump(data, sys.stdout, ensure_ascii=False,
              indent=None if args.json else 2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
