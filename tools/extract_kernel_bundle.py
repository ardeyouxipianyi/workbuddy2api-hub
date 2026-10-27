#!/usr/bin/env python3
"""Locate and print plain-text JavaScript windows inside a Bun-packaged
CodeArts Agent kernel binary (AgentKernel_Vscode_*.exe).

Purpose
-------
The official CodeArts Agent kernel is a Bun single-file executable: the
bundled JavaScript source is embedded as plain (non-minified) text, so the
real outbound-header construction code can be recovered by byte-level
searching.  This tool is the reproducible instrument behind
``.codeartsdoer/plans/codearts-ide-spoofing/official-kernel-chat-headers-evidence.md``.

It is READ-ONLY: the binary is opened ``rb`` and memory-mapped; nothing is
written back, ever.

Design notes
------------
* Standard library only, Python 3.9+ (no walrus-in-comprehension tricks, no
  ``match``, no ``|`` unions in annotations that are evaluated at runtime).
* No absolute paths are baked in.  The kernel path comes from ``--kernel``,
  ``$CODEARTS_KERNEL``, or an explicit ``--kernel-dir`` glob search.
* Every byte window that is printed is passed through a credential masker, so
  pasting tool output into a report cannot leak tokens.
* Anchors are declared as *patterns* (not baked offsets) so the same table
  works across kernel versions; the reported offset is what the operator then
  cites in evidence.

Usage
-----
    python tools/extract_kernel_bundle.py --help
    python tools/extract_kernel_bundle.py anchors
    python tools/extract_kernel_bundle.py search --pattern "user-session-id" --max-hits 5
    python tools/extract_kernel_bundle.py window --offset 0x645e41d --length 1200
    python tools/extract_kernel_bundle.py anchor --name chat-headers-middleware
    python tools/extract_kernel_bundle.py --selftest
"""

from __future__ import annotations

import argparse
import mmap
import os
import re
import sys

# --------------------------------------------------------------------------
# Anchor table
# --------------------------------------------------------------------------
# Each entry: name -> (list of byte patterns; ALL must be present in the
# reported window, first pattern is the locator).  Patterns are ASCII bytes;
# they are matched case-sensitively, so they double as a check that the
# capitalisation recorded in the evidence file is the real one.
ANCHORS = [
    (
        "inferhub-headers-object",
        [b'"x-ot-session-id"', b'"x-ot-trace-id"', b'"x-ot-span-id"'],
    ),
    (
        "inferhub-session-headers",
        [b"add inferhub session headers", b'user-session-id'],
    ),
    (
        "chat-headers-middleware",
        [b"plugin-name", b"plugin-version", b"ide-name", b"ide-version"],
    ),
    (
        "snap-traceid-uuid",
        [b"x-snap-traceid", b'replace(/-/g,"")'],
    ),
    (
        "chat-headers-compose",
        [b"ChatHeaders", b"is_confidential", b"app-id"],
    ),
    (
        "app-id-const",
        [b"CodeAgent3.0"],
    ),
    (
        "chat-completions-endpoint",
        [b"/api/v2/chat/completions"],
    ),
    (
        "chat-endpoint-v1",
        [b"/v1/chat/chat"],
    ),
    (
        "user-agent-header",
        [b"User-Agent"],
    ),
    (
        "msg-id-prefix",
        [b"msg_"],
    ),
    # ---- chat-request header construction (added after the header survey) ----
    (
        "inferhub-header-map",
        [b"getInferhubHcHeaders", b'"x-ot-session-id"', b'"client-ip"'],
    ),
    (
        "inferhub-provider-factory",
        [b"ai-sdk/openai-compatible/${", b"inferhubBaseUrl"],
    ),
    (
        "chat-headers-xot-middleware",
        [b"inferhub OT headers", b'"x-snap-traceid"', b'"created-time"'],
    ),
    (
        "chat-headers-identity-middleware",
        [b"identity headers emitted", b'"plugin-name"', b'"kernel-version"'],
    ),
    (
        "aksk-signer",
        [b"SDK-HMAC-SHA256", b"SignedHeaders="],
    ),
    (
        "identifier-ids",
        [b'session:"ses"', b'message:"msg"'],
    ),
    (
        "runtime-ua-builder",
        [b"runtime/node.js/${", b'filter(Boolean).join(" ")'],
    ),
    (
        "ai-sdk-post-to-api",
        [b"ai-sdk/provider-utils/${", b"body:{content:"],
    ),
]

