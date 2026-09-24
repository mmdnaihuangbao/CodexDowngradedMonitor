"""Real Windows process tests, isolated from the user's running monitor."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from instance_guard import InstanceGuard, _api, k32, close_handle, W

def wait_until(check, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError('timed out')

class SingletonTests(unittest.TestCase):
    def test_mutex_released_after_owner_killed(self):
        name = 'Global\\MonitorTest-' + uuid.uuid4().hex
        code = 'from instance_guard import InstanceGuard; import sys; g=InstanceGuard(sys.argv[1]); print(g.acquire(),flush=True); sys.stdin.read()'
        child = subprocess.Popen([sys.executable, '-c', code, name], cwd=ROOT,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        guard = InstanceGuard(name)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'True')
            self.assertFalse(guard.acquire())
            child.kill(); child.wait(timeout=5)
            self.assertTrue(guard.acquire())
        finally:
            guard.close()
            if child.poll() is None:
                child.kill(); child.wait(timeout=5)
            child.stdin.close(); child.stdout.close()

    def test_ten_collectors_different_ports_and_launcher_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, LOCALAPPDATA=directory)
            name = 'Global\\MonitorTest-' + uuid.uuid4().hex
            # Explicit Python-only dependency injection: production CLI has no bypass.
            code = '''import sys
import instance_guard as G
Original = G.InstanceGuard
name = sys.argv.pop(1)
G.InstanceGuard = lambda: Original(name)
import collector as C
C.enumerate_codex = lambda: []
C.Monitor.start_evidence = lambda self: None
sys.exit(C.main())
'''
            import urllib.request
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            children = []
            try:
                for i in range(10):
                    with socket.socket() as sock:
                        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
                    children.append(subprocess.Popen(
                        [sys.executable, '-c', code, name, '--port', str(port), '--host', '127.0.0.1',
                         '--db', str(Path(directory)/f'{i}.sqlite'), '--log', str(Path(directory)/f'{i}.log'), '--no-open'],
                        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
                wait_until(lambda: sum(c.poll() == 5 for c in children) == 9)
                self.assertEqual(sum(c.poll() is None for c in children), 1)
                self.assertEqual(len(list(Path(directory).glob('*.sqlite'))), 1)
                state_file = Path(directory)/'CodexDowngradedMonitor/instance.json'
                wait_until(state_file.exists)
                state = json.loads(state_file.read_text())
                url = f"http://127.0.0.1:{state['port']}"
                with opener.open(url + '/api/health', timeout=5) as response:
                    health = json.load(response)
                self.assertEqual(health['instance_id'], state['instance_id'])
                for mode in ('--status', '--no-open'):
                    result = subprocess.run([sys.executable, 'start.py', '--port', '1', mode],
                                            cwd=ROOT, env=env, capture_output=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                # Metadata must not redirect control to an unrelated or recycled instance.
                state['instance_id'] = 'stale'
                state_file = Path(directory)/'CodexDowngradedMonitor/instance.json'
                state_file.write_text(json.dumps(state))
                check = subprocess.run([sys.executable, '-c', 'import start; assert start.find_running(1) is None'],
                                       cwd=ROOT, env=env, capture_output=True, timeout=15)
                self.assertEqual(check.returncode, 0, check.stderr)
                state['instance_id'] = health['instance_id']; state_file.write_text(json.dumps(state))
                result = subprocess.run([sys.executable, 'start.py', '--port', '1', '--stop'],
                                        cwd=ROOT, env=env, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                for child in children:
                    child.wait(timeout=5)
                self.assertFalse(state_file.exists())
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill(); child.wait(timeout=5)
                    child.stderr.close()

    def test_native_helper_dies_with_parent(self):
        code = '''import os, sys
from native_scanner import NativeScanner
s = NativeScanner(os.getpid(), 1)
assert s.open()
print(s.proc.pid, flush=True)
sys.stdin.read()
s.close()
'''
        parent = subprocess.Popen([sys.executable, '-c', code], cwd=ROOT,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        handle = None
        try:
            pid = int(parent.stdout.readline())
            open_process = _api(k32, 'OpenProcess', [W.DWORD, W.BOOL, W.DWORD], W.HANDLE)
            wait = _api(k32, 'WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD)
            handle = open_process(0x100000, False, pid)
            self.assertTrue(handle)
            parent.kill(); parent.wait(timeout=5)
            self.assertEqual(wait(handle, 5000), 0)
        finally:
            if handle: close_handle(handle)
            if parent.poll() is None:
                parent.kill(); parent.wait(timeout=5)
            parent.stdin.close(); parent.stdout.close()

if __name__ == '__main__':
    unittest.main(verbosity=2)
