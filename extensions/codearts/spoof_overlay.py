"""Opt-in, build-only Go overlay for CodeArts IDE outbound header spoofing.

The overlay rewrites two functions of the external codearts2api client at build
time (``go build -overlay``); the checkout stays read-only. Everything it adds is
opt-in: with no User-Agent, no extra header, no timestamp and no OT family the
function returns ``None`` and no file is written, so the default build stays
byte-identical.

Corrections, in the order they are applied:

* ``ChatHeadersV2``: the empty ``traceID`` fallback becomes 32 zero-padded hex
  characters instead of ``%x(UnixNano)``. This only aligns the *length* with the
  value observed from the official client: the value is still a ``UnixNano``
  timestamp and stays predictable;
* ``ChatHeadersV2``: a static header family is appended after the original seven
  headers, in the caller's order, as Go string literals escaped with
  ``json.dumps``. ``timestamp`` can be filled per request with
  ``fmt.Sprintf("%d", time.Now().UnixMilli())`` instead of a frozen literal;
* with ``with_ot_family`` the official per-request family (``x-ot-trace-id``,
  ``x-ot-span-id``, ``x-snap-traceid``, ``user-msg-id``, ``created-time``,
  session ids, ``model-id``/``model-name``, ``client-ip``) is produced by
  injected helpers and written inside ``sendChatV2`` before ``signRequest``, so
  it is covered by ``SignedHeaders`` exactly like the official request;
* with ``with_ot_family`` the three transport headers the official client adds
  *after* signing (``Content-Type``, ``Accept``, ``User-Agent``) are removed
  before ``signRequest`` and re-added right after it, so ``SignedHeaders`` stays
  free of them while the bytes on the wire still look official.

``OFFICIAL_STATIC_HEADERS`` holds the fixed identity headers read verbatim from
the official client's own request log (task-5 evidence). Per-request keys are
never duplicated into that table, and a static entry with the same name as a
dynamic key is dropped so the dynamic value wins.

``OFFICIAL_USER_AGENT`` is the User-Agent the official transport layer sends
(ai-sdk provider-utils 4.0.21 on Bun 1.3.14). It is used as the default only for
OT-family builds and can always be overridden.

Known deviations from the observed official chat request, kept on purpose: the
base seven headers still include ``Accept``/``Content-Type``/``x-auth-token``/
``app-id``/``is_confidential`` (the source function is shared with other
endpoints; only the OT family removes the first two before signing), and
``X-Language: zh-cn`` stays in the base map because the official request sends it
too.
"""
import json
from collections.abc import Mapping
from pathlib import Path
import re


# User-Agent the official transport layer sends: ai-sdk provider-utils 4.0.21
# running on Bun 1.3.14 (task-5 evidence §⑤). It is not part of the business
# header map there either -- it is added after signing -- so the overlay applies
# it only to OT-family builds, where it is written post-sign.
OFFICIAL_USER_AGENT = "ai-sdk/provider-utils/4.0.21 runtime/bun/1.3.14"

# Fixed identity headers of the official chat request, read verbatim from the
# client's own request log (task-5 evidence §③). Only the *static* part belongs
# here: per-request keys are produced by DYNAMIC_OT_HEADERS / ideSpoofDynamic.
# ``X-Language: zh-cn`` is absent on purpose -- ChatHeadersV2 already sends it.
# ``x-ot-function`` is configurable (identity_ide_function) and defaults to
# DEFAULT_OT_FUNCTION; the evidence saw agent-claw / agent-teams /
# agent-general / empty.
OFFICIAL_STATIC_HEADERS = {
    # The official request carries this header with an empty value most of the
    # time (487/1371 empty, 884/1371 equal to the session id; main sessions send
    # the session id itself, which is why "" is the safe default here).
    "x-ot-parent-session-id": "",
    "x-ot-client-type": "IDE",
    "x-ot-client-version": "26.10.2",
    "plugin-name": "codemate_vscode",
    "plugin-version": "26.10.2",
    "ide-name": "VSCode",
    "ide-version": "26.10.2",
}

# Default for x-ot-function; identity_ide_function overrides it per deployment.
DEFAULT_OT_FUNCTION = "agent-claw"