# --------------------------------------------------------------------------
# Credential masking
# --------------------------------------------------------------------------
# Anything that looks like a bearer/credential assignment gets its value
# replaced by a fixed mask.  The mask keeps the field name (so evidence stays
# readable) and drops the secret.
_MASK = "***MASKED***"

_SENSITIVE_KEYS = (
    "x-auth-token",
    "x-security-token",
    "x-sdk-content-sha256",
    "access-token",
    "accesstoken",
    "accesskeyid",
    "secretaccesskey",
    "securitytoken",
    "refresh_token",
    "refreshtoken",
    "authorization",
    "x-api-key",
    "api_key",
    "apikey",
    "dpop",
    "private_key",
    "privatekey",
    "client_secret",
    "clientsecret",
    "password",
    "secret",
)

_MASK_PATTERNS = [
    # key: "value" / key: 'value' / "key":"value" / ["key"]="value"
    re.compile(
        r"(?i)(?P<k>\[?\s*[\"']?(?:"
        + "|".join(re.escape(k) for k in _SENSITIVE_KEYS)
        + r")[\"']?\s*\]?\s*[:=]\s*)"
        r"(?P<v>\"[^\"\\\r\n]{0,4096}\""
        r"|'[^'\\\r\n]{0,4096}'"
        r"|[A-Za-z0-9._~+/=\-]{6,})"
    ),
    # Bearer <token>
    re.compile(r"(?i)(?P<k>Bearer\s+)(?P<v>[A-Za-z0-9._~+/=\-]{8,})"),
    # SDK-HMAC-SHA256 Access=...  Signature=...
    re.compile(r"(?i)(?P<k>Access=)(?P<v>[A-Za-z0-9._~+/=\-]{4,})"),
    re.compile(r"(?i)(?P<k>Signature=)(?P<v>[A-Za-z0-9._~+/=\-]{4,})"),
    # Long opaque base64/hex-ish blobs that are clearly secrets (>= 40 chars)
    # are left alone unless they follow a sensitive key: random ids such as
    # sha256 digests legitimately appear in this binary.
]


def mask_secrets(text: str) -> str:
    """Replace credential-looking values with a fixed mask.

    Field names are preserved; only values are replaced.  Session ids
    (``ses_...``) and trace ids are **not** credentials and are preserved.
    """
    out = text
    for pat in _MASK_PATTERNS:
        def _sub(m, _pat=pat):
            k = m.group("k")
            quote = ""
            v = m.group("v")
            if v[:1] in ("'", '"'):
                quote = v[0]
            return "%s%s%s%s" % (k, quote, _MASK, quote)

        out = pat.sub(_sub, out)
    return out


# --------------------------------------------------------------------------
# Binary access
# --------------------------------------------------------------------------
DEFAULT_KERNEL_GLOB_PREFIX = "AgentKernel_Vscode_"
DEFAULT_KERNEL_GLOB_SUFFIX = ".exe"
DEFAULT_KERNEL_DIR = os.path.join("~", ".codeartsdoer", "CodeArts_Agent")

MAX_WINDOW = 200 * 1024


def resolve_kernel(explicit: str = "", kernel_dir: str = "") -> str:
    """Resolve the kernel binary path without hardcoding an absolute path."""
    if explicit:
        path = os.path.expanduser(explicit)
        if not os.path.isfile(path):
            raise SystemExit("error: kernel not found: %s" % path)
        return path
    env = os.environ.get("CODEARTS_KERNEL", "").strip()
    if env:
        path = os.path.expanduser(env)
        if not os.path.isfile(path):
            raise SystemExit("error: $CODEARTS_KERNEL not found: %s" % path)
        return path
    directory = os.path.expanduser(
        kernel_dir or os.environ.get("CODEARTS_KERNEL_DIR", "") or DEFAULT_KERNEL_DIR
    )
    if not os.path.isdir(directory):
        raise SystemExit(
            "error: kernel not given. Pass --kernel PATH, set $CODEARTS_KERNEL, "
            "or make sure %s exists." % directory
        )
    candidates = [
        os.path.join(directory, name)
        for name in sorted(os.listdir(directory))
        if name.startswith(DEFAULT_KERNEL_GLOB_PREFIX)
        and name.endswith(DEFAULT_KERNEL_GLOB_SUFFIX)
    ]
    if not candidates:
        raise SystemExit(
            "error: no %s*%s under %s"
            % (DEFAULT_KERNEL_GLOB_PREFIX, DEFAULT_KERNEL_GLOB_SUFFIX, directory)
        )
    # Newest mtime wins when several kernel builds are present.
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates[0]


