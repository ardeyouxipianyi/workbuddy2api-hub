"""The standalone configurator: `python wb_agents.py ...` (hermetic, no network).

The panel's one-click setup can only ever edit the machine the gateway runs
on - a remote browser cannot write another computer's config files - so the
CLI is what covers every other machine, and it is the one part of the agents
feature a user runs by hand. This suite drives the real entry point as a
subprocess (argument parsing, key resolution, exit codes and all) against
temporary homes, with a stub gateway standing in for GET /v1/models.

Two properties the panel side cannot show are pinned here as well: a dry run
prints exactly what a real run writes (both go through plan()), and
GET /agents/cli serves the script to remote machines without a panel session
while the neighbouring /agents read still refuses one.

    python tests/_test_agents_cli.py
"""

import email
import http.server
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from _isolated_dirs import isolated_data_dirs  # noqa: E402  (its own data root)

_TMP_ENV = isolated_data_dirs("wb-agents-cli-")

import wb_agents  # noqa: E402
import wb_proxy  # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


# ---------------------------------------------------------------------------
# Stub gateway + a port nothing listens on
# ---------------------------------------------------------------------------

class StubGateway(http.server.BaseHTTPRequestHandler):
    """Answers GET /v1/models; the status and payload are the test's to set."""

    status = 200
    payload = {"object": "list", "data": []}
    seen_paths = []
    seen_auth = None

    def do_GET(self):
        type(self).seen_paths.append(self.path)
        type(self).seen_auth = self.headers.get("Authorization")
        if self.path != "/v1/models":
            self.send_error(404)
            return
        if self.status != 200:
            self.send_response(self.status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps(self.payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


_gateway = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StubGateway)
threading.Thread(target=_gateway.serve_forever, daemon=True).start()
GW = "http://127.0.0.1:%d" % _gateway.server_address[1]

# A closed socket's port: connection refused without any real network use.
_sock = socket.socket()
_sock.bind(("127.0.0.1", 0))
DEAD = "http://127.0.0.1:%d" % _sock.getsockname()[1]
_sock.close()

TMP = tempfile.mkdtemp(prefix="wb-agents-cli-")


def run_cli(args, env=None):
    """Run the real CLI. PATH is emptied so the client probes stay deterministic
    (a developer machine may well have `codex` on PATH; CI does not), and the
    wall clock is bounded so a child that blocks on a prompt fails this suite
    instead of hanging it."""
    child = dict(os.environ)
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "WB_PROXY_API_KEY"):
        child.pop(name, None)
    child["PATH"] = ""
    child["PYTHONPATH"] = ROOT
    child["PYTHONIOENCODING"] = "utf-8"
    if env:
        child.update(env)
    try:
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "wb_agents.py")] + list(args),
            capture_output=True, stdin=subprocess.DEVNULL, cwd=ROOT, env=child,
            timeout=60)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after 60s (the CLI blocked on input?)"
    return (proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
print("[1] list: five clients, nothing installed in a bare home")

home1 = os.path.join(TMP, "home1")
os.makedirs(home1)
code, out, err = run_cli(["--home", home1, "--state-dir", os.path.join(TMP, "state1"), "list"])
check("exit 0", code == 0, (code, err))
for client in ("claude-code", "codex", "opencode", "dsh", "crush", "zcode"):
    check("%s listed" % client, client in out, out)
check("nothing is reported installed", out.count("not detected") == 6, out)

# The shared options work on either side of the subcommand (parents= with
# SUPPRESS defaults); --json after the verb is the natural spelling.
code, out, err = run_cli(["--home", home1, "--state-dir", os.path.join(TMP, "state1"),
                          "list", "--json"])
data = json.loads(out)
check("--json after the subcommand parses",
      code == 0 and sorted(data) == ["claude-code", "codex", "crush", "dsh",
                                     "opencode", "zcode"],
      (code, err, out[:200]))

# ---------------------------------------------------------------------------
print()
print("[2] apply codex: only the wb-proxy block is added, everything else survives")

home2 = os.path.join(TMP, "home2")
codex_dir = os.path.join(home2, ".codex")
os.makedirs(codex_dir)
original2 = (
    "# hand-written comment\n"
    "[desktop]\n"
    'followUpQueueMode = "steer"\n'
    "\n"
    "[model_providers.other]\n"
    'name = "Other"\n'
    'base_url = "http://other/v1"\n'
)
cfg2 = os.path.join(codex_dir, "config.toml")
with open(cfg2, "w", encoding="utf-8", newline="") as fh:
    fh.write(original2)
state2 = os.path.join(TMP, "state2")
code, out, err = run_cli(["--home", home2, "--state-dir", state2, "apply",
                          "--client", "codex", "--base-url", GW + "/v1",
                          "--key", "sk-cli-test-0001", "--no-models"])
check("exit 0", code == 0, (code, err))
after2 = read(cfg2)
check("original lines survive verbatim", original2 in after2, after2)
check("comment preserved", "# hand-written comment" in after2, after2)
check("other provider preserved",
      "[model_providers.other]" in after2 and 'name = "Other"' in after2, after2)
check("wb-proxy provider added", "[model_providers.wb-proxy]" in after2, after2)
check("base_url points at the gateway", 'base_url = "%s/v1"' % GW in after2, after2)
check("wire_api is responses", 'wire_api = "responses"' in after2, after2)
check("model_provider selected", 'model_provider = "wb-proxy"' in after2, after2)
auth2 = json.loads(read(os.path.join(codex_dir, "auth.json")))
check("auth.json carries the key", auth2.get("OPENAI_API_KEY") == "sk-cli-test-0001", auth2)
check("state file written", os.path.isfile(os.path.join(state2, "integration-state.json")))
backups2 = os.listdir(os.path.join(state2, "agent-backups", "codex"))
check("one backup stored", len(backups2) == 1, backups2)
check("backup holds the original bytes",
      read(os.path.join(state2, "agent-backups", "codex", backups2[0])) == original2)
check("key is masked in the output",
      "sk-cli-test-0001" not in out and "***0001" in out, out)

# ---------------------------------------------------------------------------
print()
print("[3] status reads the record; restore returns the file byte for byte")

code, out, err = run_cli(["--home", home2, "--state-dir", state2, "status"])
check("status exit 0", code == 0, (code, err))
check("status shows the applied record", "applied:" in out and GW + "/v1" in out, out)

code, out, err = run_cli(["--home", home2, "--state-dir", state2, "restore", "--client", "codex"])
check("restore exit 0", code == 0, (code, err))
check("config.toml restored byte for byte", read(cfg2) == original2)
check("auth.json removed (it did not exist before)",
      not os.path.exists(os.path.join(codex_dir, "auth.json")))
code, out, err = run_cli(["--home", home2, "--state-dir", state2, "restore", "--client", "codex"])
check("a second restore fails cleanly", code == 1 and "no recorded integration" in err,
      (code, err))

# ---------------------------------------------------------------------------
print()
print("[4] --dry-run writes nothing and prints exactly what apply writes")

home4 = os.path.join(TMP, "home4")
os.makedirs(home4)
state4 = os.path.join(TMP, "state4")
args4 = ["--home", home4, "--state-dir", state4, "apply", "--client", "claude-code",
         "--base-url", GW + "/v1", "--key", "sk-dry-run-0003", "--no-models"]
code, out, err = run_cli(args4 + ["--dry-run"])
check("exit 0", code == 0, (code, err))
check("nothing under the home", not os.path.exists(os.path.join(home4, ".claude")))
check("nothing under the state dir", not os.path.exists(state4))
check("dry run announces itself", "dry run" in out, out)
check("the would-be file is named", "create" in out and "settings.json" in out, out)
code, out2, err = run_cli(args4)
check("real apply exit 0", code == 0, (code, err))
written4 = read(os.path.join(home4, ".claude", "settings.json"))
# stdout goes through the platform's text translation (CRLF on Windows);
# the file is written with newline="" and keeps bare LF.
check("dry-run content equals the real write",
      written4 in out.replace("\r\n", "\n"), (out[-400:], written4))

# ---------------------------------------------------------------------------
print()
print("[5] the model list comes from the gateway and feeds embedded-model clients")

StubGateway.status = 200
StubGateway.payload = {"object": "list", "data": [
    {"id": "m-one", "context_length": 128000, "max_output_tokens": 4096},
    {"id": "m-two", "max_input_tokens": 200000},
    {"id": "m-one", "name": "duplicate id"},
]}
StubGateway.seen_paths = []
home5 = os.path.join(TMP, "home5")
os.makedirs(os.path.join(home5, ".config", "opencode"))
state5 = os.path.join(TMP, "state5")
code, out, err = run_cli(["--home", home5, "--state-dir", state5, "apply",
                          "--client", "opencode", "--base-url", GW + "/v1",
                          "--key", "sk-models-0004", "--model", "m-one"])
check("exit 0", code == 0, (code, err))
oc5 = json.loads(read(os.path.join(home5, ".config", "opencode", "opencode.json")))
provider5 = oc5["provider"]["wb-proxy"]
check("provider registered", provider5["npm"] == "@ai-sdk/openai-compatible", provider5)
check("baseURL uses the /v1 form", provider5["options"]["baseURL"] == GW + "/v1", provider5)
check("models carried over, duplicates collapsed",
      sorted(provider5["models"]) == ["m-one", "m-two"], provider5.get("models"))
check("default model selected", oc5.get("model") == "wb-proxy/m-one", oc5.get("model"))
check("the Bearer key was sent",
      StubGateway.seen_auth == "Bearer sk-models-0004", StubGateway.seen_auth)
check("the gateway saw /v1/models", "/v1/models" in StubGateway.seen_paths,
      StubGateway.seen_paths)
check("the output reports the fetch", "2 fetched" in out, out)

# ---------------------------------------------------------------------------
print()
print("[6] a 401 from the gateway names the key problem and stops the apply")

StubGateway.status = 401
home6 = os.path.join(TMP, "home6")
os.makedirs(home6)
state6 = os.path.join(TMP, "state6")
code, out, err = run_cli(["--home", home6, "--state-dir", state6, "apply",
                          "--client", "codex", "--base-url", GW + "/v1",
                          "--key", "sk-wrong"])
check("exit 1", code == 1, (code, out, err))
check("401 named as a key problem", "401" in err and "key" in err, err)
check("--no-models offered as the escape hatch", "--no-models" in err, err)
check("nothing was written", not os.path.exists(os.path.join(home6, ".codex")))

# ---------------------------------------------------------------------------
print()
print("[7] an unreachable gateway fails with the same escape hatch")

StubGateway.status = 200
code, out, err = run_cli(["--home", home6, "--state-dir", state6, "apply",
                          "--client", "codex", "--base-url", DEAD + "/v1",
                          "--key", "sk-x"])
check("exit 1", code == 1, (code, out, err))
check("message says the gateway cannot be reached", "cannot reach" in err, err)
check("--no-models offered", "--no-models" in err, err)

# ---------------------------------------------------------------------------
print()
print("[8] --no-models works offline; the key may come from the environment")

code, out, err = run_cli(["--home", home6, "--state-dir", state6, "apply",
                          "--client", "codex", "--base-url", DEAD + "/v1",
                          "--key", "sk-offline", "--no-models"])
check("exit 0 offline", code == 0, (code, err))
check("config written", os.path.isfile(os.path.join(home6, ".codex", "config.toml")))
check("no models line in the output", "fetched" not in out, out)

home8 = os.path.join(TMP, "home8")
os.makedirs(home8)
state8 = os.path.join(TMP, "state8")
code, out, err = run_cli(["--home", home8, "--state-dir", state8, "apply",
                          "--client", "claude-code", "--base-url", DEAD + "/v1",
                          "--no-models"],
                         env={"WB_PROXY_API_KEY": "sk-env-0005"})
check("exit 0 with the env key", code == 0, (code, err))
env8 = json.loads(read(os.path.join(home8, ".claude", "settings.json")))["env"]
check("env key written", env8["ANTHROPIC_AUTH_TOKEN"] == "sk-env-0005", env8)
check("Anthropic base URL keeps no /v1 suffix",
      env8["ANTHROPIC_BASE_URL"] == DEAD, env8)

code, out, err = run_cli(["--home", home8, "--state-dir", state8, "apply",
                          "--client", "claude-code", "--base-url", DEAD + "/v1",
                          "--key", "sk-a", "--no-models", "--models-from-gateway"])
check("--no-models with --models-from-gateway is refused", code == 1, (code, out, err))

# ---------------------------------------------------------------------------
print()
print("[9] a missing key in a non-interactive run fails cleanly")

code, out, err = run_cli(["--home", home8, "--state-dir", state8, "apply",
                          "--client", "claude-code", "--base-url", DEAD + "/v1",
                          "--no-models"])
check("exit 1", code == 1, (code, out, err))
check("the message points at --key", "--key" in err, err)

# ---------------------------------------------------------------------------
print()
print("[10] a second apply keeps the first backup; restore goes back to the very first state")

home10 = os.path.join(TMP, "home10")
dsh_dir = os.path.join(home10, ".dsh")
os.makedirs(dsh_dir)
settings10 = os.path.join(dsh_dir, "settings.yaml")
original10 = "# my dsh settings\nllm-pi-ai:\n  timeout: 30\n"
with open(settings10, "w", encoding="utf-8", newline="") as fh:
    fh.write(original10)
state10 = os.path.join(TMP, "state10")
common10 = ["--home", home10, "--state-dir", state10, "apply", "--client", "dsh",
            "--base-url", DEAD + "/v1", "--key", "sk-dsh", "--no-models"]
code, out, err = run_cli(common10 + ["--model", "m-one"])
check("first apply exit 0", code == 0, (code, err))
first_backup = json.loads(read(os.path.join(state10, "integration-state.json")))["dsh"]["files"][0]["backup"]

with open(settings10, "a", encoding="utf-8") as fh:
    fh.write("# an external edit after the first apply\n")
code, out, err = run_cli(["--home", home10, "--state-dir", state10, "status"])
check("status flags the external change", "external change" in out, out)

code, out, err = run_cli(common10 + ["--model", "m-two"])
check("second apply exit 0", code == 0, (code, err))
second_backup = json.loads(read(os.path.join(state10, "integration-state.json")))["dsh"]["files"][0]["backup"]
check("the backup pointer still names the first snapshot", second_backup == first_backup,
      (first_backup, second_backup))

code, out, err = run_cli(["--home", home10, "--state-dir", state10, "restore", "--client", "dsh"])
check("restore exit 0", code == 0, (code, err))
check("restore returns to the pre-first-apply bytes", read(settings10) == original10)
check("the credentials file we created is gone",
      not os.path.exists(os.path.join(dsh_dir, ".credentials.yaml")))

# The other half of the rule: a hand edit to a file we created makes it the
# user's file, and restore must bring their version back rather than delete it.
home11 = os.path.join(TMP, "home11")
os.makedirs(os.path.join(home11, ".dsh"))
state11 = os.path.join(TMP, "state11")
apply11 = ["--home", home11, "--state-dir", state11, "apply", "--client", "dsh",
           "--base-url", DEAD + "/v1", "--no-models"]
run_cli(apply11 + ["--key", "sk-first"])
cred11 = os.path.join(home11, ".dsh", ".credentials.yaml")
with open(cred11, "a", encoding="utf-8") as fh:
    fh.write("  MY_OWN_REF: keep-me\n")
run_cli(apply11 + ["--key", "sk-second"])
code, out, err = run_cli(["--home", home11, "--state-dir", state11, "restore", "--client", "dsh"])
check("a hand-edited file we created survives restore",
      code == 0 and os.path.exists(cred11) and "MY_OWN_REF" in read(cred11), (code, err))

# ---------------------------------------------------------------------------
print()
print("[11] GET /agents/cli serves the script without a panel session")


class CliDispatch(wb_proxy.Handler):
    """A real Handler driven over an in-memory socket (the pattern the
    panel-route-auth suite uses): what answers is the dispatcher, not a
    hand-picked method."""

    def __init__(self, path):
        self.path = path
        self.command = "GET"
        self.request_version = "HTTP/1.1"
        self.close_connection = True
        self.headers = email.message_from_string("")
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.status = None
        self.client_address = ("127.0.0.1", 0)

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass

    def log_message(self, fmt, *args):
        pass


request = CliDispatch("/agents/cli")
request.do_GET()
body = request.wfile.getvalue()
with open(os.path.join(ROOT, "wb_agents.py"), "rb") as fh:
    source = fh.read()
check("served without a panel session (200)", request.status == 200, request.status)
check("body is the repository file, byte for byte", body == source,
      (len(body), len(source)))

request = CliDispatch("/agents")
request.do_GET()
check("the neighbouring /agents read still needs the panel session",
      request.status == 401, request.status)

# ---------------------------------------------------------------------------
_gateway.shutdown()
shutil.rmtree(TMP, ignore_errors=True)

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