# Header the kernel fills with the IDE session id once per client (``ses_`` plus
# 26 characters: 12 hex + 14 base62).
SESSION_ID_HEADER = "user-session-id"

# Header names that must never be injected: they carry the call credential, are
# produced by the request signer or decide request routing. Matched
# case-insensitively and quoted in the error message so a rejected config says
# why instead of failing silently at build time.
RESERVED_HEADERS = {
    "authorization": "the caller's credential flow owns it",
    "x-auth-token": "the call credential comes from the account token",
    "x-sdk-date": "the request signer writes it immediately before signing",
    "x-sdk-content-sha256": "the request signer writes it immediately before signing",
    "x-security-token": "the request signer writes it immediately before signing",
    # Benefit routing is decided per account when the request is sent, so a
    # frozen value would silently drive the call down the wrong routing path.
    "maas_type": "benefit routing is decided per account at request time",
    # Both ids are set by the backend *after* signRequest, which leaves them out
    # of SignedHeaders; injecting them before signing would make the signed value
    # disagree with the value actually sent on the wire.
    "chat-id": "the backend sets the chat affinity id after signing",
    "session-id": "the backend sets the session id after signing",
    "content-length": "the HTTP transport owns it",
    "host": "the HTTP transport owns it",
    "transfer-encoding": "the HTTP transport owns it",
}

# RFC 7230 token: the only characters a header name may contain.
_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")

# Header names filled per request by the injected ``ideSpoofDynamic`` (lowercase).
# When the OT family is enabled these win over any same-named static entry: the
# dynamic value is the one observed from the official client, and emitting both
# would create two sources of truth for the same request. The fixed identity
# headers above are deliberately *not* listed here -- they stay static.
DYNAMIC_OT_HEADERS = frozenset({
    "x-ot-trace-id", "x-ot-span-id", "x-snap-traceid", "x-ot-session-id",
    "user-session-id", "user-msg-id", "created-time", "model-id", "model-name",
    "client-ip",
})

_TRACE_ID = 'traceID = fmt.Sprintf("%x", time.Now().UnixNano())'
_PADDED_TRACE_ID = 'traceID = fmt.Sprintf("%032x", time.Now().UnixNano())'
_TIMESTAMP = '\theaders["timestamp"] = fmt.Sprintf("%d", time.Now().UnixMilli())\n'
_IMPORTS = ('"fmt"', '"time"')

# The OT family may not add imports, so every identifier it uses must already be
# imported (and under the same name) by the source checkout. ``net`` is the one
# exception: it is added by rewriting the existing ``"net/http"`` import line.
_OT_IMPORTS = ('crand "crypto/rand"', '"crypto/sha256"', '"encoding/hex"',
               '"fmt"', '"net/http"', '"strings"', '"sync/atomic"', '"time"')
_NET_HTTP_IMPORT = '\t"net/http"\n'

# Anchors inside sendChatV2. The static family loop runs before benefit routing
# and before signRequest, so the dynamic family is inserted right after it and is
# therefore signed; the transport headers are deleted there and re-added after
# signRequest, which keeps them out of SignedHeaders like the official client.
_CHAT_HEADERS_LOOP = ('\tfor k, v := range ChatHeadersV2(userToken, traceID, "zh-cn") {\n'
                      '\t\thttpReq.Header.Set(k, v)\n'
                      '\t}')
_SIGN_CALL = '\tsignRequest(httpReq, raw, cred)'
_OT_PRE_SIGN = ('\n\t// The official client adds these three after signing, so they must'
                ' stay out of\n\t// SignedHeaders while still being sent.\n'
                '\thttpReq.Header.Del("Accept")\n'
                '\thttpReq.Header.Del("Content-Type")\n'
                '\tfor k, v := range ideSpoofDynamic(traceID, chatID, sessionID, body) {\n'
                '\t\thttpReq.Header.Set(k, v)\n'
                '\t}')
_OT_POST_SIGN = ('\n\thttpReq.Header.Set("Content-Type", "application/json")\n'
                 '\thttpReq.Header.Set("Accept", "*/*")\n'
                 '\thttpReq.Header.Set("User-Agent", ideSpoofUA)')

