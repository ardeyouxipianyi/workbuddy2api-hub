"""Build-only Go overlay contract for CodeArts IDE header spoofing.

Assertions target the on-disk contract instead of implementation details: the
exact bytes of the generated ``client.go``, the ``Replace`` map handed to
``go build -overlay``, and the merged overlay. Only a temporary mini source
tree and a temporary build directory are written; the real external checkout
is read-only and is used purely for wiring assertions.

No Go toolchain is involved anywhere: the injected header family and the
dynamic timestamp are pinned down as generated text, never as a successful
build.
"""
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import extensions.codearts.backend as backend
from extensions.codearts import backend_overlay, spoof_overlay
from extensions.codearts.config import DEFAULTS, BridgeConfig
from extensions.codearts.spoof_overlay import (
    chat_headers_overlay, header_family, validate_header_family)


def _real_source():
    """Locate a read-only codearts2api checkout without a machine-specific path.

    ``CODEARTS2API_SRC`` wins so CI and other machines can point at their own
    checkout; otherwise the sibling directory next to this repository is
    probed. Returns None when neither candidate looks like the Go module, so
    the wiring cases skip instead of failing on someone else's machine.
    """
    candidates = []
    configured = os.environ.get('CODEARTS2API_SRC')
    if configured:
        candidates.append(Path(configured))
    candidates.append(ROOT.parent / 'codearts2api-main')
    for candidate in candidates:
        try:
            if (candidate / 'go.mod').is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


REAL_SOURCE = _real_source()


SPOOFED_TRACE = 'traceID = fmt.Sprintf("%032x", time.Now().UnixNano())'
SOURCE_TRACE = '\ttraceID = fmt.Sprintf("%x", time.Now().UnixNano())\n'
SHAPE_ERROR = 'Unsupported CodeArts source shape for IDE header spoofing'
HEADER_LINE_ERROR = 'User-Agent must be a single header line'
SOURCE_DIR_ERROR = 'CodeArts spoof output must stay outside the source tree'

HEADER_FIELDS = (
    '\t\t"Content-Type":    "application/json",\n'
    '\t\t"Accept":          "text/event-stream",\n'
    '\t\t"x-auth-token":    token,\n'
    '\t\t"x-snap-traceid":  traceID,\n'
    '\t\t"X-Language":      language,\n'
    '\t\t"app-id":          "CodeAgent3.0",\n'
    '\t\t"is_confidential": "false",\n'
)

CLIENT_HEAD = (
    'package upstream\n'
    '\n'
    'import (\n'
    '\t"fmt"\n'
    '\t"time"\n'
    ')\n'
    '\n'
    'func chatHeaders(token, language string) map[string]string {\n'
)

# Tab indentation and LF endings mirror the real upstream client.go shape.
SOURCE_CLIENT = (
    CLIENT_HEAD
    + SOURCE_TRACE
    + '\treturn map[string]string{\n'
    + HEADER_FIELDS
    + '\t}\n'
    '}\n'
)

CLIENT = Path('internal/upstream/client.go')
USER_AGENT_LINE = '\theaders["User-Agent"] = '
TYPICAL_USER_AGENT = 'CodeArts IDE/1.0 (Windows NT 10.0; Win64; x64)'


def expected_client(user_agent):
    """Full expected client.go text, byte for byte, for a given User-Agent."""
    return (
        CLIENT_HEAD
        + '\t' + SPOOFED_TRACE + '\n'
        + '\theaders := map[string]string{\n'
        + HEADER_FIELDS
        + '\t}\n'
        + USER_AGENT_LINE + json.dumps(user_agent, ensure_ascii=False) + '\n'
        '\treturn headers\n'
        '}\n'
    )


def injected_user_agent(text):
    """Recover the injected User-Agent by decoding the emitted Go literal."""
    for line in text.split('\n'):
        if line.startswith(USER_AGENT_LINE):
            return json.loads(line[len(USER_AGENT_LINE):])
    raise AssertionError('no injected User-Agent header line in generated client.go')


class SpoofOverlayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / 'codearts2api'
        (self.source / 'internal' / 'upstream').mkdir(parents=True)
        self.write_client(SOURCE_CLIENT)
        self.directory = self.root / 'build' / 'spoof'

    def write_client(self, text):
        (self.source / CLIENT).write_bytes(text.encode('utf-8'))

    def assert_disabled(self, user_agent):
        self.assertIsNone(chat_headers_overlay(self.source, self.directory, user_agent))
        self.assertFalse(self.directory.exists(), 'disabled spoofing must not write files')

    def test_blank_user_agent_disables_spoofing(self):
        # Guard order: whitespace-only values (bare CR/LF are stripped by
        # str.strip) mean "not configured" and win over the control-character
        # check, so they disable spoofing instead of raising.
        for user_agent in (None, '', '   ', '\t', '\r', '\n', '\r\n\t '):
            with self.subTest(user_agent=user_agent):
                self.assert_disabled(user_agent)

    def test_non_string_user_agent_is_rejected(self):
        for user_agent in (123, 0, ['CodeArts'], b'CodeArts'):
            with self.subTest(user_agent=user_agent):
                with self.assertRaisesRegex(TypeError, re.escape('user_agent must be a string')):
                    chat_headers_overlay(self.source, self.directory, user_agent)
                self.assertFalse(self.directory.exists())

    def test_typical_user_agent_writes_lf_client_and_overlay(self):
        overlay = chat_headers_overlay(self.source, self.directory, TYPICAL_USER_AGENT)
        self.assertEqual(overlay, (self.directory / 'spoof-overlay.json').resolve())
        raw = (self.directory / 'client.go').read_bytes()
        self.assertNotIn(b'\r', raw, 'generated Go file must stay LF-only')
        text = raw.decode('utf-8')
        self.assertEqual(text, expected_client(TYPICAL_USER_AGENT))
        self.assertEqual(text.count(SPOOFED_TRACE), 1)
        self.assertNotIn('return map[string]string{', text)
        self.assertEqual(injected_user_agent(text), TYPICAL_USER_AGENT)
        self.assertTrue(overlay.read_bytes().endswith(b'\n'),
                        'overlay JSON must end with a newline')

    def test_surrounding_whitespace_is_stripped(self):
        # canonicalHeaders trims header values, so the emitted literal must be
        # the trimmed value or the signature would not match what is sent.
        chat_headers_overlay(self.source, self.directory, '  IDE/1.0  ')
        text = (self.directory / 'client.go').read_bytes().decode('utf-8')
        self.assertEqual(injected_user_agent(text), 'IDE/1.0')
        self.assertEqual(text, expected_client('IDE/1.0'))

    def test_user_agent_with_quotes_and_backslashes_is_json_escaped(self):
        user_agent = 'CodeArts "IDE" C:\\Program Files\\x\tv2'
        chat_headers_overlay(self.source, self.directory, user_agent)
        text = (self.directory / 'client.go').read_bytes().decode('utf-8')
        for line in text.split('\n'):
            if line.startswith(USER_AGENT_LINE):
                literal = line[len(USER_AGENT_LINE):]
                break
        else:
            self.fail('no injected User-Agent header line in generated client.go')
        # The literal must round-trip to the exact original value.
        self.assertEqual(json.loads(literal), user_agent)
        self.assertEqual(literal, json.dumps(user_agent, ensure_ascii=False))
        self.assertIn('\\"IDE\\"', literal)
        self.assertIn('C:\\\\Program Files', literal)

    def test_non_ascii_user_agent_kept_verbatim(self):
        user_agent = 'CodeArts IDE/1.0 中文客户端'
        chat_headers_overlay(self.source, self.directory, user_agent)
        text = (self.directory / 'client.go').read_bytes().decode('utf-8')
        self.assertEqual(injected_user_agent(text), user_agent)
        self.assertIn('中文客户端', text)
        self.assertNotIn('\\u4e2d', text, 'non-ASCII must not be \\uXXXX-escaped')

    def test_control_characters_rejected(self):
        # A bare "\r"/"\n" is whitespace-only and therefore disables spoofing;
        # every embedded C0 byte except tab, plus DEL, reaches the header check
        # (bare \x00/\x01/\x7f are not whitespace and are rejected too).
        for index, user_agent in enumerate(('bad\rvalue', 'bad\nvalue', 'bad\x00value',
                                            'bad\x01value', 'bad\x1fvalue', 'bad\x7fvalue',
                                            '\x00', '\x01', '\x7f')):
            with self.subTest(user_agent=repr(user_agent)):
                directory = self.root / ('rejected-%d' % index)
                with self.assertRaisesRegex(ValueError, re.escape(HEADER_LINE_ERROR)):
                    chat_headers_overlay(self.source, directory, user_agent)
                self.assertFalse(directory.exists())

    def test_line_separator_characters_rejected(self):
        # U+2028/U+2029 terminate a line for the Go scanner, so a raw copy would
        # make the generated client.go unparsable even though they are not C0.
        for index, user_agent in enumerate(('IDE\u2028X', 'IDE\u2029X')):
            with self.subTest(user_agent=repr(user_agent)):
                directory = self.root / ('separator-%d' % index)
                with self.assertRaisesRegex(ValueError, re.escape(HEADER_LINE_ERROR)):
                    chat_headers_overlay(self.source, directory, user_agent)
                self.assertFalse(directory.exists())

    def test_output_directory_inside_source_tree_rejected(self):
        # The overlay must not overwrite the read-only external checkout.
        for directory in (self.source, self.source / 'build' / 'spoof'):
            with self.subTest(directory=str(directory)):
                with self.assertRaisesRegex(ValueError, re.escape(SOURCE_DIR_ERROR)):
                    chat_headers_overlay(self.source, directory, TYPICAL_USER_AGENT)
        self.assertFalse((self.source / 'build').exists())

    def test_missing_header_field_rejected(self):
        self.write_client(SOURCE_CLIENT.replace('\t\t"X-Language":      language,\n', ''))
        with self.assertRaisesRegex(ValueError, re.escape(SHAPE_ERROR + ' (chat headers)')):
            chat_headers_overlay(self.source, self.directory, TYPICAL_USER_AGENT)
        self.assertFalse(self.directory.exists())

    def test_duplicated_trace_line_rejected(self):
        self.write_client(SOURCE_CLIENT.replace(SOURCE_TRACE, SOURCE_TRACE + SOURCE_TRACE))
        with self.assertRaisesRegex(ValueError, re.escape(SHAPE_ERROR + ' (traceID)')):
            chat_headers_overlay(self.source, self.directory, TYPICAL_USER_AGENT)
        self.assertFalse(self.directory.exists())

    def test_already_spoofed_source_rejected(self):
        self.write_client(SOURCE_CLIENT.replace(
            '\t' + 'traceID = fmt.Sprintf("%x", time.Now().UnixNano())',
            '\t' + SPOOFED_TRACE))
        with self.assertRaisesRegex(ValueError, re.escape(SHAPE_ERROR + ' (traceID)')):
            chat_headers_overlay(self.source, self.directory, TYPICAL_USER_AGENT)
        self.assertFalse(self.directory.exists())

    def test_missing_source_file_rejected_before_any_output(self):
        (self.source / CLIENT).unlink()
        with self.assertRaises(FileNotFoundError):
            chat_headers_overlay(self.source, self.directory, TYPICAL_USER_AGENT)
        self.assertFalse(self.directory.exists())

    def test_overlay_json_replace_maps_absolute_paths(self):
        overlay = chat_headers_overlay(self.source, self.directory, TYPICAL_USER_AGENT)
        raw = overlay.read_bytes()
        self.assertNotIn(b'\r', raw, 'overlay JSON must stay LF-only')
        self.assertTrue(raw.endswith(b'\n'), 'overlay JSON must end with a newline')
        expected = {'Replace': {
            str((self.source / CLIENT).resolve()): str((self.directory / 'client.go').resolve()),
        }}
        self.assertEqual(json.loads(raw), expected)
        self.assertEqual(len(expected['Replace']), 1)


class BackendBuildOverlayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.output = self.root / 'bin' / 'server.exe'

    def write_payload(self, name, payload):
        path = self.root / name
        path.write_bytes((json.dumps(payload, indent=2, ensure_ascii=False)
                          + '\n').encode('utf-8'))
        return path

    def write_overlay(self, name, replacements):
        return self.write_payload(name, {'Replace': replacements})

    def make_minimal_source(self):
        """Minimal CodeArts-shaped tree so build wiring runs without a checkout."""
        source = self.root / 'source'
        (source / 'internal' / 'upstream').mkdir(parents=True)
        (source / 'go.mod').write_bytes(b'module codearts2api\n')
        (source / CLIENT).write_bytes(SOURCE_CLIENT.encode('utf-8'))
        return source

    def test_merge_overlays_keeps_replacements_from_both_sides(self):
        # The same key may repeat only with the same value; see the conflict case.
        first = self.write_overlay('first.json', {'a.go': 'a.out', 'shared.go': 'shared.out'})
        second = self.write_overlay('second.json', {'b.go': 'b.out', 'shared.go': 'shared.out'})
        target = self.root / 'merged.json'
        result = backend._merge_overlays((first, second), target)
        self.assertEqual(result, target)
        raw = target.read_bytes()
        self.assertNotIn(b'\r', raw)
        self.assertTrue(raw.endswith(b'\n'))
        self.assertEqual(json.loads(raw), {'Replace': {
            'a.go': 'a.out',
            'b.go': 'b.out',
            'shared.go': 'shared.out',
        }})

    def test_merge_overlays_without_inputs_writes_empty_register(self):
        target = self.root / 'empty.json'
        backend._merge_overlays((), target)
        raw = target.read_bytes()
        self.assertEqual(json.loads(raw), {'Replace': {}})
        self.assertTrue(raw.endswith(b'\n'))

    def test_merge_overlays_rejects_missing_replace_key(self):
        missing = self.write_payload('missing.json', {'NotReplace': {}})
        target = self.root / 'merged.json'
        with self.assertRaisesRegex(ValueError, re.escape('has no Replace object')):
            backend._merge_overlays((missing,), target)
        self.assertFalse(target.exists())

    def test_merge_overlays_rejects_non_object_replace(self):
        broken = self.write_payload('broken.json', {'Replace': ['a.go']})
        target = self.root / 'merged.json'
        with self.assertRaisesRegex(ValueError, re.escape('has no Replace object')):
            backend._merge_overlays((broken,), target)
        self.assertFalse(target.exists())

    def test_merge_overlays_rejects_non_string_replacements(self):
        broken = self.write_payload('broken.json', {'Replace': {'a.go': 1}})
        target = self.root / 'merged.json'
        with self.assertRaisesRegex(ValueError, re.escape('must map strings to strings')):
            backend._merge_overlays((broken,), target)
        self.assertFalse(target.exists())

    def test_merge_overlays_rejects_conflicting_replacements(self):
        first = self.write_overlay('first.json', {'shared.go': 'from-first'})
        second = self.write_overlay('second.json', {'shared.go': 'from-second'})
        target = self.root / 'merged.json'
        with self.assertRaisesRegex(ValueError, re.escape('Conflicting overlay replacements')):
            backend._merge_overlays((first, second), target)
        self.assertFalse(target.exists())

    def test_build_backend_rejects_foreign_source_directory(self):
        source = self.root / 'foreign'
        source.mkdir()
        (source / 'go.mod').write_bytes(b'module example.com/other\n')
        with self.assertRaisesRegex(ValueError,
                                    re.escape('Expected the CodeArts2API source directory')):
            backend.build_backend(source, self.output)
        self.assertFalse(self.output.parent.exists())

    def test_build_backend_uses_user_agent_environment_fallback(self):
        # No explicit argument: the environment variable is the documented
        # opt-in entry point and must reach the spoof overlay.
        source = self.make_minimal_source()
        default_overlay = self.write_overlay('default.json', {'models.go': 'models.out'})
        with mock.patch.dict(os.environ, {'CODEARTS_IDE_USER_AGENT': TYPICAL_USER_AGENT}), \
                mock.patch.object(backend_overlay, 'model_id_overlay',
                                  return_value=default_overlay), \
                mock.patch.object(backend.subprocess, 'run') as run:
            result = backend.build_backend(source, self.output, go='fake-go',
                                           spoof_user_agent=None)
        build = self.output.parent / 'build-overlay'
        merged_path = build / 'overlay-merged.json'
        run.assert_called_once_with(
            ['fake-go', 'build', '-overlay', str(merged_path),
             '-o', str(self.output.resolve()), './cmd/server'],
            cwd=str(source.resolve()), check=True)
        self.assertEqual(result, self.output.resolve())
        raw = merged_path.read_bytes()
        self.assertNotIn(b'\r', raw)
        self.assertTrue(raw.endswith(b'\n'))
        merged = json.loads(raw)['Replace']
        client_key = str((source / CLIENT).resolve())
        spoofed_client = build / 'spoof' / 'client.go'
        self.assertEqual(merged, {'models.go': 'models.out',
                                  client_key: str(spoofed_client)})
        self.assertTrue(spoofed_client.is_file())
        text = spoofed_client.read_bytes().decode('utf-8')
        self.assertEqual(injected_user_agent(text), TYPICAL_USER_AGENT)

    def test_build_backend_blank_explicit_user_agent_uses_environment(self):
        # Clearing the UI field sends a blank string; it must behave like "not
        # supplied" instead of silently suppressing the documented env fallback.
        source = self.make_minimal_source()
        default_overlay = self.write_overlay('default.json', {'models.go': 'models.out'})
        with mock.patch.dict(os.environ, {'CODEARTS_IDE_USER_AGENT': TYPICAL_USER_AGENT}), \
                mock.patch.object(backend_overlay, 'model_id_overlay',
                                  return_value=default_overlay), \
                mock.patch.object(backend.subprocess, 'run'):
            backend.build_backend(source, self.output, go='fake-go', spoof_user_agent='   ')
        spoofed_client = self.output.parent / 'build-overlay' / 'spoof' / 'client.go'
        self.assertTrue(spoofed_client.is_file())
        self.assertEqual(injected_user_agent(spoofed_client.read_bytes().decode('utf-8')),
                         TYPICAL_USER_AGENT)

    @unittest.skipUnless(REAL_SOURCE is not None, 'codearts2api source checkout unavailable')
    def test_build_backend_defaults_to_no_spoof_overlay(self):
        # The environment fallback must stay off: an ambient value would silently
        # enable spoofing, so every entry point is cleared here. The header and
        # session variables are popped too: a machine or CI job that exports them
        # would otherwise make this "default off" case look enabled.
        with mock.patch.dict(os.environ):
            for name in ENV_ENTRY_POINTS:
                os.environ.pop(name, None)
            with mock.patch.object(backend.subprocess, 'run') as run:
                result = backend.build_backend(REAL_SOURCE, self.output, go='fake-go')
        build = self.output.parent / 'build-overlay'
        run.assert_called_once_with(
            ['fake-go', 'build', '-overlay', str(build / 'overlay.json'),
             '-o', str(self.output.resolve()), './cmd/server'],
            cwd=str(REAL_SOURCE.resolve()), check=True)
        self.assertEqual(result, self.output.resolve())
        self.assertFalse((build / 'spoof').exists(),
                         'blank User-Agent must not generate any spoof artifacts')
        replacements = json.loads((build / 'overlay.json').read_text(encoding='utf-8'))['Replace']
        self.assertNotIn(str((REAL_SOURCE / CLIENT).resolve()), replacements)
        self.assertFalse((build / 'overlay-merged.json').exists())

    @unittest.skipUnless(REAL_SOURCE is not None, 'codearts2api source checkout unavailable')
    def test_build_backend_merges_spoof_overlay_when_user_agent_given(self):
        with mock.patch.object(backend.subprocess, 'run'):
            backend.build_backend(REAL_SOURCE, self.output, go='fake-go')
        build = self.output.parent / 'build-overlay'
        default = json.loads((build / 'overlay.json').read_text(encoding='utf-8'))['Replace']

        with mock.patch.object(backend.subprocess, 'run') as run:
            backend.build_backend(REAL_SOURCE, self.output, go='fake-go',
                                  spoof_user_agent=TYPICAL_USER_AGENT)
        merged_path = build / 'overlay-merged.json'
        run.assert_called_once_with(
            ['fake-go', 'build', '-overlay', str(merged_path),
             '-o', str(self.output.resolve()), './cmd/server'],
            cwd=str(REAL_SOURCE.resolve()), check=True)
        raw = merged_path.read_bytes()
        self.assertNotIn(b'\r', raw)
        self.assertTrue(raw.endswith(b'\n'))
        merged = json.loads(raw)['Replace']
        client_key = str((REAL_SOURCE / CLIENT).resolve())
        # Enabled spoofing adds exactly one replacement and keeps every default one.
        self.assertEqual(set(merged) - set(default), {client_key})
        self.assertEqual(set(default) - set(merged), set())
        spoofed_client = Path(merged[client_key])
        self.assertEqual(spoofed_client, (build / 'spoof' / 'client.go').resolve())
        text = spoofed_client.read_bytes().decode('utf-8')
        self.assertEqual(injected_user_agent(text), TYPICAL_USER_AGENT)
        self.assertEqual(text.count(SPOOFED_TRACE), 1)


