#!/usr/bin/env python3
"""Optional launcher: python start-codearts-bridge.py --port 8799 [native Hub flags]."""
import argparse
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--codearts-config", help="separate bridge JSON (default: accounts/codearts/bridge.json)")
    options, native_args = parser.parse_known_args()
    import wb_proxy as hub
    from extensions.codearts.bridge import install
    sys.argv = [sys.argv[0]] + native_args
    args = hub._parse_cli_args()
    hub._apply_cli_overrides(args)
    if args.probe_only:
        raise SystemExit(0 if hub._probe_running_instance(args) else 3)
    if hub._probe_running_instance(args):
        return
    path = options.codearts_config or (Path(args.accounts_dir or hub.ACCOUNTS_DIR) / "codearts" / "bridge.json")
    install(hub, path)
    generated = hub._bootstrap_runtime(args)
    if hub._report_first_run(args):
        return
    hub._log_startup_summary(args, generated)
    hub.log("CodeArts extension loaded; configure it in the CodeArts panel (disabled by default)")
    hub._serve_forever(args)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    main()