_HEADER_RETURN = '''\treturn map[string]string{
\t\t"Content-Type":    "application/json",
\t\t"Accept":          "text/event-stream",
\t\t"x-auth-token":    token,
\t\t"x-snap-traceid":  traceID,
\t\t"X-Language":      language,
\t\t"app-id":          "CodeAgent3.0",
\t\t"is_confidential": "false",
\t}'''

# Appended verbatim to the generated client.go when the OT family is enabled. The
# placeholders carry the configured values; empty means "decide per request".
# Everything here uses identifiers client.go already imports (plus ``net``, see
# _NET_HTTP_IMPORT): crand, sha256, hex, fmt, strings, sync/atomic, time.
_OT_FUNCTIONS = '''// ideSpoofUA is the User-Agent of the official client: ai-sdk provider-utils
// 4.0.21 on Bun 1.3.14. It is written after signing, exactly like the official
// transport layer does.
const ideSpoofUA = __IDE_SPOOF_UA__

// ideSpoofSessionID is the configured IDE session id (identity_ide_user_session_id).
// Empty means the id is derived per conversation.
const ideSpoofSessionID = __IDE_SPOOF_SESSION_ID__

// ideSpoofClientIP is the configured client-ip (identity_ide_client_ip). Empty
// means the local non-loopback IPv4 address is reported, like the official client.
const ideSpoofClientIP = __IDE_SPOOF_CLIENT_IP__

// ideIDCounter supplies the low bits of the timestamp-based id prefix.
var ideIDCounter uint64

// ideID builds the official 26-character id body: 12 hex characters from
// (milliseconds<<12 | counter) followed by 14 base62 random characters. Both the
// ses_ and the msg_ ids are built this way.
func ideID(prefix string) string {
	stamp := (uint64(time.Now().UnixMilli()) << 12) | (atomic.AddUint64(&ideIDCounter, 1) & 0xfff)
	var raw [6]byte
	for index := len(raw) - 1; index >= 0; index-- {
		raw[index] = byte(stamp)
		stamp >>= 8
	}
	random := make([]byte, 14)
	_, _ = crand.Read(random)
	return prefix + hex.EncodeToString(raw[:]) + ideBase62(random, 14)
}

// ideBase62 encodes raw as 0-9A-Za-z base62 and truncates or zero-pads it to n
// characters. Pure byte arithmetic, so the same input always yields the same
// output and the helper stays testable with fixed vectors.
func ideBase62(raw []byte, n int) string {
	const alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
	if n <= 0 {
		return ""
	}
	work := make([]byte, len(raw))
	copy(work, raw)
	digits := make([]byte, 0, len(raw)*2+2)
	for {
		remainder := 0
		for index := range work {
			value := remainder*256 + int(work[index])
			work[index] = byte(value / len(alphabet))
			remainder = value % len(alphabet)
		}
		digits = append(digits, alphabet[remainder])
		nonzero := false
		for _, digit := range work {
			if digit != 0 {
				nonzero = true
				break
			}
		}
		if !nonzero {
			break
		}
	}
	out := make([]byte, 0, n)
	for index := len(digits) - 1; index >= 0 && len(out) < n; index-- {
		out = append(out, digits[index])
	}
	for len(out) < n {
		out = append(out, alphabet[0])
	}
	return string(out)
}

// ideLocalIPv4 returns the first usable local IPv4 address: the interface is up,
// not loopback, the address is not link-local, and RFC1918 private ranges win
// over public ones. An empty string means "nothing to report", in which case the
// client-ip header is skipped.
func ideLocalIPv4() string {
	interfaces, err := net.Interfaces()
	if err != nil {
		return ""
	}
	fallback := ""
	for _, iface := range interfaces {
		if iface.Flags&net.FlagUp == 0 || iface.Flags&net.FlagLoopback != 0 {
			continue
		}
		addresses, err := iface.Addrs()
		if err != nil {
			continue
		}
		for _, address := range addresses {
			ipNet, ok := address.(*net.IPNet)
			if !ok {
				continue
			}
			ip := ipNet.IP.To4()
			if ip == nil || ip.IsLoopback() || ip.IsLinkLocalUnicast() {
				continue
			}
			if ip[0] == 10 || (ip[0] == 172 && ip[1] >= 16 && ip[1] <= 31) || (ip[0] == 192 && ip[1] == 168) {
				return ip.String()
			}
			if fallback == "" {
				fallback = ip.String()
			}
		}
	}
	return fallback
}

// ideSpoofDynamic builds the per-request part of the official IDE chat header
// family. The caller writes it before signRequest, so every key here is covered
// by SignedHeaders, like the official request. Fixed identity headers are
// injected statically instead.
func ideSpoofDynamic(traceID, chatID, sessionID string, body map[string]any) map[string]string {
	if traceID == "" {
		traceID = fmt.Sprintf("%032x", time.Now().UnixNano())
	}
	// The span id and the x-snap-traceid suffix are independent random values in
	// the observed request, so they are drawn separately.
	span := make([]byte, 8)
	_, _ = crand.Read(span)
	suffix := make([]byte, 8)
	_, _ = crand.Read(suffix)
	headers := map[string]string{
		"x-ot-trace-id":  traceID,
		"x-ot-span-id":   hex.EncodeToString(span),
		"x-snap-traceid": traceID + "_" + hex.EncodeToString(suffix),
		"user-msg-id":    ideID("msg_"),
		"created-time":   time.Now().UTC().Format("2006-01-02T15:04:05Z"),
	}
	session := ideSpoofSessionID
	if session == "" && strings.HasPrefix(sessionID, "ses_") {
		session = sessionID
	}
	if session == "" {
		sum := sha256.Sum256([]byte("codearts-ide-session:" + chatID))
		session = "ses_" + hex.EncodeToString(sum[:6]) + ideBase62(sum[6:], 14)
	}
	headers["x-ot-session-id"] = session
	headers["user-session-id"] = session
	ip := ideSpoofClientIP
	if ip == "" {
		ip = ideLocalIPv4()
	}
	if ip != "" {
		headers["client-ip"] = ip
	}
	if model, ok := body["model"].(string); ok && model != "" {
		headers["model-id"] = model
		headers["model-name"] = model
	}
	return headers
}
'''


