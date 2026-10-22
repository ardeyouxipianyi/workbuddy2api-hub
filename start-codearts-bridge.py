#!/usr/bin/env python3
"""Unified gateway (native default 8788); use --port 8799 for isolated tests."""
import argparse
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--codearts-config", help="separate bridge JSON (default: accounts/codearts/bridge.json)")
    parser.add_argument("--codearts-source", help="build and manage a local CodeArts2API source checkout")
    parser.add_argument("--codearts-backend", help="manage an already built CodeArts2API executable")
    parser.add_argument("--codearts-backend-port", type=int, default=7866, help="internal loopback port, not the client gateway")
    parser.add_argument("--go-executable", default="go", help="Go compiler when using --codearts-source")
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
    runtime = install(hub, path)
    generated = hub._bootstrap_runtime(args)
    if hub._report_first_run(args):
        return
    manager = None
    try:
        if options.codearts_source or options.codearts_backend:
            from extensions.codearts.backend import ManagedBackend, build_backend
            directory = Path(path).resolve().parent / "backend"
            executable = options.codearts_backend
            if options.codearts_source:
                executable = build_backend(options.codearts_source,
                    directory / ("codearts2api.exe" if sys.platform == "win32" else "codearts2api"),
                    options.go_executable)
            manager = ManagedBackend([str(Path(executable).resolve())], directory,
                                     port=options.codearts_backend_port)
            manager.start(runtime.config)
            manager.attach_exit(hub)
            hub.log("CodeArts backend ready; clients use the same Hub /v1 gateway")
        hub._log_startup_summary(args, generated)
        hub.log("CodeArts extension loaded; configure it in the CodeArts panel")
        hub._serve_forever(args)
    finally:
        if manager is not None:
            manager.stop()


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    main()
