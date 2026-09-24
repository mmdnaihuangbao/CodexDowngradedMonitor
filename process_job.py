"""Own only the native helper: Windows kills it if its Python owner dies."""
import ctypes
from ctypes import wintypes as W
from instance_guard import k32, _api, close_handle

class _Basic(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                ('flags', W.DWORD), ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t),
                ('active', W.DWORD), ('affinity', ctypes.c_size_t),
                ('priority', W.DWORD), ('scheduling', W.DWORD)]

class _IO(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]

class _Extended(ctypes.Structure):
    _fields_ = [('basic', _Basic), ('io', _IO), ('process_memory', ctypes.c_size_t),
                ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t),
                ('peak_job', ctypes.c_size_t)]

class ProcessJob:
    def __init__(self):
        create = _api(k32, 'CreateJobObjectW', [W.LPVOID, W.LPCWSTR], W.HANDLE)
        configure = _api(k32, 'SetInformationJobObject', [W.HANDLE, ctypes.c_int, W.LPVOID, W.DWORD], W.BOOL)
        self.handle = create(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not configure(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign(self, process):
        assign = _api(k32, 'AssignProcessToJobObject', [W.HANDLE, W.HANDLE], W.BOOL)
        if not assign(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            close_handle(self.handle)
            self.handle = None
