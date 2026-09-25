"""Windows per-user singleton, independent of checkout, database and HTTP port."""
import ctypes
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import uuid

APP_ID = 'CodexDowngradedMonitor'
ALREADY_RUNNING = 5
k32 = ctypes.WinDLL('kernel32', use_last_error=True)
a32 = ctypes.WinDLL('advapi32', use_last_error=True)

def _api(dll, name, args, result):
    fn = getattr(dll, name)
    fn.argtypes, fn.restype = args, result
    return fn

close_handle = _api(k32, 'CloseHandle', [W.HANDLE], W.BOOL)
_create_mutex = _api(k32, 'CreateMutexW', [W.LPVOID, W.BOOL, W.LPCWSTR], W.HANDLE)
_wait = _api(k32, 'WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD)
_release = _api(k32, 'ReleaseMutex', [W.HANDLE], W.BOOL)

def user_sid():
    current = _api(k32, 'GetCurrentProcess', [], W.HANDLE)
    open_token = _api(a32, 'OpenProcessToken', [W.HANDLE, W.DWORD, ctypes.POINTER(W.HANDLE)], W.BOOL)
    get_info = _api(a32, 'GetTokenInformation', [W.HANDLE, ctypes.c_int, W.LPVOID, W.DWORD, ctypes.POINTER(W.DWORD)], W.BOOL)
    to_string = _api(a32, 'ConvertSidToStringSidW', [W.LPVOID, ctypes.POINTER(W.LPWSTR)], W.BOOL)
    free = _api(k32, 'LocalFree', [W.LPVOID], W.LPVOID)
    token, size = W.HANDLE(), W.DWORD()
    if not open_token(current(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        get_info(token, 1, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if not get_info(token, 1, buf, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buf, ctypes.POINTER(W.LPVOID))[0]
        value = W.LPWSTR()
        if not to_string(sid, ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return value.value
        finally:
            free(ctypes.cast(value, W.LPVOID))
    finally:
        close_handle(token)

def state_path():
    return Path(os.environ['LOCALAPPDATA']) / APP_ID / 'instance.json'

def read_state():
    try:
        state = json.loads(state_path().read_text(encoding='utf-8'))
        if (state.get('app_id') == APP_ID and isinstance(state.get('host'), str)
                and type(state.get('port')) is int and 1 <= state['port'] <= 65535
                and type(state.get('pid')) is int and isinstance(state.get('instance_id'), str)):
            return state
    except (OSError, ValueError, AttributeError):
        pass
    return None

def instance_running():
    """Check the per-user mutex without starting a collector or changing metadata."""
    guard = InstanceGuard()
    try:
        return not guard.acquire()
    finally:
        guard.close()

class InstanceGuard:
    def __init__(self, name=None):
        self.name = name or ('Global\\' + APP_ID + '-' + user_sid())
        self.handle = None
        self.instance_id = uuid.uuid4().hex
        self.published = False

    def acquire(self):
        self.handle = _create_mutex(None, False, self.name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        result = _wait(self.handle, 0)
        if result in (0, 0x80):  # acquired or abandoned by a crashed owner
            return True
        error = ctypes.get_last_error()
        close_handle(self.handle)
        self.handle = None
        if result == 0x102:
            return False
        raise ctypes.WinError(error)

    def publish(self, host, port):
        state = dict(app_id=APP_ID, instance_id=self.instance_id,
                     pid=os.getpid(), host=host, port=port)
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.' + self.instance_id + '.tmp')
        try:
            temporary.write_text(json.dumps(state), encoding='utf-8')
            os.replace(temporary, path)
            self.published = True
        finally:
            temporary.unlink(missing_ok=True)

    def close(self):
        if self.handle:
            try:
                if self.published:
                    state = read_state()
                    if state and state['instance_id'] == self.instance_id:
                        try:
                            state_path().unlink(missing_ok=True)
                        except OSError:
                            pass  # stale metadata is rejected by health identity checks
            finally:
                _release(self.handle)
                close_handle(self.handle)
                self.handle = None
