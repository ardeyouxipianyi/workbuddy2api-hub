"""Process ownership and service readiness using a real local child process."""
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
import os
import subprocess
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from extensions.codearts.config import BridgeConfig
from extensions.codearts.backend import ManagedBackend

FIXTURE = '''import json,os,sys
from http.server import BaseHTTPRequestHandler,HTTPServer
c=json.load(open(sys.argv[sys.argv.index('-config')+1],encoding='utf-8'))
class H(BaseHTTPRequestHandler):
 def log_message(self,*a): pass
 def do_GET(self):
  if self.path!='/healthz' and self.headers.get('Authorization')!='Bearer '+os.environ['CA2A_API_KEY']:
   self.send_response(401);self.end_headers();return
  raw=b'ok' if self.path=='/healthz' else b'{"service":"codearts2api","stats":{"total":0},"accounts":[]}'
  self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
HTTPServer(('127.0.0.1',int(c['listen'].rsplit(':',1)[1])),H).serve_forever()
'''


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        fixture = self.root / 'backend_fixture.py'
        fixture.write_text(FIXTURE, encoding='utf-8')
        self.command = [sys.executable, str(fixture)]
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.config = BridgeConfig(self.root / 'bridge.json')

    def manager(self):
        manager = ManagedBackend(self.command, self.root / 'backend', port=self.port, timeout=5)
        self.addCleanup(manager.stop)
        return manager

    def test_start_prepares_private_backend_and_enables_bridge_after_ready(self):
        manager = self.manager()
        manager.start(self.config)
        cfg = self.config.ready()
        self.assertEqual(cfg['base_url'], 'http://127.0.0.1:%s' % self.port)
        self.assertGreaterEqual(len(cfg['api_key']), 24)
        stored = json.loads((self.root / 'backend' / 'config.json').read_text())
        self.assertEqual(stored['listen'], '127.0.0.1:%s' % self.port)
        self.assertTrue(Path(stored['auth_dir']).is_dir())
        self.assertIsNone(manager.process.poll())
        manager.stop()
        self.assertIsNotNone(manager.process.poll())

    def test_reuse_verified_service_does_not_take_ownership(self):
        first = self.manager()
        first.start(self.config)
        second = self.manager()
        second.start(self.config)
        self.assertIsNone(second.process)
        second.stop()
        self.assertIsNone(first.process.poll())

    def test_wrong_secret_does_not_adopt_or_stop_another_service(self):
        first = self.manager()
        first.start(self.config)
        self.config.update({'api_key': 'wrong-secret-value'})
        second = self.manager()
        with self.assertRaisesRegex(RuntimeError, 'occupied|existing'):
            second.start(self.config)
        self.assertIsNone(first.process.poll())
        self.assertIsNone(second.process)

    def test_failed_child_does_not_enable_configuration(self):
        manager = ManagedBackend([sys.executable, '-c', 'raise SystemExit(2)'], self.root / 'bad', port=self.port, timeout=2)
        self.addCleanup(manager.stop)
        with self.assertRaises(RuntimeError):
            manager.start(self.config)
        self.assertFalse(self.config.view()['enabled'])

    def test_native_hard_exit_hook_stops_owned_child_before_exit(self):
        manager = self.manager()
        manager.start(self.config)
        exits = []
        hub = SimpleNamespace(os=SimpleNamespace(_exit=exits.append))
        manager.attach_exit(hub)
        hub.os._exit(0)
        self.assertEqual(exits, [0])
        self.assertIsNotNone(manager.process.poll())

    @unittest.skipUnless(os.name == 'nt', 'Windows job object lifecycle')
    def test_abrupt_parent_exit_stops_owned_backend(self):
        script = self.root / 'parent.py'
        marker = self.root / 'ready'
        script.write_text('''import os,sys,time
sys.path.insert(0,sys.argv[1])
from extensions.codearts.backend import ManagedBackend
from extensions.codearts.config import BridgeConfig
m=ManagedBackend([sys.executable,sys.argv[2]],sys.argv[3],port=int(sys.argv[4]))
m.start(BridgeConfig(sys.argv[5]))
open(sys.argv[6],'w').write(str(m.process.pid))
time.sleep(.5)
os._exit(0)
''', encoding='utf-8')
        parent = subprocess.Popen([sys.executable, str(script), str(ROOT), self.command[1],
            str(self.root / 'owned'), str(self.port), str(self.root / 'owned-bridge.json'), str(marker)])
        self.addCleanup(lambda: parent.poll() is None and parent.kill())
        deadline = time.monotonic() + 10
        while not marker.exists() and parent.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(marker.exists(), 'child never became ready')
        child_pid = int(marker.read_text())
        import ctypes as c
        from ctypes import wintypes as w
        kernel = c.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        kernel.OpenProcess.restype = w.HANDLE
        kernel.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        child_handle = kernel.OpenProcess(1, False, child_pid)
        if child_handle:
            def cleanup():
                kernel.TerminateProcess(child_handle, 1)
                kernel.CloseHandle(child_handle)
            self.addCleanup(cleanup)
        parent.wait(timeout=10)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with socket.socket() as probe:
                if probe.connect_ex(('127.0.0.1', self.port)) != 0:
                    return
            time.sleep(.05)
        # Ensure a failed assertion does not strand this test's child.
        self.fail('owned backend survived abrupt parent exit')



if __name__ == '__main__':
    unittest.main()