FAKE_HEADERS = {
    'x-client-type': 'kernel',
    'plugin-version': '0.0.0-test',
}
# Synthetic, obviously fake IDE session id: the real ``ses_`` shape without
# anybody's actual session.
FAKE_SESSION_ID = 'ses_' + '0' * 26
TIMESTAMP_LINE = '\theaders["timestamp"] = fmt.Sprintf("%d", time.Now().UnixMilli())\n'
ENV_ENTRY_POINTS = ('CODEARTS_IDE_USER_AGENT', 'CODEARTS_IDE_HEADERS',
                    'CODEARTS_IDE_SESSION_ID')


def expected_family_client(rows, timestamp=False):
    """Full expected client.go text for injected ``(name, literal)`` rows."""
    body = ''.join('\theaders[%s] = %s\n' % (json.dumps(name, ensure_ascii=False), literal)
                   for name, literal in rows)
    if timestamp:
        body += TIMESTAMP_LINE
    return (
        CLIENT_HEAD
        + '\t' + SPOOFED_TRACE + '\n'
        + '\theaders := map[string]string{\n'
        + HEADER_FIELDS
        + '\t}\n'
        + body
        + '\treturn headers\n'
        '}\n'
    )


def injected_headers(text):
    """Recover every injected header line as ``(name, Go literal or expression)``.

    The dynamic timestamp keeps its Go expression instead of a literal, so the
    helper never tries to JSON-decode it.
    """
    rows = []
    for line in text.split('\n'):
        if line.startswith('\theaders['):
            name, _, value = line[len('\theaders['):].partition('] = ')
            rows.append((json.loads(name), value))
    return rows


def injected_literals(text):
    """Static header literals only; per-request assignments hold Go expressions."""
    return [(name, value) for name, value in injected_headers(text)
            if value.startswith('"') and value.endswith('"')]


def fake_rows(*headers):
    """Expected rows for fake header mappings, in insertion order."""
    return [(name, json.dumps(value, ensure_ascii=False)) for name, value in headers]


