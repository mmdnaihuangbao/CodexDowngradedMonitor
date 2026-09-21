"""Windows process discovery. No PowerShell, WMI child, injection or privilege changes."""
import ctypes
from ctypes import wintypes as W
import os

class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [('dwSize', W.DWORD), ('cntUsage', W.DWORD), ('th32ProcessID', W.DWORD),
                ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', W.DWORD),
                ('cntThreads', W.DWORD), ('th32ParentProcessID', W.DWORD),
                ('pcPriClassBase', W.LONG), ('dwFlags', W.DWORD), ('szExeFile', W.WCHAR * 260)]

class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [('cb', W.DWORD), ('PageFaultCount', W.DWORD)] + [
        (name, ctypes.c_size_t) for name in ['PeakWorkingSetSize', 'WorkingSetSize',
        'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
        'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage']]

def enumerate_codex():
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    ps = ctypes.WinDLL('psapi', use_last_error=True)
    k.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
    k.CreateToolhelp32Snapshot.restype = W.HANDLE
    k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [W.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k.Process32FirstW.restype = k.Process32NextW.restype = W.BOOL
    k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]; k.OpenProcess.restype = W.HANDLE
    k.CloseHandle.argtypes = [W.HANDLE]; k.CloseHandle.restype = W.BOOL
    k.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)]
    k.QueryFullProcessImageNameW.restype = W.BOOL
    ps.GetProcessMemoryInfo.argtypes = [W.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), W.DWORD]
    ps.GetProcessMemoryInfo.restype = W.BOOL
    snap = k.CreateToolhelp32Snapshot(2, 0)
    if snap == ctypes.c_void_p(-1).value:
        return []
    result = []
    try:
        item = PROCESSENTRY32W(); item.dwSize = ctypes.sizeof(item)
        ok = k.Process32FirstW(snap, ctypes.byref(item))
        while ok:
            if item.szExeFile.lower() == 'codex.exe':
                path = ''; mb = 0
                h = k.OpenProcess(0x1000, False, item.th32ProcessID)
                if h:
                    try:
                        text = ctypes.create_unicode_buffer(32768); size = W.DWORD(len(text))
                        if k.QueryFullProcessImageNameW(h, 0, text, ctypes.byref(size)): path = text.value
                        mem = PROCESS_MEMORY_COUNTERS(); mem.cb = ctypes.sizeof(mem)
                        if ps.GetProcessMemoryInfo(h, ctypes.byref(mem), mem.cb): mb = round(mem.WorkingSetSize / 1048576, 1)
                    finally: k.CloseHandle(h)
                # Desktop Electron is Codex.exe; the engine in resources is codex.exe.
                # Preserve all candidates for diagnostics; picker prioritizes engine paths.
                result.append({'pid': item.th32ProcessID, 'parent_pid': item.th32ParentProcessID,
                               'path': path, 'name': item.szExeFile, 'cmd': '', 'mb': mb})
            ok = k.Process32NextW(snap, ctypes.byref(item))
    finally: k.CloseHandle(snap)
    return result