def _replace(text, old, new, anchor):
    if text.count(old) != 1:
        raise ValueError(
            "Unsupported CodeArts source shape for IDE header spoofing (%s)" % anchor)
    return text.replace(old, new, 1)


def header_name(name):
    """Return a header name, rejecting anything that is not an RFC 7230 token."""
    if not isinstance(name, str):
        raise TypeError("header names must be strings")
    if not _TOKEN.fullmatch(name):
        raise ValueError("header name %r must be an RFC 7230 token "
                         "(letters, digits and !#$%%&'*+-.^_`|~ only)" % (name,))
    return name


def single_line(value, label):
    """Strip a header value and reject anything that could forge a second line.

    Tab is legal inside a value; every other C0 control byte and DEL would
    inject a second header line or corrupt the signed canonical form, and
    U+2028/U+2029 are line terminators for the Go scanner, so a raw copy would
    make the generated client.go unparsable even though they are not C0.
    """
    if not isinstance(value, str):
        raise TypeError("%s must be a string" % label)
    value = value.strip()
    if any((ord(char) < 0x20 and char != "\t") or ord(char) == 0x7F
           or char in "\u2028\u2029" for char in value):
        raise ValueError("%s must be a single header line" % label)
    return value


def validate_header_family(headers, value_limit=None):
    """Return the ordered ``(name, value)`` pairs of an injectable header family.

    Raises TypeError for a non-mapping, and ValueError for anything that would
    break the signed request: a non-token name, a reserved name, a duplicate
    name (case-insensitive), a multi-line value, or a value longer than
    ``value_limit`` characters.
    """
    if not isinstance(headers, Mapping):
        raise TypeError("headers must be a mapping of header names to values")
    pairs, seen = [], set()
    for name, value in headers.items():
        name = header_name(name)
        lowered = name.lower()
        if lowered in RESERVED_HEADERS:
            raise ValueError("header %r is reserved: %s" % (name, RESERVED_HEADERS[lowered]))
        if lowered in seen:
            raise ValueError("duplicate header name %r" % name)
        seen.add(lowered)
        value = single_line(value, "value of header %r" % name)
        if value_limit is not None and len(value) > value_limit:
            raise ValueError("value of header %r must be at most %d characters"
                             % (name, value_limit))
        pairs.append((name, value))
    return pairs


