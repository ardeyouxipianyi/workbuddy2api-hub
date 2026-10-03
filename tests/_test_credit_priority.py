"""Offline checks for expiring credits, exhausted accounts and Windows launchers."""
import calendar
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_startup = tempfile.TemporaryDirectory(prefix="wb-credit-tests-")
os.environ["ACCOUNTS_DIR"] = _startup.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup.name
import wb_accounts as A
import wb_proxy as P


def account(uid, day=15, remain=10, realm="cn"):
    return A.Account({"uid": uid, "realm": realm, "accessToken": "synthetic",
                      "credits": {"remain": remain, "packages": [
                          {"remain": remain, "expires_at": A.billing_epoch(
                              "2030-10-%02d 23:59:59" % day)}]}})


def response(packages, total=None):
    return {"data": {"Response": {"Data": {
        "Accounts": packages, "TotalCount": len(packages) if total is None else total}}}}


class CreditPriorityTests(unittest.TestCase):
    def pool(self, accounts):
        pool = A.AccountPool(_startup.name)
        pool.accounts = accounts
        # All chat attempts in this suite are synthetic; billing is tested separately.
        pool.refresh_credits = lambda _realm=None: None
        return pool

    def test_billing_precision_cycle_expiry_pagination_and_failed_refresh(self):
        a = account("a")
        monthly = {"PackageName": "monthly", "CycleCapacitySize": 500,
                   "CycleCapacitySizePrecise": None,
                   "CycleCapacityRemain": 0, "CycleCapacityRemainPrecise": "0.75",
                   "DeductionEndTime": A.billing_epoch("2035-10-31") * 1000,
                   "CycleEndTime": "2030-10-31 23:59:59"}
        bonus = {"CapacitySize": 100, "CapacityRemain": 10,
                 "CapacitySizePrecise": "", "CapacityRemainPrecise": None,
                 "DeductionEndTime": A.billing_epoch("2030-10-15 12:00:00") * 1000}
        with mock.patch.object(A, "http_json", side_effect=[response([monthly], 2), response([bonus], 2)]) as fetch:
            self.assertTrue(a.fetch_credits()["ok"])
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(json.loads(fetch.call_args.kwargs["data"])["PageNumber"], 2)
        self.assertAlmostEqual(a.credits["remain"], 10.75)
        self.assertEqual(a.credits["packages"][0]["expires_at"], A.billing_epoch(monthly["CycleEndTime"]))
        self.assertEqual(a.credit_priority()["remain"], 10)
        old = a.credits
        with mock.patch.object(A, "http_json", return_value={"error": {"code": 401}}):
            self.assertFalse(a.fetch_credits()["ok"])
        self.assertIs(a.credits, old)
        with mock.patch.object(A, "http_json", return_value=response([])):
            self.assertTrue(a.fetch_credits()["ok"])
        self.assertTrue(a.credit_blocked())
        with mock.patch.object(A, "http_json", return_value=response([bonus])):
            self.assertTrue(a.fetch_credits()["ok"])
        self.assertTrue(a.ready())

    def test_priority_overrides_sticky_binding_then_reorders_after_package_spent(self):
        a, b, c = account("a", 15, 20), account("b", 16, 5), account("c", 16, 10)
        a.credits["packages"].append({"remain": 100, "expires_at": A.billing_epoch("2030-10-17")})
        a.credits["remain"] += 100
        disabled = account("disabled", 14, 1)
        disabled.enabled = False
        foreign = account("foreign", 14, 1, realm="intl")
        pool = self.pool([c, disabled, b, a, foreign])
        pool.affinity.bind("session", c.uid)
        self.assertIs(pool.pick_for_session("cn", "session", model="m"), a)
        self.assertEqual({pool.pick("cn").uid for _ in range(4)}, {"a"})
        a.credits["packages"][0]["remain"] = 0
        self.assertIs(pool.pick_for_session("cn", "session", model="m"), b)
        b.note_error("429", model="m", until=time.time() + 600)
        self.assertIs(pool.pick_for_session("cn", "session", model="m"), c)
        self.assertIs(pool.pick("cn", model="other"), b)
        self.assertIs(pool.pick("cn", exclude={"b", "c"}), a)
        unknown = A.Account({"uid": "unknown", "accessToken": "synthetic", "realm": "cn"})
        pool = self.pool([unknown, A.Account({"uid": "second", "accessToken": "synthetic", "realm": "cn"})])
        self.assertEqual({pool.pick("cn").uid for _ in range(4)}, {"unknown", "second"})
        pool.affinity.bind("s", "unknown")
        self.assertIs(pool.pick_for_session("cn", "s"), unknown)

    def test_14018_rotates_and_does_not_retry_exhausted_account_on_other_models(self):
        a, b = account("a", 15), account("b", 16)
        pool = self.pool([a, b])
        detail = json.dumps({"error": {"data": {"code": 14018, "msg": "Credits exhausted"}}})
        error = urllib.error.HTTPError("https://synthetic.invalid", 429, "quota", {}, io.BytesIO(detail.encode()))
        success = io.BytesIO(b"data: [DONE]\n\n")
        with mock.patch.object(P, "POOL", pool), mock.patch.object(A, "urlopen", side_effect=[error, success]) as send:
            stream, chosen = P.open_upstream({"model": "m", "messages": []}, target_realm="cn")
            self.assertIs(chosen, b)
            self.assertIs(stream, success)
            self.assertEqual(send.call_count, 2)
        self.assertTrue(a.credit_blocked())
        self.assertFalse(a.ready(model="another-model"))
        self.assertEqual(a.model_cooldowns, {})
        with mock.patch.object(P, "POOL", self.pool([a])), mock.patch.object(A, "urlopen") as send:
            with self.assertRaises(P.RateLimited) as caught:
                P.open_upstream({"model": "m", "messages": []}, target_realm="cn")
            self.assertEqual(caught.exception.error_type, "insufficient_quota")
            send.assert_not_called()

    def test_rate_limit_honors_header_and_preserves_other_models(self):
        a, b = account("a", 15), account("b", 16)
        pool = self.pool([a, b])
        error = urllib.error.HTTPError("https://synthetic.invalid", 429, "limit", {"Retry-After": "120"},
                                      io.BytesIO(b'{"error":{"data":{"code":6004}}}'))
        with mock.patch.object(P, "POOL", pool), mock.patch.object(A, "urlopen", side_effect=[error, io.BytesIO()]):
            _, chosen = P.open_upstream({"model": "m", "messages": []}, target_realm="cn")
            self.assertIs(chosen, b)
        self.assertGreater(a.throttle_wait("m"), 115)
        self.assertTrue(a.ready("other"))
        expected = calendar.timegm(time.strptime("2030-10-15 12:00:00", "%Y-%m-%d %H:%M:%S")) - 28800
        self.assertEqual(P.parse_rate_limit_reset("usage will reset at 2030-10-15 12:00:00 UTC+8"), expected)
        self.assertEqual(P.parse_retry_after("Tue, 15 Oct 2030 04:00:00 GMT"), expected)
        self.assertIsNone(P.parse_retry_after("unknown"))
        self.assertIsNone(P.parse_retry_after("NaN"))
        self.assertIsNone(P.parse_retry_after("Infinity"))
        self.assertIsNone(P.parse_retry_after("-1"))

    def test_usage_invalidates_balance_and_cache_avoids_repeated_billing(self):
        a = account("a")
        pack = {"CapacitySize": 100, "CapacityRemain": 20,
                "DeductionEndTime": A.billing_epoch("2030-10-15") * 1000}
        with mock.patch.object(A, "http_json", return_value=response([pack])) as fetch:
            self.assertTrue(a.fetch_credits(max_age=300)["ok"])
            self.assertTrue(a.fetch_credits(max_age=300)["ok"])
            self.assertEqual(fetch.call_count, 1)
            with mock.patch.object(P, "POOL", self.pool([a])):
                P.record_usage("m", {"prompt_tokens": 1, "total_tokens": 1}, account="a")
            self.assertTrue(a._credits_dirty)
            a.fetch_credits(max_age=300)
            self.assertEqual(fetch.call_count, 2)
            with mock.patch.object(P, "POOL", self.pool([a])):
                P.record_error("m", 502, "synthetic stream ended", account="a",
                               outcome="upstream_aborted")
            self.assertTrue(a._credits_dirty)
            a.fetch_credits(max_age=300)
            self.assertEqual(fetch.call_count, 3)

    def test_localized_network_abort_retries_same_account_then_rotates(self):
        def abort():
            return urllib.error.URLError(OSError(10053, "你的主机中的软件中止了一个已建立的连接。"))

        self.assertTrue(P.is_transient(abort()))
        self.assertTrue(P.is_transient(OSError(10054, "远程主机强迫关闭了一个现有的连接。")))
        self.assertFalse(P.is_transient(urllib.error.URLError(OSError(2, "文件不存在"))))
        for failures, expected_calls, expected_account in ((1, 2, "a"), (2, 3, "b")):
            a, b = account("a", 15), account("b", 16)
            pool = self.pool([a, b])
            with mock.patch.object(P, "POOL", pool), \
                    mock.patch.object(P.time, "sleep"), \
                    mock.patch.object(A, "urlopen", side_effect=[abort() for _ in range(failures)] + [io.BytesIO()]) as send:
                _, chosen = P.open_upstream({"model": "m", "messages": []},
                                            session_key="novel", target_realm="cn")
            self.assertEqual(chosen.uid, expected_account)
            self.assertEqual(send.call_count, expected_calls)
            self.assertTrue(a.ready(model="m"))
            self.assertEqual(a.last_error, "")
            self.assertEqual(pool.affinity.get("novel"), expected_account)

    def test_desktop_scan_and_import_reject_encrypted_credentials(self):
        with tempfile.TemporaryDirectory(prefix="wb-desktop-test-") as directory:
            plain = Path(directory) / "workbuddy-desktop.info"
            encrypted = Path(directory) / "workbuddy-desktop-ai.info"
            plain.write_text(json.dumps({"auth": {"accessToken": "synthetic"},
                                         "account": {"uid": "plain"}}), encoding="utf-8")
            encrypted.write_text(json.dumps({"auth": {"accessToken": {
                "$wbEncrypted": 1, "envelope": "synthetic"}}, "account": {
                "uid": "current-encrypted", "nickname": {"$wbEncrypted": 1, "envelope": "synthetic"}}}), encoding="utf-8")
            with mock.patch.object(A, "desktop_credential_candidates", return_value=[
                    (str(plain), "cn"), (str(encrypted), "cn")]):
                rows = A.scan_desktop_credentials()
            self.assertTrue(rows[0]["valid"])
            self.assertTrue(rows[1]["readable"])
            self.assertFalse(rows[1]["valid"])
            self.assertEqual(rows[1]["uid"], "current-encrypted")
            self.assertEqual(rows[1]["nickname"], "")
            self.assertNotIn("envelope", json.dumps(rows))
            self.assertIn("OAuth", rows[1]["error"])
            pool = A.AccountPool(directory)
            with mock.patch.object(pool, "add") as add:
                with self.assertRaisesRegex(RuntimeError, "OAuth"):
                    pool.import_desktop_credential(str(encrypted))
                add.assert_not_called()
            envelope = {"$wbEncrypted": 1, "envelope": "a.b.c"}
            for row in ({"uid": "synthetic", "accessToken": envelope},
                        {"account": {"uid": "synthetic"}, "auth": {"accessToken": envelope}},
                        {"uid": "synthetic", "accessToken": "a.b.c", "refreshToken": envelope}):
                with self.assertRaisesRegex(ValueError, "OAuth"):
                    A.normalise_import_row(row)

    def test_desktop_backups_are_current_deduplicated_and_importable(self):
        with tempfile.TemporaryDirectory(prefix="wb-backup-test-") as directory:
            dest = Path(directory)
            def credential(name, uid, exp):
                (dest / name).write_text(json.dumps({"auth": {"accessToken": "synthetic",
                    "expiresAt": exp}, "account": {"uid": uid}}), encoding="utf-8")
            credential("workbuddy-desktop-ai.info", "current", time.time() + 3600)
            credential("workbuddy-desktop-ai.2030-10-02T00.info", "current", time.time() + 3600)
            credential("workbuddy-desktop-ai.2030-10-01T00.info", "backup", time.time() + 3600)
            credential("workbuddy-desktop-ai.2030-09-30T00.info", "backup", time.time() + 3600)
            credential("workbuddy-desktop-ai.2030-09-29T00.info", "expired", time.time() - 10)
            credential("unrelated.info", "unrelated", time.time() + 3600)
            with mock.patch.object(A, "desktop_auth_dirs", return_value=[directory]):
                rows = A.scan_desktop_credentials()
                self.assertEqual([row["uid"] for row in rows], ["current", "backup"])
                self.assertEqual([row["backup"] for row in rows], [False, True])
                self.assertTrue(rows[1]["file"].endswith("10-01T00.info"))
                pool = A.AccountPool(str(dest / "pool"))
                self.assertEqual([a.uid for a in pool.import_desktop_credential(realm="intl")],
                                 ["current", "backup"])
                with mock.patch.object(P, "POOL", pool), mock.patch.object(P, "log"):
                    self.assertEqual([a.uid for a in P.import_desktop_accounts(realm="intl")],
                                     ["current", "backup"])
            with self.assertRaisesRegex(RuntimeError, "accessToken"):
                A.desktop_access_token({"accessToken": "   "})

    def test_401_refreshes_once_before_rotating(self):
        for failures in (1, 2):
            a, b = account("a", 15), account("b", 16)
            def refresh():
                a.access_token = "refreshed-synthetic"
                return True
            a.refresh = mock.Mock(side_effect=refresh)
            errors = [urllib.error.HTTPError("https://synthetic.invalid", 401, "expired", {}, io.BytesIO())
                      for _ in range(failures)]
            with mock.patch.object(P, "POOL", self.pool([a, b])), \
                    mock.patch.object(A, "urlopen", side_effect=errors + [io.BytesIO()]) as send:
                _, chosen = P.open_upstream({"model": "m", "messages": []}, target_realm="cn")
            self.assertEqual(chosen.uid, "a" if failures == 1 else "b")
            a.refresh.assert_called_once()
            self.assertEqual(send.call_args_list[1].args[0].get_header("Authorization"),
                             "Bearer refreshed-synthetic")

    def test_wire_streams_report_upstream_failures_and_client_cancellation(self):
        with self.assertRaisesRegex(P.UpstreamStreamError, "synthetic upstream error"):
            list(P.checked_upstream_stream([b'data: {"error":{"message":"synthetic upstream error"}}\n']))
        self.assertEqual(len(list(P.checked_upstream_stream([
            b'data: {"choices":[{"finish_reason":"stop"}]}\n']))), 1)
        chunk = b'data: {"choices":[{"delta":{"content":"partial"}}],"usage":{"total_tokens":5}}\n'
        class Stream:
            def __init__(self, fail=False, complete=False):
                self.fail, self.complete = fail, complete
            def __iter__(self):
                yield b": heartbeat\n"
                yield chunk
                if self.fail:
                    raise ConnectionResetError(10054, "synthetic upstream reset")
                if self.complete:
                    yield b"data: [DONE]\n"
                    raise AssertionError("must stop reading after DONE")
            def close(self):
                pass
        class DisconnectedClient(io.BytesIO):
            def write(self, data):
                raise BrokenPipeError("synthetic client cancelled")
        def handler(client=None):
            h = object.__new__(P.Handler)
            h.path, h.headers, h.wfile = "/v1/chat/completions", {}, client if client is not None else io.BytesIO()
            h.send_response = h.send_header = h.end_headers = mock.Mock()
            h._error = mock.Mock()
            return h
        for protocol in ("chat", "responses"):
            def serve(h, stream):
                if protocol == "chat":
                    return h._chat_stream_response(stream, "m", None, account("a"), time.time())
                return h._responses_stream_response(stream, "m", [], {}, None, account("a"), time.time())
            for reset in (False, True):
                h = handler()
                with mock.patch.object(P, "record_error") as failure, mock.patch.object(P, "record_usage") as usage:
                    serve(h, Stream(fail=reset))
                    self.assertEqual(failure.call_args.kwargs["outcome"], "upstream_aborted")
                    usage.assert_not_called()
                wire = h.wfile.getvalue().decode()
                self.assertIn(": keepalive", wire)
                self.assertIn("upstream_stream_error", wire)
                if protocol == "responses":
                    frames = [json.loads(line[6:]) for line in wire.splitlines() if line.startswith("data: ")]
                    self.assertEqual(frames[-1]["type"], "response.failed")
                    self.assertEqual(frames[-1]["response"]["id"], frames[0]["response"]["id"])
                    self.assertNotIn("response.completed", wire)
            h = handler(DisconnectedClient())
            with mock.patch.object(P, "record_error") as failure, mock.patch.object(P, "record_usage") as usage:
                serve(h, Stream(complete=True))
                self.assertEqual(usage.call_args.kwargs["outcome"], "client_aborted")
                failure.assert_not_called()
            h = handler()
            with mock.patch.object(P, "record_error") as failure, mock.patch.object(P, "record_usage") as usage:
                serve(h, Stream(complete=True))
                failure.assert_not_called()
                usage.assert_called_once()
            self.assertNotIn("upstream_stream_error", h.wfile.getvalue().decode())
        holder = {}
        list(P.stream_responses_events(P.checked_upstream_stream(Stream(complete=True)), "m", holder))
        response_id = holder["response_meta"]["id"]
        holder["suppress_lifecycle"] = True
        with self.assertRaises(P.UpstreamStreamError):
            list(P.stream_responses_events(P.checked_upstream_stream(Stream()), "m", holder))
        self.assertEqual(holder["response_meta"]["id"], response_id)
        for protocol in ("chat", "responses"):
            h = handler()
            with mock.patch.object(P, "record_error") as failure:
                if protocol == "chat":
                    h._chat_nonstream_response(Stream(), "m", None, account("a"), time.time())
                else:
                    h._responses_nonstream_response(Stream(), "m", [], {}, None, account("a"), time.time())
                self.assertEqual(h._error.call_args.args[0], 502)
                failure.assert_called_once()

    @unittest.skipUnless(os.name == "nt", "Windows batch files")
    def test_real_launcher_shows_startup_and_request_logs(self):
        with tempfile.TemporaryDirectory(prefix="wb-visible-launch-") as directory:
            dest = Path(directory)
            shutil.copy2(ROOT / "start-wb-proxy.bat", dest)
            for source in ROOT.glob("wb_*.py"):
                shutil.copy2(source, dest)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            env = dict(os.environ, ACCOUNTS_DIR=str(dest / "accounts"),
                       WB_PROXY_USAGE_DIR=str(dest / "usage"),
                       PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
            output = dest / "console.log"
            with output.open("w", encoding="utf-8") as sink:
                proc = subprocess.Popen(["cmd", "/d", "/c", str(dest / "start-wb-proxy.bat"), str(port)],
                                        cwd=dest, env=env, stdout=sink, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        try:
                            with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=1) as resp:
                                self.assertEqual(resp.status, 200)
                            break
                        except (urllib.error.URLError, ConnectionError):
                            time.sleep(0.1)
                    else:
                        self.fail("launcher did not serve health: " + output.read_text(errors="replace"))
                    request = urllib.request.Request("http://127.0.0.1:%d/panel/login" % port,
                        data=b'{"password":"admin"}', headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(request, timeout=2) as resp:
                        self.assertEqual(resp.status, 200)
                    console = output.read_text(errors="replace")
                    self.assertIn("[wb-proxy]", console)
                    self.assertIn("listening", console)
                    self.assertIn("POST /panel/login", console)
                finally:
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                    proc.wait(timeout=10)

    @unittest.skipUnless(os.name == "nt", "Windows batch files")
    def test_launchers_capture_exit_code_and_bound_crash_restarts(self):
        for name in ("start-wb-proxy.bat", "start-wb-proxy-lan.bat"):
            for code, launches in ((0, 1), (1, 1), (130, 1), (7, 4)):
                with tempfile.TemporaryDirectory(prefix="wb-launch-test-") as directory:
                    dest = Path(directory)
                    shutil.copy2(ROOT / name, dest / name)
                    (dest / "wb_proxy.py").write_text(
                        'import sys\nprint("synthetic failure", file=sys.stderr)\nsys.exit(%d)\n' % code,
                        encoding="utf-8")
                    env = dict(os.environ, PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
                    run = subprocess.run(["cmd", "/d", "/c", str(dest / name), "9876", "synthetic-key"],
                                         input="\n", text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         env=env, timeout=30)
                    log = (dest / "wb-proxy-errors.log").read_text(errors="replace")
                    self.assertEqual(log.count("starting proxy"), launches, run.stdout)
                    self.assertEqual(log.count("synthetic failure"), launches)
                    self.assertIn("exit code %d" % code, log)
                    self.assertEqual(run.returncode, code, run.stdout)


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        _startup.cleanup()