class IdentityHeaderFamilyTest(unittest.TestCase):
    """Static header family plus dynamic timestamp, asserted as generated text."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / 'codearts2api'
        (self.source / 'internal' / 'upstream').mkdir(parents=True)
        self.write_client(SOURCE_CLIENT)
        self.directory = self.root / 'build' / 'spoof'

    def write_client(self, text):
        (self.source / CLIENT).write_bytes(text.encode('utf-8'))

    def generate(self, **kwargs):
        overlay = chat_headers_overlay(self.source, self.directory, **kwargs)
        self.assertIsNotNone(overlay)
        raw = (self.directory / 'client.go').read_bytes()
        self.assertNotIn(b'\r', raw, 'generated Go file must stay LF-only')
        return raw.decode('utf-8')

    def test_nothing_configured_writes_no_files(self):
        # Default off: no User-Agent, no extra headers, no timestamp.
        for kwargs in ({}, {'extra_headers': {}}, {'extra_headers': None,
                                                   'with_timestamp': False}):
            with self.subTest(kwargs=kwargs):
                self.assertIsNone(chat_headers_overlay(self.source, self.directory, **kwargs))
                self.assertFalse(self.directory.exists(),
                                 'disabled spoofing must not write files')

    def test_extra_headers_follow_the_user_agent_in_caller_order(self):
        text = self.generate(user_agent=TYPICAL_USER_AGENT, extra_headers=FAKE_HEADERS)
        rows = [('User-Agent', json.dumps(TYPICAL_USER_AGENT, ensure_ascii=False))]
        rows += fake_rows(*FAKE_HEADERS.items())
        self.assertEqual(text, expected_family_client(rows))
        self.assertEqual(injected_headers(text), rows)
        self.assertNotIn('return map[string]string{', text)
        self.assertEqual(text.count(SPOOFED_TRACE), 1)

    def test_extra_headers_alone_enable_spoofing(self):
        text = self.generate(extra_headers=FAKE_HEADERS)
        self.assertEqual(injected_headers(text), fake_rows(*FAKE_HEADERS.items()))
        self.assertNotIn('User-Agent', text)

    def test_extra_header_values_are_json_escaped_and_kept_verbatim(self):
        value = 'a "quoted" C:\\path\t中文'
        text = self.generate(extra_headers={'x-client-type': value})
        self.assertEqual(injected_headers(text),
                         [('x-client-type', json.dumps(value, ensure_ascii=False))])
        self.assertIn('\\"quoted\\"', text)
        self.assertIn('C:\\\\path', text)
        self.assertIn('中文', text)
        self.assertNotIn('\\u4e2d', text, 'non-ASCII must not be \\uXXXX-escaped')

    def test_extra_header_values_are_stripped(self):
        # canonicalHeaders trims values, so the emitted literal must be the
        # trimmed value or the signature would not match what is sent.
        text = self.generate(extra_headers={'x-client-type': '  kernel  '})
        self.assertEqual(injected_headers(text), [('x-client-type', '"kernel"')])

    def test_reserved_header_names_rejected(self):
        for name in sorted(spoof_overlay.RESERVED_HEADERS):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, 'is reserved') as caught:
                    chat_headers_overlay(self.source, self.directory / name,
                                         extra_headers={name.upper(): 'value'})
                self.assertIn(spoof_overlay.RESERVED_HEADERS[name], str(caught.exception))
        self.assertFalse(self.directory.exists())

    def test_routing_and_post_signing_headers_are_reserved(self):
        # maas_type decides benefit routing per account; chat-id/session-id are
        # set by the backend *after* signRequest, so injecting them before
        # signing would put a value into SignedHeaders that the wire contradicts.
        for name in ('maas_type', 'chat-id', 'session-id'):
            with self.subTest(name=name):
                self.assertIn(name, spoof_overlay.RESERVED_HEADERS)
                with self.assertRaisesRegex(ValueError, 'is reserved'):
                    chat_headers_overlay(self.source, self.directory, extra_headers={name: 'x'})
        self.assertFalse(self.directory.exists())

    def test_non_token_header_names_rejected(self):
        for name in ('', 'bad name', 'bad:name', 'bad\rname', 'bad,name',
                     'h\u00e9ader', ' bad'):
            with self.subTest(name=repr(name)):
                with self.assertRaisesRegex(ValueError, 'must be an RFC 7230 token'):
                    chat_headers_overlay(self.source, self.directory,
                                         extra_headers={name: 'value'})
        for name in (5, None, b'x'):
            with self.subTest(name=repr(name)):
                with self.assertRaisesRegex(TypeError, 'header names must be strings'):
                    chat_headers_overlay(self.source, self.directory,
                                         extra_headers={name: 'value'})
        self.assertFalse(self.directory.exists())

    def test_multiline_and_separator_values_rejected(self):
        for value in ('a\rb', 'a\nb', 'a\x00b', 'a\x01b', 'a\x7fb',
                      'a\u2028b', 'a\u2029b'):
            with self.subTest(value=repr(value)):
                with self.assertRaisesRegex(ValueError, 'must be a single header line'):
                    chat_headers_overlay(self.source, self.directory,
                                         extra_headers={'x-client-type': value})
        self.assertFalse(self.directory.exists())

    def test_non_string_values_rejected(self):
        for value in (1, None, ['x']):
            with self.subTest(value=repr(value)):
                with self.assertRaisesRegex(TypeError, 'must be a string'):
                    chat_headers_overlay(self.source, self.directory,
                                         extra_headers={'x-client-type': value})
        self.assertFalse(self.directory.exists())

    def test_duplicate_header_names_rejected_case_insensitively(self):
        with self.assertRaisesRegex(ValueError,
                                    re.escape("duplicate header name 'x-client-type'")):
            chat_headers_overlay(self.source, self.directory,
                                 extra_headers={'X-Client-Type': 'a', 'x-client-type': 'b'})
        self.assertFalse(self.directory.exists())

    def test_user_agent_conflicts_with_a_same_named_extra_header(self):
        with self.assertRaisesRegex(ValueError,
                                    re.escape("duplicate header name 'user-agent'")):
            chat_headers_overlay(self.source, self.directory,
                                 user_agent=TYPICAL_USER_AGENT,
                                 extra_headers={'user-agent': 'other'})
        self.assertFalse(self.directory.exists())

    def test_extra_headers_must_be_a_mapping(self):
        for value in ([('x-client-type', 'kernel')], 'x-client-type: kernel', 5):
            with self.subTest(value=repr(value)):
                with self.assertRaisesRegex(TypeError, 'must be a mapping'):
                    chat_headers_overlay(self.source, self.directory, extra_headers=value)
        self.assertFalse(self.directory.exists())

    def test_timestamp_is_injected_once_without_any_static_header(self):
        text = self.generate(with_timestamp=True)
        self.assertEqual(text, expected_family_client([], timestamp=True))
        self.assertEqual(text.count(TIMESTAMP_LINE), 1)
        self.assertNotIn('return map[string]string{', text)
        # No import may be added: the patched function still uses exactly the
        # fmt/time imports the source already had.
        self.assertEqual(text.count('"fmt"'), 1)
        self.assertEqual(text.count('"time"'), 1)

    def test_timestamp_is_the_last_injected_line(self):
        text = self.generate(user_agent=TYPICAL_USER_AGENT, extra_headers=FAKE_HEADERS,
                             with_timestamp=True)
        rows = [('User-Agent', json.dumps(TYPICAL_USER_AGENT, ensure_ascii=False))]
        rows += fake_rows(*FAKE_HEADERS.items())
        self.assertEqual(text, expected_family_client(rows, timestamp=True))
        lines = [line for line in text.split('\n') if line.startswith('\theaders[')]
        self.assertEqual(lines[-1], TIMESTAMP_LINE.rstrip('\n'))
        self.assertEqual(lines.count(TIMESTAMP_LINE.rstrip('\n')), 1)

    def test_frozen_timestamp_header_conflicts_with_the_dynamic_one(self):
        with self.assertRaisesRegex(ValueError,
                                    re.escape("header 'timestamp' is generated per request")):
            chat_headers_overlay(self.source, self.directory,
                                 extra_headers={'Timestamp': '1700000000000'},
                                 with_timestamp=True)
        self.assertFalse(self.directory.exists())

    def test_timestamp_flag_must_be_boolean(self):
        for value in (1, 'true', None, []):
            with self.subTest(value=repr(value)):
                with self.assertRaisesRegex(TypeError, 'with_timestamp must be true or false'):
                    chat_headers_overlay(self.source, self.directory, with_timestamp=value)
        self.assertFalse(self.directory.exists())

    def test_timestamp_requires_the_fmt_and_time_imports(self):
        for quoted in ('\t"time"\n', '\t"fmt"\n'):
            with self.subTest(import_=quoted.strip()):
                self.write_client(SOURCE_CLIENT.replace(quoted, '', 1))
                with self.assertRaisesRegex(ValueError, re.escape('(fmt/time imports)')):
                    chat_headers_overlay(self.source, self.directory, with_timestamp=True)
                self.assertFalse(self.directory.exists())
                self.write_client(SOURCE_CLIENT)

    def test_explicit_trace_id_overrides_the_padded_fallback(self):
        trace = '0123456789abcdef0123456789abcdef'
        text = self.generate(extra_headers={'x-snap-traceid': trace})
        assignments = [line for line in text.split('\n')
                       if line.startswith('\theaders["x-snap-traceid"] = ')]
        self.assertEqual(assignments,
                         ['\theaders["x-snap-traceid"] = ' + json.dumps(trace)])
        # The padded fallback is kept (only one padding change) and the original
        # map entry still consumes traceID.
        self.assertEqual(text.count(SPOOFED_TRACE), 1)
        self.assertIn('"x-snap-traceid":  traceID,', text)

    def test_output_directory_inside_source_tree_rejected(self):
        for directory in (self.source, self.source / 'build' / 'spoof'):
            with self.subTest(directory=str(directory)):
                with self.assertRaisesRegex(ValueError, re.escape(SOURCE_DIR_ERROR)):
                    chat_headers_overlay(self.source, directory, extra_headers=FAKE_HEADERS)
        self.assertFalse((self.source / 'build').exists())

    def test_official_table_entries_stay_injectable(self):
        # Backfill-friendly guard: whatever the evidence file eventually pins
        # down must pass the same validation as a caller-provided family.
        pairs = validate_header_family(spoof_overlay.OFFICIAL_STATIC_HEADERS)
        self.assertEqual([name for name, _ in pairs],
                         list(spoof_overlay.OFFICIAL_STATIC_HEADERS))

    def test_header_family_copies_the_table_and_keeps_order(self):
        family = header_family()
        self.assertEqual(list(family), list(spoof_overlay.OFFICIAL_STATIC_HEADERS))
        self.assertIsNot(family, spoof_overlay.OFFICIAL_STATIC_HEADERS)
        family['zeta'] = 'z'
        self.assertNotIn('zeta', spoof_overlay.OFFICIAL_STATIC_HEADERS)
        merged = header_family({'zeta': 'z', 'alpha': 'a'})
        self.assertEqual(list(merged)[-2:], ['zeta', 'alpha'])

    def test_header_family_session_id_wins_over_a_same_named_entry(self):
        family = header_family(session_id=FAKE_SESSION_ID)
        self.assertEqual(family[spoof_overlay.SESSION_ID_HEADER], FAKE_SESSION_ID)
        family = header_family({spoof_overlay.SESSION_ID_HEADER: 'from-map'},
                               FAKE_SESSION_ID)
        self.assertEqual(family[spoof_overlay.SESSION_ID_HEADER], FAKE_SESSION_ID)
        family = header_family({spoof_overlay.SESSION_ID_HEADER: 'from-map'})
        self.assertEqual(family[spoof_overlay.SESSION_ID_HEADER], 'from-map')
        family = header_family({'X-Client-Type': 'kernel'}, '')
        listed = {name.lower(): value for name, value in family.items()}
        self.assertEqual(listed['x-client-type'], 'kernel')

    def test_header_family_rejects_wrong_types(self):
        for value in ([], 'x-client-type: kernel', 5):
            with self.subTest(value=repr(value)):
                with self.assertRaisesRegex(TypeError, 'must be a mapping'):
                    header_family(value)
        with self.assertRaisesRegex(TypeError, 'session_id must be a string'):
            header_family(None, 5)
        with self.assertRaisesRegex(ValueError, 'must be an RFC 7230 token'):
            header_family({'bad name': 'value'})
        with self.assertRaisesRegex(TypeError, 'header names must be strings'):
            header_family({5: 'value'})


class IdentityConfigTest(unittest.TestCase):
    """Config-side validation for the identity settings; still off by default."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config = BridgeConfig(self.root / 'config.json')

    def test_identity_settings_default_off(self):
        view = self.config.view()
        self.assertEqual(view['identity_ide_headers'], {})
        self.assertEqual(view['identity_ide_user_session_id'], '')
        self.assertFalse(view['identity_ide_timestamp'])

    def test_identity_headers_round_trip_in_order(self):
        headers = {'x-client-type': 'kernel', 'plugin-version': '0.0.0-test'}
        view = self.config.update({'identity_ide_headers': headers})
        self.assertEqual(list(view['identity_ide_headers']), list(headers))
        self.assertEqual(view['identity_ide_headers'], headers)

    def test_identity_headers_reject_unsafe_or_malformed_values(self):
        cases = [
            {'authorization': 'value'},
            {'x-sdk-date': '20260101T000000Z'},
            {'x-auth-token': 'value'},
            {'bad name': 'value'},
            {'x-client-type': 'a\rb'},
            {'x-client-type': 'a\u2028b'},
            {'X-Client-Type': 'a', 'x-client-type': 'b'},
            {'x-client-type': 1},
            {'x-client-type': 'z' * 4097},
        ]
        for headers in cases:
            with self.subTest(headers=headers):
                with self.assertRaises(ValueError):
                    self.config.update({'identity_ide_headers': headers})
        for headers in (['x-client-type'], 'x-client-type=kernel', 5):
            with self.subTest(headers=repr(headers)):
                with self.assertRaises(ValueError):
                    self.config.update({'identity_ide_headers': headers})
        self.assertEqual(self.config.view()['identity_ide_headers'], {})

    def test_identity_session_id_is_single_line_and_bounded(self):
        view = self.config.update({'identity_ide_user_session_id':
                                   '  ' + FAKE_SESSION_ID + '  '})
        self.assertEqual(view['identity_ide_user_session_id'], FAKE_SESSION_ID)
        for value in (5, 'ses_a\nses_b', 'x' * 4097, ['ses_a']):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    self.config.update({'identity_ide_user_session_id': value})

    def test_identity_timestamp_must_be_boolean(self):
        for value in ('true', 1, None):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    self.config.update({'identity_ide_timestamp': value})
        self.assertFalse(self.config.view()['identity_ide_timestamp'])
        self.assertTrue(self.config.update(
            {'identity_ide_timestamp': True})['identity_ide_timestamp'])

    def test_identity_family_function_and_identity_values(self):
        view = self.config.view()
        self.assertFalse(view['identity_ide_ot_family'])
        self.assertEqual(view['identity_ide_function'], 'agent-claw')
        self.assertEqual(view['identity_ide_user_agent'], '')
        self.assertEqual(view['identity_ide_client_ip'], '')
        view = self.config.update({'identity_ide_ot_family': True,
                                   'identity_ide_function': '  agent-teams  ',
                                   'identity_ide_user_agent': '  Custom UA  ',
                                   'identity_ide_client_ip': ' 10.0.0.7 '})
        self.assertTrue(view['identity_ide_ot_family'])
        self.assertEqual(view['identity_ide_function'], 'agent-teams')
        self.assertEqual(view['identity_ide_user_agent'], 'Custom UA')
        self.assertEqual(view['identity_ide_client_ip'], '10.0.0.7')
        for payload in ({'identity_ide_ot_family': 'true'},
                        {'identity_ide_ot_family': 1},
                        {'identity_ide_function': 5},
                        {'identity_ide_function': 'a\nb'},
                        {'identity_ide_function': 'x' * 257},
                        {'identity_ide_user_agent': 5},
                        {'identity_ide_user_agent': 'a\rb'},
                        {'identity_ide_client_ip': 5},
                        {'identity_ide_client_ip': 'a\rb'},
                        {'identity_ide_client_ip': 'x' * 65}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.config.update(payload)

    def test_example_config_lists_every_setting_without_a_real_session_id(self):
        example = ROOT / 'extensions' / 'codearts' / 'config.example.json'
        raw = example.read_text(encoding='utf-8')
        data = json.loads(raw)
        self.assertLessEqual(set(data), set(DEFAULTS),
                             'config.example.json must only use known settings')
        identity_keys = {name for name in DEFAULTS if name.startswith('identity_ide_')}
        self.assertLessEqual(identity_keys, set(data))
        # The shipped example must never carry somebody's real IDE session id.
        self.assertIsNone(re.search(r'ses_[0-9A-Za-z]{20,}', raw))
        self.config.path.write_text(raw, encoding='utf-8')
        view = self.config.view()
        self.assertEqual(view['identity_ide_user_session_id'], 'ses_<26-char-session-id>')
        self.assertFalse(view['identity_ide_timestamp'])


class IdentityBackendTest(unittest.TestCase):
    """build_backend wiring: parameters and CODEARTS_IDE_* environment entries."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.calls = 0

    def run_build(self, env=None, **kwargs):
        """Run build_backend on a throwaway CodeArts-shaped tree, Go stubbed out."""
        self.calls += 1
        work = self.root / ('run-%d' % self.calls)
        source = work / 'source'
        (source / 'internal' / 'upstream').mkdir(parents=True)
        (source / 'go.mod').write_bytes(b'module codearts2api\n')
        (source / CLIENT).write_bytes(SOURCE_CLIENT.encode('utf-8'))
        default = work / 'default-overlay.json'
        default.write_bytes((json.dumps({'Replace': {'models.go': 'models.out'}})
                             + '\n').encode('utf-8'))
        self.default_overlay = default
        patched = mock.patch.dict(os.environ)
        patched.start()
        self.addCleanup(patched.stop)
        for name in ENV_ENTRY_POINTS:
            os.environ.pop(name, None)
        os.environ.update(env or {})
        self.build = work / 'bin' / 'build-overlay'
        self.output = work / 'bin' / 'server.exe'
        with mock.patch.object(backend_overlay, 'model_id_overlay', return_value=default), \
                mock.patch.object(backend.subprocess, 'run') as run:
            backend.build_backend(source, self.output, go='fake-go', **kwargs)
        self.spoofed = self.build / 'spoof' / 'client.go'
        return run

    def spoofed_text(self):
        return self.spoofed.read_bytes().decode('utf-8')

    def test_environment_headers_and_session_id_reach_the_build(self):
        run = self.run_build(env={
            'CODEARTS_IDE_USER_AGENT': TYPICAL_USER_AGENT,
            'CODEARTS_IDE_HEADERS': json.dumps(FAKE_HEADERS),
            'CODEARTS_IDE_SESSION_ID': FAKE_SESSION_ID,
        })
        merged_path = self.build / 'overlay-merged.json'
        run.assert_called_once_with(
            ['fake-go', 'build', '-overlay', str(merged_path),
             '-o', str(self.output.resolve()), './cmd/server'],
            cwd=str((self.root / 'run-1' / 'source').resolve()), check=True)
        merged = json.loads(merged_path.read_bytes())['Replace']
        client_key = str((self.root / 'run-1' / 'source' / CLIENT).resolve())
        self.assertEqual(merged[client_key], str(self.spoofed.resolve()))
        # Both sides keep their replacements: the default overlay is not lost.
        self.assertEqual(merged['models.go'], 'models.out')
        rows = dict(injected_headers(self.spoofed_text()))
        self.assertEqual(rows['User-Agent'], json.dumps(TYPICAL_USER_AGENT))
        self.assertEqual(rows['x-client-type'], json.dumps('kernel'))
        self.assertEqual(rows[spoof_overlay.SESSION_ID_HEADER],
                         json.dumps(FAKE_SESSION_ID))
        names = [name for name, _ in injected_headers(self.spoofed_text())]
        self.assertEqual(names[0], 'User-Agent')
        self.assertLess(names.index('x-client-type'),
                        names.index(spoof_overlay.SESSION_ID_HEADER))
        self.assertEqual(names[-1], spoof_overlay.SESSION_ID_HEADER)

    def test_explicit_headers_win_over_the_environment(self):
        self.run_build(env={'CODEARTS_IDE_HEADERS': json.dumps({'x-client-type': 'env'})},
                       spoof_headers={'x-client-type': 'explicit'})
        rows = dict(injected_headers(self.spoofed_text()))
        self.assertEqual(rows['x-client-type'], json.dumps('explicit'))

    def test_headers_only_configuration_still_writes_the_overlay(self):
        self.run_build(spoof_headers=FAKE_HEADERS)
        text = self.spoofed_text()
        self.assertEqual(injected_headers(text), fake_rows(*FAKE_HEADERS.items()))
        self.assertNotIn('User-Agent', text)
        self.assertTrue((self.build / 'overlay-merged.json').is_file())

    def test_explicit_session_id_wins_over_a_same_named_header(self):
        self.run_build(spoof_headers={spoof_overlay.SESSION_ID_HEADER: 'from-map'},
                       spoof_session_id=FAKE_SESSION_ID)
        rows = dict(injected_headers(self.spoofed_text()))
        self.assertEqual(rows[spoof_overlay.SESSION_ID_HEADER],
                         json.dumps(FAKE_SESSION_ID))

    def test_timestamp_parameter_adds_one_dynamic_line(self):
        self.run_build(spoof_user_agent=TYPICAL_USER_AGENT, spoof_timestamp=True)
        text = self.spoofed_text()
        self.assertEqual(text.count(TIMESTAMP_LINE), 1)
        self.assertEqual(injected_headers(text)[-1][0], 'timestamp')

    def test_nothing_configured_writes_no_spoof_artifacts(self):
        run = self.run_build()
        self.assertFalse((self.build / 'spoof').exists())
        self.assertFalse((self.build / 'overlay-merged.json').exists())
        run.assert_called_once_with(
            ['fake-go', 'build', '-overlay', str(self.default_overlay),
             '-o', str(self.output.resolve()), './cmd/server'],
            cwd=str((self.root / 'run-1' / 'source').resolve()), check=True)

    def test_invalid_headers_environment_is_rejected(self):
        for raw in ('not-json', '[]', '"x-client-type"', '5'):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ValueError,
                                            'CODEARTS_IDE_HEADERS must be a JSON object'):
                    self.run_build(env={'CODEARTS_IDE_HEADERS': raw})

    def test_reserved_header_from_the_environment_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'is reserved'):
            self.run_build(env={'CODEARTS_IDE_HEADERS': json.dumps(
                {'Authorization': 'Bearer nope'})})
        self.assertFalse((self.build / 'overlay-merged.json').exists())

    def test_headers_must_be_a_mapping_and_timestamp_a_boolean(self):
        with self.assertRaisesRegex(TypeError, 'spoof_headers must be a JSON object'):
            self.run_build(spoof_headers=['x-client-type=kernel'])
        with self.assertRaisesRegex(TypeError, 'spoof_session_id must be a string'):
            self.run_build(spoof_session_id=5)
        with self.assertRaisesRegex(TypeError, 'spoof_timestamp must be true or false'):
            self.run_build(spoof_timestamp='true')


class SpoofBuildOptionsTest(unittest.TestCase):
    """Configuration snapshot -> build_backend keyword arguments."""

    def test_blank_configuration_means_not_configured(self):
        for snapshot in ({}, dict(DEFAULTS),
                         {'identity_ide_headers': {},
                          'identity_ide_user_session_id': '',
                          'identity_ide_timestamp': False,
                          'identity_ide_ot_family': False}):
            with self.subTest(snapshot=sorted(snapshot)):
                self.assertEqual(backend.spoof_build_options(snapshot), {
                    'spoof_user_agent': None,
                    'spoof_headers': None,
                    'spoof_session_id': None,
                    'spoof_timestamp': False,
                    'spoof_ot_family': False,
                    'spoof_client_ip': None,
                })

    def test_full_configuration_moves_the_user_agent_out_of_the_headers(self):
        options = backend.spoof_build_options({
            'identity_ide_headers': {'User-Agent': '  IDE/26.10.2  ',
                                     'x-client-type': 'kernel'},
            'identity_ide_user_session_id': '  ' + FAKE_SESSION_ID + '  ',
            'identity_ide_timestamp': True,
        })
        self.assertEqual(options, {
            'spoof_user_agent': 'IDE/26.10.2',
            'spoof_headers': {'x-client-type': 'kernel'},
            'spoof_session_id': FAKE_SESSION_ID,
            'spoof_timestamp': True,
            'spoof_ot_family': False,
            'spoof_client_ip': None,
        })
        # Header names are case-insensitive, so any spelling is accepted.
        for name in ('user-agent', 'USER-AGENT', 'User-Agent'):
            with self.subTest(name=name):
                options = backend.spoof_build_options(
                    {'identity_ide_headers': {name: 'IDE/1.0', 'x-a': '1'}})
                self.assertEqual(options['spoof_user_agent'], 'IDE/1.0')
                self.assertEqual(options['spoof_headers'], {'x-a': '1'})

    def test_blank_user_agent_keeps_the_environment_entry_available(self):
        options = backend.spoof_build_options({'identity_ide_headers': {'User-Agent': '   '}})
        self.assertIsNone(options['spoof_user_agent'])
        self.assertIsNone(options['spoof_headers'])

    def test_header_order_is_preserved(self):
        options = backend.spoof_build_options(
            {'identity_ide_headers': {'zeta': 'z', 'Plugin-Version': '26.10.2', 'alpha': 'a'}})
        self.assertEqual(list(options['spoof_headers']), ['zeta', 'Plugin-Version', 'alpha'])

    def test_snapshot_is_not_mutated(self):
        snapshot = {'identity_ide_headers': {'User-Agent': 'IDE/1.0', 'x-client-type': 'kernel'},
                    'identity_ide_user_session_id': FAKE_SESSION_ID,
                    'identity_ide_timestamp': True}
        before = json.dumps(snapshot, sort_keys=True)
        backend.spoof_build_options(snapshot)
        self.assertEqual(json.dumps(snapshot, sort_keys=True), before)

    def test_reserved_and_invalid_headers_raise(self):
        cases = [
            ({'identity_ide_headers': {'maas_type': 'benefit'}}, ValueError, 'is reserved'),
            ({'identity_ide_headers': {'Session-Id': 'x'}}, ValueError, 'is reserved'),
            ({'identity_ide_headers': {'bad name': 'x'}}, ValueError, 'must be an RFC 7230 token'),
            ({'identity_ide_headers': {'x-a': 'a\rb'}}, ValueError,
             'must be a single header line'),
            ({'identity_ide_headers': {'User-Agent': 'a', 'user-agent': 'b'}}, ValueError,
             "duplicate header name 'User-Agent'"),
        ]
        for snapshot, error, message in cases:
            with self.subTest(snapshot=repr(snapshot)):
                with self.assertRaisesRegex(error, re.escape(message)):
                    backend.spoof_build_options(snapshot)

    def test_wrong_types_raise(self):
        cases = [
            (['x-client-type'], 'snapshot must be a configuration dict'),
            ({'identity_ide_headers': ['x-client-type']},
             'identity_ide_headers must be a JSON object'),
            ({'identity_ide_headers': {'User-Agent': 1}},
             'User-Agent in identity_ide_headers must be a string'),
            ({'identity_ide_headers': {'x-a': 1}}, 'must be a string'),
            ({'identity_ide_user_session_id': 5},
             'identity_ide_user_session_id must be a string'),
            ({'identity_ide_timestamp': 'true'},
             'identity_ide_timestamp must be true or false'),
        ]
        for snapshot, message in cases:
            with self.subTest(snapshot=repr(snapshot)):
                with self.assertRaisesRegex(TypeError, re.escape(message)):
                    backend.spoof_build_options(snapshot)

    def test_ot_family_flag_and_function_override(self):
        options = backend.spoof_build_options({
            'identity_ide_headers': {'x-client-type': 'kernel'},
            'identity_ide_ot_family': True,
            'identity_ide_function': 'agent-claw',
        })
        self.assertTrue(options['spoof_ot_family'])
        self.assertEqual(options['spoof_headers']['x-ot-function'], 'agent-claw')
        # The mode value only matters for the OT family: with it off nothing is
        # added, so a static-only build keeps its exact header set.
        options = backend.spoof_build_options({
            'identity_ide_headers': {'x-client-type': 'kernel'},
            'identity_ide_ot_family': False,
            'identity_ide_function': 'agent-claw',
        })
        self.assertFalse(options['spoof_ot_family'])
        self.assertEqual(options['spoof_headers'], {'x-client-type': 'kernel'})

    def test_blank_function_keeps_the_built_in_default(self):
        options = backend.spoof_build_options({'identity_ide_ot_family': True,
                                               'identity_ide_function': '   '})
        self.assertTrue(options['spoof_ot_family'])
        self.assertIsNone(options['spoof_headers'])

    def test_identity_user_agent_and_client_ip_settings(self):
        options = backend.spoof_build_options({
            'identity_ide_headers': {'User-Agent': 'from-headers'},
            'identity_ide_user_agent': '  from-setting  ',
            'identity_ide_client_ip': ' 192.168.1.20 ',
        })
        self.assertEqual(options['spoof_user_agent'], 'from-setting')
        self.assertEqual(options['spoof_client_ip'], '192.168.1.20')
        self.assertIsNone(options['spoof_headers'])
        # A blank setting keeps the header value, and then the env fallback.
        options = backend.spoof_build_options({
            'identity_ide_headers': {'User-Agent': 'from-headers'},
            'identity_ide_user_agent': '   ',
        })
        self.assertEqual(options['spoof_user_agent'], 'from-headers')
        self.assertIsNone(options['spoof_client_ip'])
        for snapshot, message in (
                ({'identity_ide_user_agent': 5}, 'identity_ide_user_agent must be a string'),
                ({'identity_ide_client_ip': 5}, 'identity_ide_client_ip must be a string')):
            with self.subTest(snapshot=repr(snapshot)):
                with self.assertRaisesRegex(TypeError, re.escape(message)):
                    backend.spoof_build_options(snapshot)

    def test_ot_family_and_function_types_are_validated(self):
        cases = [
            ({'identity_ide_ot_family': 'true'},
             'identity_ide_ot_family must be true or false'),
            ({'identity_ide_function': 5}, 'identity_ide_function must be a string'),
        ]
        for snapshot, message in cases:
            with self.subTest(snapshot=repr(snapshot)):
                with self.assertRaisesRegex(TypeError, re.escape(message)):
                    backend.spoof_build_options(snapshot)


class IdentityWiringTest(unittest.TestCase):
    """Bridge configuration -> build_backend -> generated client.go."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config = BridgeConfig(self.root / 'bridge.json')
        self.source = self.root / 'source'
        (self.source / 'internal' / 'upstream').mkdir(parents=True)
        (self.source / 'go.mod').write_bytes(b'module codearts2api\n')
        # The OT family is anchored on sendChatV2, so the wiring fixture carries
        # that shape even for cases that do not enable the family.
        (self.source / CLIENT).write_bytes(OT_SOURCE_CLIENT.encode('utf-8'))
        self.output = self.root / 'bin' / 'server.exe'
        self.default_overlay = self.root / 'default-overlay.json'
        self.default_overlay.write_bytes(
            (json.dumps({'Replace': {'models.go': 'models.out'}}) + '\n').encode('utf-8'))

    def build(self, env=None, **kwargs):
        """Run the real build_backend path with Go stubbed out."""
        patched = mock.patch.dict(os.environ)
        patched.start()
        self.addCleanup(patched.stop)
        for name in ENV_ENTRY_POINTS:
            os.environ.pop(name, None)
        os.environ.update(env or {})
        with mock.patch.object(backend_overlay, 'model_id_overlay',
                               return_value=self.default_overlay), \
                mock.patch.object(backend.subprocess, 'run'):
            backend.build_backend(self.source, self.output, go='fake-go', **kwargs)
        return self.output.parent / 'build-overlay'

    def test_configuration_snapshot_reaches_the_generated_client(self):
        self.config.update({
            'identity_ide_headers': {'User-Agent': TYPICAL_USER_AGENT,
                                     'x-client-type': 'kernel',
                                     'plugin-version': '0.0.0-test'},
            'identity_ide_user_session_id': FAKE_SESSION_ID,
            'identity_ide_timestamp': True,
        })
        build = self.build(**backend.spoof_build_options(self.config.snapshot()))
        text = (build / 'spoof' / 'client.go').read_bytes().decode('utf-8')
        rows = injected_headers(text)
        self.assertEqual(rows[0], ('User-Agent', json.dumps(TYPICAL_USER_AGENT)))
        self.assertEqual(dict(rows)['x-client-type'], json.dumps('kernel'))
        self.assertEqual(dict(rows)['plugin-version'], json.dumps('0.0.0-test'))
        self.assertEqual(dict(rows)[spoof_overlay.SESSION_ID_HEADER],
                         json.dumps(FAKE_SESSION_ID))
        # The User-Agent must be emitted once, never twice.
        self.assertEqual([name for name, _ in rows].count('User-Agent'), 1)
        self.assertEqual(text.count(TIMESTAMP_LINE), 1)
        self.assertTrue((build / 'overlay-merged.json').is_file())

    def test_configuration_without_identity_settings_keeps_the_default_build(self):
        self.config.update({'identity_ide_headers': {},
                            'identity_ide_user_session_id': '',
                            'identity_ide_timestamp': False})
        build = self.build(**backend.spoof_build_options(self.config.snapshot()))
        self.assertFalse((build / 'spoof').exists())
        self.assertFalse((build / 'overlay-merged.json').exists())

    def test_blank_user_agent_setting_still_allows_the_environment_entry(self):
        # Configuration without a User-Agent must not block the documented
        # environment fallback: spoof_build_options leaves the field unset.
        self.config.update({'identity_ide_headers': {'x-client-type': 'kernel'}})
        build = self.build(env={'CODEARTS_IDE_USER_AGENT': TYPICAL_USER_AGENT},
                           **backend.spoof_build_options(self.config.snapshot()))
        rows = injected_headers((build / 'spoof' / 'client.go').read_bytes().decode('utf-8'))
        self.assertEqual(rows[0], ('User-Agent', json.dumps(TYPICAL_USER_AGENT)))
        self.assertIn(('x-client-type', json.dumps('kernel')), rows)

    def test_launcher_passes_the_configuration_to_build_backend(self):
        # Text contract: the launcher deliberately has no CLI flag for identity
        # settings, so the bridge config snapshot is the only entry point.
        launcher = (ROOT / 'start-codearts-bridge.py').read_text(encoding='utf-8')
        self.assertIn('**spoof_build_options(runtime.config.snapshot())', launcher)
        self.assertIn('spoof_build_options', launcher.split('**spoof_build_options')[0])

    def test_ot_family_configuration_reaches_the_generated_client(self):
        self.config.update({
            'identity_ide_headers': {'x-client-type': 'kernel'},
            'identity_ide_user_session_id': FAKE_SESSION_ID,
            'identity_ide_ot_family': True,
            'identity_ide_function': 'agent-teams',
            'identity_ide_client_ip': '10.0.0.9',
        })
        build = self.build(**backend.spoof_build_options(self.config.snapshot()))
        text = (build / 'spoof' / 'client.go').read_bytes().decode('utf-8')
        self.assertEqual(text.count(OT_DYNAMIC_LOOP), 1)
        self.assertIn('const ideSpoofSessionID = ' + json.dumps(FAKE_SESSION_ID), text)
        self.assertIn('const ideSpoofClientIP = ' + json.dumps('10.0.0.9'), text)
        self.assertIn('const ideSpoofUA = '
                      + json.dumps(spoof_overlay.OFFICIAL_USER_AGENT), text)
        rows = dict(injected_literals(text))
        self.assertEqual(rows['x-client-type'], json.dumps('kernel'))
        self.assertEqual(rows['x-ot-function'], json.dumps('agent-teams'))
        self.assertNotIn('User-Agent', rows)

    def test_ot_family_off_keeps_the_official_table_out_of_the_build(self):
        self.config.update({'identity_ide_headers': {'x-client-type': 'kernel'},
                            'identity_ide_ot_family': False})
        build = self.build(**backend.spoof_build_options(self.config.snapshot()))
        text = (build / 'spoof' / 'client.go').read_bytes().decode('utf-8')
        self.assertNotIn(OT_DYNAMIC_LOOP, text)
        self.assertNotIn('x-ot-function', text)
        self.assertEqual(injected_headers(text), [('x-client-type', json.dumps('kernel'))])

# Text-only fixture: the OT family is anchored on sendChatV2, so these cases need
# its shape. It is never compiled -- real compilation is verified separately
# against the read-only codearts2api checkout, which is why the stub may mention
# identifiers a full package would define.
OT_CHAT_HEADERS_LOOP = ('\tfor k, v := range ChatHeadersV2(userToken, traceID, "zh-cn") {\n'
                        '\t\thttpReq.Header.Set(k, v)\n'
                        '\t}')
OT_DYNAMIC_LOOP = ('\tfor k, v := range ideSpoofDynamic(traceID, chatID, sessionID, body) {\n'
                   '\t\thttpReq.Header.Set(k, v)\n'
                   '\t}')
OT_SIGN_CALL = '\tsignRequest(httpReq, raw, cred)'
OT_DEL_ACCEPT = '\thttpReq.Header.Del("Accept")'
OT_DEL_CONTENT_TYPE = '\thttpReq.Header.Del("Content-Type")'
OT_POST_SIGN = ('\n\thttpReq.Header.Set("Content-Type", "application/json")\n'
                '\thttpReq.Header.Set("Accept", "*/*")\n'
                '\thttpReq.Header.Set("User-Agent", ideSpoofUA)')

OT_CLIENT_HEAD = (
    'package upstream\n'
    '\n'
    'import (\n'
    '\tcrand "crypto/rand"\n'
    '\t"crypto/sha256"\n'
    '\t"encoding/hex"\n'
    '\t"encoding/json"\n'
    '\t"fmt"\n'
    '\t"net/http"\n'
    '\t"strings"\n'
    '\t"sync/atomic"\n'
    '\t"time"\n'
    ')\n'
    '\n'
    'func chatHeaders(token, language string) map[string]string {\n'
)

SEND_CHAT_STUB = (
    '\n'
    'func sendChatV2(body map[string]any, traceID, userToken, chatID, sessionID string, benefit bool) {\n'
    '\thttpReq := &http.Request{Header: http.Header{}}\n'
    + OT_CHAT_HEADERS_LOOP + '\n'
    '\tif benefit {\n'
    '\t\thttpReq.Header.Set(HeaderMaasType, MaasBenefit)\n'
    '\t}\n'
    + OT_SIGN_CALL + '\n'
    '}\n'
)

OT_SOURCE_CLIENT = (
    OT_CLIENT_HEAD
    + SOURCE_TRACE
    + '\treturn map[string]string{\n'
    + HEADER_FIELDS
    + '\t}\n'
    '}\n'
    + SEND_CHAT_STUB
)

# The fixed identity half of the official chat request, verbatim from the client
# request log (task-5 evidence). x-ot-function is configurable, so it is not part
# of the table; DEFAULT_OT_FUNCTION fills it in.
OFFICIAL_FIXED_HEADERS = {
    'x-ot-parent-session-id': '',
    'x-ot-client-type': 'IDE',
    'x-ot-client-version': '26.10.2',
    'plugin-name': 'codemate_vscode',
    'plugin-version': '26.10.2',
    'ide-name': 'VSCode',
    'ide-version': '26.10.2',
}


class OtFamilyOverlayTest(unittest.TestCase):
    """Official per-request OT family, asserted as generated Go text."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / 'codearts2api'
        (self.source / 'internal' / 'upstream').mkdir(parents=True)
        self.write_client(OT_SOURCE_CLIENT)
        self.directory = self.root / 'build' / 'spoof'

    def write_client(self, text):
        (self.source / CLIENT).write_bytes(text.encode('utf-8'))

    def generate(self, **kwargs):
        overlay = chat_headers_overlay(self.source, self.directory, **kwargs)
        self.assertIsNotNone(overlay)
        raw = (self.directory / 'client.go').read_bytes()
        self.assertNotIn(b'\r', raw, 'generated Go file must stay LF-only')
        self.assertTrue(raw.endswith(b'\n'), 'generated Go file must end with a newline')
        return raw.decode('utf-8')

    def test_family_is_off_by_default(self):
        for kwargs in ({}, {'with_ot_family': False}, {'extra_headers': {},
                                                      'with_ot_family': False}):
            with self.subTest(kwargs=kwargs):
                self.assertIsNone(chat_headers_overlay(self.source, self.directory, **kwargs))
                self.assertFalse(self.directory.exists())

    def test_family_blocks_and_helpers_are_injected_once(self):
        text = self.generate(with_ot_family=True)
        self.assertEqual(text.count(OT_DEL_ACCEPT), 1)
        self.assertEqual(text.count(OT_DEL_CONTENT_TYPE), 1)
        self.assertEqual(text.count(OT_DYNAMIC_LOOP), 1)
        self.assertEqual(text.count(OT_POST_SIGN), 1)
        for helper in ('func ideSpoofDynamic(', 'func ideBase62(', 'func ideID(',
                       'func ideLocalIPv4('):
            self.assertEqual(text.count(helper), 1, helper)
        for constant in ('const ideSpoofUA =', 'const ideSpoofSessionID =',
                         'const ideSpoofClientIP ='):
            self.assertEqual(text.count(constant), 1, constant)
        # One fallback in ChatHeadersV2, one in ideSpoofDynamic.
        self.assertEqual(text.count(SPOOFED_TRACE), 2)

    def test_transport_headers_move_from_before_signing_to_after_it(self):
        text = self.generate(with_ot_family=True)
        loop = text.index(OT_CHAT_HEADERS_LOOP)
        delete_accept = text.index(OT_DEL_ACCEPT)
        delete_type = text.index(OT_DEL_CONTENT_TYPE)
        dynamic = text.index(OT_DYNAMIC_LOOP)
        sign = text.index(OT_SIGN_CALL)
        set_type = text.index('\thttpReq.Header.Set("Content-Type", "application/json")')
        set_accept = text.index('\thttpReq.Header.Set("Accept", "*/*")')
        set_ua = text.index('\thttpReq.Header.Set("User-Agent", ideSpoofUA)')
        self.assertLess(loop, delete_accept)
        self.assertLess(delete_accept, delete_type)
        self.assertLess(delete_type, dynamic)
        self.assertLess(dynamic, sign)
        self.assertLess(sign, set_type)
        self.assertLess(set_type, set_accept)
        self.assertLess(set_accept, set_ua)

    def test_dynamic_family_is_written_before_signing(self):
        text = self.generate(with_ot_family=True)
        body = text[text.index('func ideSpoofDynamic('):]
        self.assertLess(text.index(OT_DYNAMIC_LOOP), text.index(OT_SIGN_CALL))
        for name in ('x-ot-trace-id', 'x-ot-span-id', 'x-snap-traceid',
                     'user-msg-id', 'created-time', 'x-ot-session-id',
                     'user-session-id', 'client-ip', 'model-id', 'model-name'):
            self.assertIn('"%s"' % name, body)

    def test_only_the_net_import_is_added(self):
        text = self.generate(with_ot_family=True)
        expected_head = OT_CLIENT_HEAD.replace('\t"net/http"\n', '\t"net"\n\t"net/http"\n')
        self.assertEqual(text[:len(expected_head)], expected_head)
        for quoted in ('crand "crypto/rand"', '"crypto/sha256"', '"encoding/hex"',
                       '"fmt"', '"net"', '"net/http"', '"strings"',
                       '"sync/atomic"', '"time"'):
            self.assertEqual(text.count(quoted), 1, quoted)

    def test_official_values_match_the_evidence(self):
        self.assertEqual(spoof_overlay.OFFICIAL_STATIC_HEADERS, OFFICIAL_FIXED_HEADERS)
        self.assertEqual(validate_header_family(spoof_overlay.OFFICIAL_STATIC_HEADERS),
                         list(OFFICIAL_FIXED_HEADERS.items()))
        self.assertEqual(spoof_overlay.OFFICIAL_USER_AGENT,
                         'ai-sdk/provider-utils/4.0.21 runtime/bun/1.3.14')
        self.assertEqual(spoof_overlay.DEFAULT_OT_FUNCTION, 'agent-claw')

    def test_static_family_is_emitted_with_the_default_function(self):
        text = self.generate(with_ot_family=True)
        rows = dict(injected_literals(text))
        expected = {name: json.dumps(value) for name, value in
                    OFFICIAL_FIXED_HEADERS.items()}
        expected['x-ot-function'] = json.dumps('agent-claw')
        self.assertEqual(rows, expected)
        self.assertNotIn('User-Agent', rows)

    def test_configured_values_are_baked_as_constants(self):
        text = self.generate(with_ot_family=True, session_id='  ' + FAKE_SESSION_ID + '  ',
                             client_ip='10.1.2.3', user_agent='  Custom UA  ')
        self.assertIn('const ideSpoofUA = ' + json.dumps('Custom UA'), text)
        self.assertIn('const ideSpoofSessionID = ' + json.dumps(FAKE_SESSION_ID), text)
        self.assertIn('const ideSpoofClientIP = ' + json.dumps('10.1.2.3'), text)
        self.assertIn('\thttpReq.Header.Set("User-Agent", ideSpoofUA)', text)

    def test_default_user_agent_is_the_official_transport_value(self):
        text = self.generate(with_ot_family=True)
        self.assertEqual(text.count('const ideSpoofUA = '
                                    + json.dumps(spoof_overlay.OFFICIAL_USER_AGENT)), 1)

    def test_user_agent_is_never_part_of_the_signed_family(self):
        text = self.generate(user_agent=TYPICAL_USER_AGENT, with_ot_family=True)
        self.assertNotIn('\theaders["User-Agent"] = ', text)
        self.assertNotIn('User-Agent', dict(injected_literals(text)))
        self.assertIn('const ideSpoofUA = ' + json.dumps(TYPICAL_USER_AGENT), text)

    def test_dynamic_keys_are_dropped_from_the_static_family(self):
        text = self.generate(extra_headers={'x-snap-traceid': 'f' * 32,
                                            'user-session-id': 'ses_from_static',
                                            'model-id': 'static-model',
                                            'client-ip': '10.9.9.9',
                                            'x-client-type': 'kernel'},
                             with_ot_family=True)
        rows = dict(injected_literals(text))
        for name in ('x-snap-traceid', 'user-session-id', 'model-id', 'client-ip'):
            self.assertNotIn(name, rows)
        self.assertEqual(rows['x-client-type'], json.dumps('kernel'))
        self.assertNotIn('ses_from_static', text)

    def test_caller_values_win_over_official_defaults(self):
        text = self.generate(extra_headers={'plugin-version': '0.0.0-test',
                                            'x-ot-function': 'agent-general'},
                             with_ot_family=True)
        rows = dict(injected_literals(text))
        self.assertEqual(rows['plugin-version'], json.dumps('0.0.0-test'))
        self.assertEqual(rows['x-ot-function'], json.dumps('agent-general'))

    def test_timestamp_and_family_coexist(self):
        text = self.generate(with_timestamp=True, with_ot_family=True)
        self.assertEqual(text.count(TIMESTAMP_LINE), 1)
        self.assertEqual(text.count(OT_DYNAMIC_LOOP), 1)
        self.assertLess(text.index(TIMESTAMP_LINE.rstrip('\n')), text.index(OT_SIGN_CALL))

    def test_ot_family_requires_its_imports(self):
        for quoted in ('\tcrand "crypto/rand"\n', '\t"crypto/sha256"\n',
                       '\t"encoding/hex"\n', '\t"net/http"\n', '\t"strings"\n',
                       '\t"sync/atomic"\n'):
            with self.subTest(import_=quoted.strip()):
                self.write_client(OT_SOURCE_CLIENT.replace(quoted, '', 1))
                with self.assertRaisesRegex(ValueError, re.escape('(OT imports)')):
                    chat_headers_overlay(self.source, self.directory, with_ot_family=True)
                self.assertFalse(self.directory.exists())
                self.write_client(OT_SOURCE_CLIENT)

    def test_ot_family_requires_the_send_chat_anchor(self):
        self.write_client(OT_SOURCE_CLIENT.replace(OT_CHAT_HEADERS_LOOP, ''))
        with self.assertRaisesRegex(ValueError, re.escape('(sendChatV2 headers)')):
            chat_headers_overlay(self.source, self.directory, with_ot_family=True)
        self.assertFalse(self.directory.exists())

    def test_ot_family_requires_a_unique_sign_call(self):
        self.write_client(OT_SOURCE_CLIENT + '\nfunc other() {\n' + OT_SIGN_CALL + '\n}\n')
        with self.assertRaisesRegex(ValueError, re.escape('(signRequest call)')):
            chat_headers_overlay(self.source, self.directory, with_ot_family=True)
        self.assertFalse(self.directory.exists())

    def test_already_injected_family_is_rejected(self):
        self.write_client(OT_SOURCE_CLIENT + '\nfunc ideSpoofDynamic(traceID, chatID, '
                                             'sessionID string, body map[string]any) '
                                             'map[string]string {\n\treturn nil\n}\n')
        with self.assertRaisesRegex(ValueError, re.escape('(ideSpoofDynamic)')):
            chat_headers_overlay(self.source, self.directory, with_ot_family=True)
        self.assertFalse(self.directory.exists())

    def test_flag_and_configured_values_are_validated(self):
        for value in (1, 'true', None):
            with self.subTest(value=repr(value)):
                with self.assertRaisesRegex(TypeError, 'with_ot_family must be true or false'):
                    chat_headers_overlay(self.source, self.directory, with_ot_family=value)
        with self.assertRaisesRegex(TypeError, 'session_id must be a string'):
            chat_headers_overlay(self.source, self.directory, with_ot_family=True,
                                 session_id=5)
        with self.assertRaisesRegex(ValueError, 'session_id must be a single header line'):
            chat_headers_overlay(self.source, self.directory, with_ot_family=True,
                                 session_id='ses_a\rb')
        with self.assertRaisesRegex(TypeError, 'client_ip must be a string'):
            chat_headers_overlay(self.source, self.directory, with_ot_family=True,
                                 client_ip=5)
        with self.assertRaisesRegex(ValueError, 'client_ip must be a single header line'):
            chat_headers_overlay(self.source, self.directory, with_ot_family=True,
                                 client_ip='10.0.0.1\r\nX-Evil: 1')
        self.assertFalse(self.directory.exists())


if __name__ == '__main__':
    unittest.main()