def _set(family, name, value):
    """Set a header, replacing a case-insensitive twin in place to keep the order."""
    name = header_name(name)
    for existing in family:
        if existing.lower() == name.lower():
            family[existing] = value
            return
    family[name] = value


def header_family(overrides=None, session_id="", static_family=True):
    """Build the family: official static table, then overrides, then session id.

    Later sources win case-insensitively while the first spelling and position
    are kept, so the emitted order stays stable. ``overrides`` is the caller's
    mapping (config or environment) and ``session_id`` the observed IDE session
    id, which is the dedicated field for ``SESSION_ID_HEADER`` and therefore
    wins over a same-named entry of ``overrides``.

    ``static_family=False`` leaves ``OFFICIAL_STATIC_HEADERS`` out, which is what
    builds without the OT family use: the official identity headers then never
    reach a UA-only or timestamp-only build, keeping that build byte-identical.
    Values are validated by :func:`chat_headers_overlay` (and by
    :func:`validate_header_family` when a config file is saved).
    """
    if overrides is not None and not isinstance(overrides, Mapping):
        raise TypeError("headers must be a mapping of header names to values")
    if not isinstance(session_id, str):
        raise TypeError("session_id must be a string")
    if not isinstance(static_family, bool):
        raise TypeError("static_family must be true or false")
    family = dict(OFFICIAL_STATIC_HEADERS) if static_family else {}
    for name, value in (overrides or {}).items():
        _set(family, name, value)
    if session_id.strip():
        _set(family, SESSION_ID_HEADER, session_id.strip())
    return family


def _merge_static(family):
    """Put the official static table first, keeping caller values on a name clash."""
    merged = dict(OFFICIAL_STATIC_HEADERS)
    for name, value in family:
        _set(merged, name, value)
    if not any(name.lower() == "x-ot-function" for name in merged):
        merged["x-ot-function"] = DEFAULT_OT_FUNCTION
    return list(merged.items())