class Bundle(object):
    """Read-only memory-mapped view over the kernel binary."""

    def __init__(self, path):
        self.path = path
        self._fh = open(path, "rb")
        self.size = os.fstat(self._fh.fileno()).st_size
        self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)

    def close(self):
        try:
            self._mm.close()
        finally:
            self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def find_all(self, needle, limit=0, start=0):
        """Yield byte offsets of ``needle`` (non-overlapping)."""
        hits = 0
        pos = start
        while True:
            idx = self._mm.find(needle, pos)
            if idx < 0:
                return
            yield idx
            hits += 1
            if limit and hits >= limit:
                return
            pos = idx + 1

    def count(self, needle, cap=100000):
        n = 0
        for _ in self.find_all(needle, limit=cap):
            n += 1
        return n

    def window(self, offset, length):
        if offset < 0 or offset >= self.size:
            raise SystemExit(
                "error: offset 0x%x outside 0..0x%x" % (offset, self.size)
            )
        length = max(1, min(length, MAX_WINDOW, self.size - offset))
        return self._mm[offset:offset + length]

    def outline_window(self, pattern, span=600):
        """Return a printable window centred on the first match of pattern."""
        for off in self.find_all(pattern, limit=1):
            start = max(0, off - span // 3)
            raw = self._mm[start:start + span]
            return off, start, raw
        return None, None, None


def decode(raw) -> str:
    """Decode a byte window into readable text without raising."""
    return raw.decode("utf-8", "replace")


def printable(text: str) -> str:
    """Make a raw JS window safe/legible for a console or a markdown paste."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse runs of blank lines but keep the newlines that structure code.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def cmd_anchors(bundle, args):
    print("# file : %s" % os.path.basename(bundle.path))
    print("# size : %d bytes (0x%x)" % (bundle.size, bundle.size))
    print("#")
    print("# %-28s %-12s %-7s %s" % ("anchor", "offset", "hits", "status"))
    ok = 0
    for name, patterns in ANCHORS:
        locator = patterns[0]
        offsets = list(bundle.find_all(locator, limit=1))
        total = len(offsets) if args.all_hits else bundle.count(locator, cap=999999)
        if not offsets:
            print("# %-28s %-12s %-7d MISSING(%s)"
                  % (name, "-", 0, locator.decode("ascii", "replace")))
            continue
        off = offsets[0]
        window = bundle.window(max(0, off - 4000), 8000) if len(patterns) > 1 else b""
        missing = [p.decode("ascii", "replace") for p in patterns[1:] if p not in window]
        status = "OK" if not missing else "PARTIAL missing=%s" % ",".join(missing)
        if not missing:
            ok += 1
        print("%-30s 0x%-10x %-7d %s" % (name, off, total, status))
    print("# %d/%d anchors resolved" % (ok, len(ANCHORS)))
    return 0 if ok else 1


def cmd_anchor(bundle, args):
    for name, patterns in ANCHORS:
        if name != args.name:
            continue
        locator = patterns[0]
        found = False
        for off in bundle.find_all(locator, limit=max(1, args.max_hits)):
            found = True
            start = max(0, off - args.before)
            raw = bundle.window(start, args.before + args.after)
            print("=== anchor %s @ 0x%x (window 0x%x..0x%x) ==="
                  % (name, off, start, start + len(raw)))
            print(mask_secrets(printable(decode(raw))))
            print()
        if not found:
            print("error: anchor %r not found (%s)"
                  % (name, locator.decode("ascii", "replace")))
            return 1
        return 0
    print("error: unknown anchor %r; see --help" % args.name)
    return 2


def cmd_search(bundle, args):
    needle = args.pattern.encode("utf-8")
    hits = 0
    for off in bundle.find_all(needle, limit=args.max_hits):
        hits += 1
        start = max(0, off - args.before)
        raw = bundle.window(start, args.before + args.after)
        print("=== hit %d @ 0x%x (window 0x%x..0x%x) ==="
              % (hits, off, start, start + len(raw)))
        print(mask_secrets(printable(decode(raw))))
        print()
    if not hits:
        print("no hits for %r" % args.pattern)
        return 1
    print("# %d hit(s) for %r" % (hits, args.pattern))
    return 0


def cmd_count(bundle, args):
    total = bundle.count(args.pattern.encode("utf-8"), cap=args.cap)
    print("%s\t%d" % (args.pattern, total))
    return 0


def cmd_window(bundle, args):
    offset = parse_offset(args.offset)
    raw = bundle.window(offset, args.length)
    text = printable(decode(raw))
    if args.marks:
        text = insert_offset_marks(text, offset, args.marks)
    print(mask_secrets(text))
    return 0


def insert_offset_marks(text, start_offset, every):
    """Insert ``[0xADDR]`` markers so a printed window can be cited precisely."""
    if every <= 0:
        return text
    out = []
    for idx in range(0, len(text), every):
        out.append("[0x%x]\n" % (start_offset + idx))
        out.append(text[idx:idx + every])
    out.append("\n[end 0x%x]" % (start_offset + len(text)))
    return "".join(out)


def cmd_info(bundle, args):
    print("path      : %s" % os.path.basename(bundle.path))
    print("size      : %d bytes (0x%x)" % (bundle.size, bundle.size))
    for probe in (b"/api/v2/chat/completions", b"user-session-id", b"x-snap-traceid"):
        print("count %-24s %d"
              % (probe.decode("ascii"), bundle.count(probe, cap=999999)))
    return 0


# --------------------------------------------------------------------------
# Runtime evidence: aggregate ``[header-debug] full headers:`` lines
# --------------------------------------------------------------------------
# The kernel's inferhub provider logs the *final* header map it built (before
# the transport adds content-type / user-agent / accept).  The log formatter
# filters out ``X-Security-token`` and ``Authorization``, and every value that
# is printed here is additionally masked by :func:`mask_secrets`.
HEADER_DEBUG_MARK = "full headers: "
HEADER_DEBUG_TAIL = ", globalConfig.clientType="
HEADER_DEBUG_DEFAULT_LOG_DIR = os.path.join(
    "~", ".codeartsdoer", "codearts-data", "log")

# Field values worth enumerating; everything else is summarised by shape only.
HEADER_DEBUG_ENUM_FIELDS = (
    "x-ot-function",
    "X-Language",
    "plugin-name",
    "plugin-version",
    "ide-name",
    "ide-version",
    "x-ot-client-type",
    "x-ot-client-version",
    "client-ip",
    "maas_type",
    "x-codearts-doer-scenario-type",
    "X-Remote-Origin-Url",
)


def shape_of(key, value):
    """Describe a value without leaking a high-entropy identifier."""
    text = "" if value is None else str(value)
    low = key.lower()
    if low in ("x-ot-trace-id", "x-ot-span-id", "user-session-id",
               "x-ot-session-id", "x-ot-parent-session-id"):
        if not text:
            return "empty"
        if text.startswith("ses_"):
            return "ses_+%d" % (len(text) - 4)
        if re.fullmatch(r"[0-9a-f]{32}", text):
            return "hex32"
        return "len%d" % len(text)
    if low == "x-snap-traceid":
        if "_" in text:
            head, _, tail = text.partition("_")
            return "hex%d_hex%d" % (len(head), len(tail))
        return "len%d_no-sep" % len(text)
    if low == "user-msg-id":
        if not text:
            return "empty"
        if re.fullmatch(r"msg_[0-9A-Za-z]{26}", text):
            return "msg_+26chartag"
        if re.fullmatch(r"msg_[0-9a-fA-F-]{36}", text):
            return "msg_+uuid36"
        return "msg_+other"
    if low == "created-time":
        return "iso8601s" if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", text) else "other"
    if low == "x-sdk-date":
        return "yyyymmddThhmmssZ" if re.fullmatch(r"\d{8}T\d{6}Z", text) else "other"
    return "len%d" % len(text)


def analyze_header_debug(lines):
    """Aggregate ``[header-debug]`` log lines into a summary dict."""
    import collections
    import json

    key_sets = collections.Counter()
    key_orders = collections.Counter()
    enum_values = collections.defaultdict(collections.Counter)
    shapes = collections.defaultdict(collections.Counter)
    total = 0
    skipped = 0
    sessions = collections.Counter()
    for line in lines:
        pos = line.find(HEADER_DEBUG_MARK)
        if pos < 0:
            continue
        end = line.find(HEADER_DEBUG_TAIL, pos)
        if end < 0:
            skipped += 1
            continue
        try:
            parsed = json.loads(line[pos + len(HEADER_DEBUG_MARK):end])
        except ValueError:
            skipped += 1
            continue
        total += 1
        key_sets[tuple(sorted(parsed))] += 1
        key_orders[tuple(parsed)] += 1
        for key, value in parsed.items():
            shapes[key][shape_of(key, value)] += 1
            if key in HEADER_DEBUG_ENUM_FIELDS:
                enum_values[key][str(value)] += 1
        sid = str(parsed.get("x-ot-session-id", ""))
        if sid:
            sessions[sid] += 1
    return {
        "total": total,
        "skipped": skipped,
        "key_sets": key_sets,
        "key_orders": key_orders,
        "enum_values": enum_values,
        "shapes": shapes,
        "sessions": sessions,
    }


def cmd_header_debug(args):
    log_dir = os.path.expanduser(
        args.log_dir
        or os.environ.get("CODEARTS_LOG_DIR", "")
        or HEADER_DEBUG_DEFAULT_LOG_DIR)
    if not os.path.isdir(log_dir):
        print("error: log dir not found: %s" % log_dir)
        return 1
    files = sorted(
        os.path.join(log_dir, name)
        for name in os.listdir(log_dir)
        if name.endswith(".log"))
    if args.max_files:
        # newest by mtime, not by name: log names are timestamped per process
        files = sorted(files, key=lambda p: os.path.getmtime(p))[-args.max_files:]
    all_lines = []
    scanned = 0
    hit_files = 0
    for path in files:
        scanned += 1
        before = len(all_lines)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if HEADER_DEBUG_MARK in line:
                        all_lines.append(line.rstrip("\n"))
        except OSError as exc:
            print("# skip %s: %s" % (os.path.basename(path), exc))
            continue
        if len(all_lines) > before:
            hit_files += 1
    summary = analyze_header_debug(all_lines)
    print("# log dir   : %s" % log_dir)
    print("# files     : %d scanned, %d with header-debug lines"
          % (scanned, hit_files))
    print("# records   : %d parsed, %d unparsable" % (summary["total"], summary["skipped"]))
    print()
    print("## key order (as constructed, most frequent first)")
    for order, count in summary["key_orders"].most_common(5):
        print("  x%-5d %s" % (count, " > ".join(order)))
    print()
    print("## header name -> observed value shapes")
    for key in sorted(summary["shapes"]):
        items = "; ".join("%s x%d" % (a, b)
                          for a, b in summary["shapes"][key].most_common(4))
        print("  %-28s %s" % (key, items))
    print()
    print("## enumerable fields -> observed values (masked)")
    for key in sorted(summary["enum_values"]):
        items = "; ".join("%r x%d" % (mask_secrets(a), b)
                          for a, b in summary["enum_values"][key].most_common(8))
        print("  %-28s %s" % (key, items))
    print()
    if args.samples and summary["total"]:
        print("## first %d record(s), sensitive values masked" % args.samples)
        for line in all_lines[:args.samples]:
            print(mask_secrets(line.strip())[:2000])
    return 0


def iter_header_debug_lines(paths):
    """Yield ``[header-debug]`` log lines from the given files (streaming)."""
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if HEADER_DEBUG_MARK in line:
                        yield line.rstrip("\n")
        except OSError:
            continue


# --------------------------------------------------------------------------
# Self test (offline, no kernel needed)
# --------------------------------------------------------------------------
SELFTEST_BLOB = b"""
// synthetic bundle used by --selftest (no real code, no real secrets)
// add inferhub session headers
const mi=()=>({app-id:"CodeAgent3.0",is_confidential:"false"});
function ChatHeaders(t){return Object.assign({},t,mi())}
headers["user-session-id"]=u||k.sessionID;
headers["x-ot-session-id"]=u||k.sessionID;
headers["x-ot-trace-id"]=t.traceId;
headers["x-ot-span-id"]=t.spanId;
headers["x-ot-parent-session-id"]=t.parentSessionId;
headers["x-snap-traceid"]=crypto.randomUUID().replace(/-/g,"");
headers["x-auth-token"]="sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345";
headers["X-Security-Token"]="SECRETSECRETSECRETSECRETSECRET";
headers["Authorization"]="Bearer abcdefghijklmnopqrstuvwxyz012345";
x-auth-token: "tok_should_be_masked_0001"
plugin-name, plugin-version, ide-name, ide-version, kernel-version
url:"https://snap-access.cn-north-4.myhuaweicloud.com/api/v2/chat/completions"
"""


def cmd_selftest(bundle_path=None):
    failures = []

    class FakeBundle(Bundle):
        def __init__(self, blob):
            self.path = "<selftest>"
            self._blob = blob
            self._mm = blob  # outline_window() reads self._mm directly
            self.size = len(blob)

        def find_all(self, needle, limit=0, start=0):
            hits = 0
            pos = start
            while True:
                idx = self._blob.find(needle, pos)
                if idx < 0:
                    return
                yield idx
                hits += 1
                if limit and hits >= limit:
                    return
                pos = idx + 1

        def window(self, offset, length):
            length = max(1, min(length, MAX_WINDOW, self.size - offset))
            return self._blob[offset:offset + length]

    # 1. locator must find each synthetic anchor payload
    fake = FakeBundle(SELFTEST_BLOB)
    for name, patterns in ANCHORS:
        # Only the locators that exist in the blob are asserted present.
        if name in ("inferhub-headers-object", "inferhub-session-headers",
                    "chat-headers-middleware", "snap-traceid-uuid",
                    "chat-headers-compose", "app-id-const",
                    "chat-completions-endpoint"):
            offsets = list(fake.find_all(patterns[0], limit=1))
            if not offsets:
                failures.append("locator %s not found in selftest blob" % name)

    # 2. windowing + masking must not leak the planted secrets
    off, start, raw = fake.outline_window(b"x-auth-token", span=200)
    if off is None:
        failures.append("outline_window failed on selftest blob")
    else:
        text = mask_secrets(printable(decode(raw)))
        for leaked in (b"sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
                       b"tok_should_be_masked_0001",
                       b"abcdefghijklmnopqrstuvwxyz012345",
                       b"SECRETSECRETSECRETSECRETSECRET"):
            if leaked.decode("ascii") in text:
                failures.append("masker leaked %r" % leaked.decode("ascii"))
        if _MASK not in text:
            failures.append("masker did not emit mask token")

    # 3. masker leaves non-credentials alone
    plain = 'headers["x-snap-traceid"]=crypto.randomUUID().replace(/-/g,"")'
    if mask_secrets(plain) != plain:
        failures.append("masker rewrote a non-credential line")
    # Synthetic id in the observed shape (ses_ + 12 hex + 14 base62); never a real one.
    sid = 'user-session-id:"ses_0123456789abAbCdEfGhIjKlMn"'
    if mask_secrets(sid) != sid:
        failures.append("masker rewrote a session id (must be preserved)")
    # but a token under a sensitive key must be masked
    if _MASK not in mask_secrets('x-auth-token:"deadbeefdeadbeef"'):
        failures.append("masker missed a token under a sensitive key")

    # 4. offset parsing and resolve_kernel arg handling
    if parse_offset("0x10") != 16 or parse_offset("16") != 16:
        failures.append("parse_offset broken")

    # 5. ANCHORS well-formed
    for name, patterns in ANCHORS:
        if not name or not patterns or not all(isinstance(p, bytes) for p in patterns):
            failures.append("bad anchor entry %r" % (name,))

    # 6. header-debug aggregation (synthetic log lines)
    fake_log = [
        'INFO [t] 2026-10-27T00:00:00.000 +0ms service=inferhub-provider '
        '"[header-debug] full headers: {"x-ot-trace-id":"%s","x-ot-span-id":"%s",'
        '"x-snap-traceid":"%s_%s","x-ot-session-id":"ses_AAAAAAAAAAAAAAAAAAAAAAAAAA",'
        '"x-ot-parent-session-id":"","user-session-id":"ses_AAAAAAAAAAAAAAAAAAAAAAAAAA",'
        '"x-ot-function":"agent-claw","X-Language":"zh-cn",'
        '"user-msg-id":"msg_BBBBBBBBBBBBBBBBBBBBBBBBBB","created-time":"2026-10-27T00:00:00Z",'
        '"model-id":"m","model-name":"m","x-ot-client-type":"IDE",'
        '"x-ot-client-version":"26.10.2","plugin-name":"codemate_vscode",'
        '"plugin-version":"26.10.2","ide-name":"VSCode","ide-version":"26.10.2",'
        '"client-ip":"192.168.0.1","X-Sdk-Date":"20261027T000000Z",'
        '"host":"example.invalid"}, globalConfig.clientType=IDE, '
        'globalConfig.clientVersion=26.10.2"' % ("a" * 32, "b" * 16, "a" * 32, "c" * 16),
        'INFO [t] 2026-10-27T00:00:01.000 +0ms service=inferhub-provider '
        '"[header-debug] full headers: {"x-ot-trace-id":"%s","x-ot-span-id":"%s",'
        '"x-snap-traceid":"%s_%s","user-session-id":"%s"}, '
        'globalConfig.clientType=IDE, globalConfig.clientVersion=26.10.2"'
        % ("d" * 32, "e" * 16, "d" * 32, "f" * 16, "0123456789abcdef0123456789abcdef"),
    ]
    summary = analyze_header_debug(fake_log)
    if summary["total"] != 2:
        failures.append("header-debug aggregator total=%r (want 2)" % summary["total"])
    if summary["shapes"]["x-snap-traceid"].get("hex32_hex16") != 2:
        failures.append("header-debug snap-traceid shape not hex32_hex16")
    if summary["shapes"]["user-session-id"].get("ses_+26") != 1:
        failures.append("header-debug ses_ shape not detected")
    if summary["shapes"]["user-session-id"].get("hex32") != 1:
        failures.append("header-debug hex32 fallback not detected")
    if summary["enum_values"]["x-ot-function"].get("agent-claw") != 1:
        failures.append("header-debug enum aggregation broken")
    if len(summary["key_orders"]) != 2:
        failures.append("header-debug key-order tracking broken")

    # 7. optional: real kernel smoke test when a path is supplied
    if bundle_path:
        try:
            with Bundle(bundle_path) as real:
                if real.size < 1024:
                    failures.append("kernel suspiciously small: %d" % real.size)
                if not list(real.find_all(b"/chat/completions", limit=1)):
                    failures.append("kernel has no /chat/completions anchor")
        except Exception as exc:  # pragma: no cover - environment dependent
            failures.append("kernel smoke test failed: %s" % exc)

    if failures:
        print("SELFTEST FAILED (%d)" % len(failures))
        for item in failures:
            print("  - %s" % item)
        return 1
    print("SELFTEST OK: %d anchors, masker, offset parsing, header-debug "
          "aggregator%s"
          % (len(ANCHORS), ", live kernel smoke test" if bundle_path else ""))
    return 0


def parse_offset(value: str) -> int:
    text = str(value).strip().lower().replace("_", "")
    if text.startswith("0x"):
        return int(text, 16)
    if text.startswith("0b"):
        return int(text, 2)
    return int(text, 10)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--kernel", default="", help="path to AgentKernel_Vscode_*.exe")
    common.add_argument("--kernel-dir", default="",
                        help="directory to auto-discover the kernel in "
                             "(default: ~/.codeartsdoer/CodeArts_Agent)")

    parser = argparse.ArgumentParser(
        prog="extract_kernel_bundle.py",
        description="Read-only locator for plain-text JS windows inside a Bun "
                    "CodeArts Agent kernel binary. Credential values are masked "
                    "in every printed window.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "anchors (known locators, with offsets + hit counts):\n"
            + "\n".join("  %-28s %s" % (n, ps[0].decode("ascii", "replace"))
                       for n, ps in ANCHORS)
            + "\n\nexamples:\n"
              "  CODEARTS_KERNEL=/path/to/AgentKernel_Vscode_*.exe \\\n"
              "      python tools/extract_kernel_bundle.py anchors\n"
              "  python tools/extract_kernel_bundle.py search "
              "--pattern 'user-session-id' --max-hits 3\n"
              "  python tools/extract_kernel_bundle.py window --offset 0x645e41d "
              "--length 1200\n"
              "  python tools/extract_kernel_bundle.py --selftest\n"
        ),
    )
    parser.add_argument("--selftest", action="store_true",
                        help="run the offline self test (no kernel required)")
    sub = parser.add_subparsers(dest="command")

    p_anchors = sub.add_parser("anchors", parents=[common],
                               help="resolve every known anchor and print offsets")
    p_anchors.add_argument("--all-hits", action="store_true",
                           help="do not count total hits (faster on big files)")
    p_anchors.set_defaults(func=cmd_anchors)

    p_anchor = sub.add_parser("anchor", parents=[common],
                              help="print the window around one named anchor")
    p_anchor.add_argument("--name", required=True, choices=[n for n, _ in ANCHORS])
    p_anchor.add_argument("--before", type=int, default=400)
    p_anchor.add_argument("--after", type=int, default=800)
    p_anchor.add_argument("--max-hits", type=int, default=2)
    p_anchor.set_defaults(func=cmd_anchor)

    p_search = sub.add_parser("search", parents=[common],
                              help="byte-search a literal string and print windows")
    p_search.add_argument("--pattern", required=True)
    p_search.add_argument("--before", type=int, default=200)
    p_search.add_argument("--after", type=int, default=400)
    p_search.add_argument("--max-hits", type=int, default=5)
    p_search.set_defaults(func=cmd_search)

    p_count = sub.add_parser("count", parents=[common],
                             help="count non-overlapping occurrences of a literal")
    p_count.add_argument("--pattern", required=True)
    p_count.add_argument("--cap", type=int, default=100000)
    p_count.set_defaults(func=cmd_count)

    p_window = sub.add_parser("window", parents=[common],
                              help="print a raw byte window at a known offset")
    p_window.add_argument("--offset", required=True,
                          help="decimal or 0x-prefixed byte offset")
    p_window.add_argument("--length", type=int, default=800)
    p_window.add_argument("--marks", type=int, default=0,
                          help="insert [0xADDR] offset markers every N bytes")
    p_window.set_defaults(func=cmd_window)

    p_info = sub.add_parser("info", parents=[common],
                            help="file size and a few anchor hit counts")
    p_info.set_defaults(func=cmd_info)

    p_hd = sub.add_parser(
        "header-debug",
        help="aggregate the kernel's runtime '[header-debug] full headers:' "
             "log lines (no kernel binary needed)")
    p_hd.add_argument("--log-dir", default="",
                      help="kernel log directory (default: $CODEARTS_LOG_DIR or "
                           "~/.codeartsdoer/codearts-data/log)")
    p_hd.add_argument("--max-files", type=int, default=0,
                      help="only scan the newest N log files (0 = all)")
    p_hd.add_argument("--samples", type=int, default=1,
                      help="print the first N records verbatim (masked)")
    p_hd.set_defaults(func=None, is_header_debug=True)

    return parser


def _force_utf8_stdio():
    """Avoid GBK console crashes when a window contains non-UTF8 bytes."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv=None):
    _force_utf8_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.selftest:
        kernel = ""
        if "--kernel" in argv:
            kernel = args.kernel
        elif os.environ.get("CODEARTS_KERNEL", "").strip():
            kernel = os.environ["CODEARTS_KERNEL"].strip()
        return cmd_selftest(kernel or None)

    if not getattr(args, "command", None):
        parser.print_help()
        return 2

    # header-debug reads kernel *logs*, not the bundle, so it must not require
    # a kernel path.
    if getattr(args, "is_header_debug", False):
        return cmd_header_debug(args)

    path = resolve_kernel(getattr(args, "kernel", ""), getattr(args, "kernel_dir", ""))
    with Bundle(path) as bundle:
        return args.func(bundle, args)


if __name__ == "__main__":
    sys.exit(main())