def chat_headers_overlay(source, directory, user_agent=None, extra_headers=None,
                         with_timestamp=False, with_ot_family=False, session_id="",
                         client_ip=""):
    """Write the spoofed client.go plus its overlay JSON; return the JSON path.

    Returns None when spoofing is disabled (no User-Agent, no extra headers, no
    timestamp and no OT family), so callers can keep the untouched build and
    nothing is written. ``user_agent`` must be the value observed from the
    official client; for OT-family builds it defaults to ``OFFICIAL_USER_AGENT``
    and is written after signing.

    ``extra_headers`` is emitted after the User-Agent in the caller's order,
    ``with_timestamp`` adds a per-request ``timestamp`` header, and
    ``with_ot_family`` adds the official per-request ``x-ot-*`` family together
    with the post-sign transport headers.

    An explicit ``x-snap-traceid`` in ``extra_headers`` wins over the padded
    trace id while keeping the padding itself; with ``with_ot_family`` it is
    dropped instead, because that value is then generated per request.

    ``session_id`` and ``client_ip`` are the configured official values; they are
    baked into the generated code as constants and fall back to derived / local
    values when left empty. Values are stripped before they are emitted: Go's
    ``canonicalHeaders`` uses TrimSpace, so untrimmed whitespace would make the
    signed value differ from the value actually sent.
    """
    if user_agent is not None and not isinstance(user_agent, str):
        raise TypeError("user_agent must be a string")
    if not isinstance(with_timestamp, bool):
        raise TypeError("with_timestamp must be true or false")
    if not isinstance(with_ot_family, bool):
        raise TypeError("with_ot_family must be true or false")
    session = single_line(session_id, "session_id")
    client = single_line(client_ip, "client_ip")
    user_agent = single_line(user_agent or "", "User-Agent") or None
    if with_ot_family and user_agent is None:
        user_agent = OFFICIAL_USER_AGENT
    family = []
    if not with_ot_family and user_agent is not None:
        family.append(("User-Agent", user_agent))
    if extra_headers is not None:
        family.extend(validate_header_family(extra_headers))
    if with_ot_family:
        # Dynamic keys win: the per-request generator owns them, and User-Agent is
        # written after signing instead of being part of the signed family.
        family = [(name, value) for name, value in family
                  if name.lower() not in DYNAMIC_OT_HEADERS
                  and name.lower() != "user-agent"]
        family = validate_header_family(dict(_merge_static(family)))
    seen = set()
    for name, _ in family:
        if name.lower() in seen:
            raise ValueError("duplicate header name %r" % name)
        seen.add(name.lower())
    if with_timestamp and "timestamp" in seen:
        raise ValueError("header 'timestamp' is generated per request when "
                         "with_timestamp is enabled; drop it from extra_headers")
    if not family and not with_timestamp and not with_ot_family:
        return None
    source, directory = Path(source).resolve(), Path(directory).resolve()
    if directory == source or source in directory.parents:
        raise ValueError("CodeArts spoof output must stay outside the source tree")
    target = source / "internal/upstream/client.go"
    text = target.read_text(encoding="utf-8")
    if with_timestamp and not all(quoted in text for quoted in _IMPORTS):
        raise ValueError("Unsupported CodeArts source shape for IDE header spoofing "
                         "(fmt/time imports)")
    if with_ot_family:
        if "func ideSpoofDynamic(" in text:
            raise ValueError("Unsupported CodeArts source shape for IDE header spoofing "
                             "(ideSpoofDynamic)")
        if not all(quoted in text for quoted in _OT_IMPORTS):
            raise ValueError("Unsupported CodeArts source shape for IDE header spoofing "
                             "(OT imports)")
    text = _replace(text, _TRACE_ID, _PADDED_TRACE_ID, "traceID")
    if family or with_timestamp:
        # Wrap the original seven headers verbatim; only the extra headers are added.
        spoofed = _HEADER_RETURN.replace("return map[string]string{",
                                         "headers := map[string]string{", 1)
        spoofed += "\n" + "".join(
            '\theaders[%s] = %s\n' % (json.dumps(name, ensure_ascii=False),
                                      json.dumps(value, ensure_ascii=False))
            for name, value in family)
        if with_timestamp:
            spoofed += _TIMESTAMP
        spoofed += "\treturn headers"
        text = _replace(text, _HEADER_RETURN, spoofed, "chat headers")
    if with_ot_family:
        # "net" is the only import the generated code needs beyond what the
        # source already has; it is added by splitting the existing net/http line.
        text = _replace(text, _NET_HTTP_IMPORT, '\t"net"\n' + _NET_HTTP_IMPORT, "net import")
        text = _replace(text, _CHAT_HEADERS_LOOP, _CHAT_HEADERS_LOOP + _OT_PRE_SIGN,
                        "sendChatV2 headers")
        text = _replace(text, _SIGN_CALL, _SIGN_CALL + _OT_POST_SIGN, "signRequest call")
        text += "\n" + (_OT_FUNCTIONS
                        .replace("__IDE_SPOOF_UA__", json.dumps(user_agent, ensure_ascii=False))
                        .replace("__IDE_SPOOF_SESSION_ID__", json.dumps(session, ensure_ascii=False))
                        .replace("__IDE_SPOOF_CLIENT_IP__", json.dumps(client, ensure_ascii=False)))
    output = directory / "client.go"
    overlay = directory / "spoof-overlay.json"
    # Encode before mkdir so a failure never leaves an empty build directory.
    payload = text.encode("utf-8")
    overlay_payload = (json.dumps({"Replace": {str(target): str(output)}},
                                  indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    directory.mkdir(parents=True, exist_ok=True)
    # Byte writes keep LF on Windows; Path.write_text would emit CRLF there.
    output.write_bytes(payload)
    overlay.write_bytes(overlay_payload)
    return overlay
