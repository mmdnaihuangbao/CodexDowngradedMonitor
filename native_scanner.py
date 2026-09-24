"""Adapter for the persistent, read-only C++ scanner. HTTP never calls this adapter."""
import json
import os
import queue
import subprocess
import threading

EXECUTABLE = os.path.join(os.path.dirname(__file__), 'cpp_collector', 'build', 'collector_native.exe')

class NativeScanner:
    def __init__(self, pid, workers=4):
        self.pid = pid
        self.workers = workers
        self.proc = None
        self.messages = queue.Queue()
        self.io_lock = threading.Lock()
        self.last_bytes = self.last_regions = self.last_workers = 0
        self.region_cost = 0.0
        self.metrics = {}
        self.reader = None
        self.job = None

    def _read(self):
        try:
            for line in self.proc.stdout:
                self.messages.put(line)
        finally:
            self.messages.put(None)

    def _receive(self):
        try:
            line = self.messages.get(timeout=30)
        except queue.Empty as exc:
            raise RuntimeError('C++ collector timed out (30s)') from exc
        if line is None:
            raise RuntimeError('C++ collector exited')
        return json.loads(line)

    def open(self):
        if not os.path.isfile(EXECUTABLE):
            raise FileNotFoundError('C++ collector missing; run cpp_collector/build.ps1 first')
        from process_job import ProcessJob
        self.job = ProcessJob()
        try:
            self.proc = subprocess.Popen(
                [EXECUTABLE, '--pid', str(self.pid), '--workers', str(self.workers)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', errors='replace', bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW)
            # Assign before sending any scan request; fail closed if containment fails.
            self.job.assign(self.proc)
            self.reader = threading.Thread(target=self._read, daemon=True, name='native-output')
            self.reader.start()
            ready = self._receive().get('ready', False)
            if not ready: self.close()
            return ready
        except Exception:
            self.close()
            raise

    def _ask(self, command):
        with self.io_lock:
            if self.proc is None or self.proc.poll() is not None:
                raise RuntimeError('C++ collector is not running')
            self.proc.stdin.write(command + '\n')
            self.proc.stdin.flush()
            return self._receive()

    def is_alive(self):
        try:
            return self._ask('alive').get('alive', False)
        except (OSError, RuntimeError, ValueError):
            return False

    def sweep_records(self):
        data = self._ask('scan')
        if not data.get('alive'):
            raise RuntimeError('Target process exited')
        self.last_bytes = data['bytes']
        self.last_regions = data['regions']
        self.last_workers = data['workers']
        self.region_cost = data['region_cost']
        self.metrics = {k: data.get(k, 0) for k in
                        ('scan_cost', 'read_errors', 'read_worker_seconds', 'parse_worker_seconds',
                         'prefilter_worker_seconds', 'merge_cost', 'serialize_cost',
                         'read_worker_max_seconds', 'prefilter_worker_max_seconds',
                         'parse_worker_max_seconds', 'worker_max_seconds', 'owned_bytes', 'tasks',
                         'parse_candidates', 'incomplete_candidates', 'supplemental_reads')}
        for rec in data['responses']:
            rec['_marks'] = int(rec.get('_marks', 0))
        return data

    def close(self):
        if self.job is not None:
            self.job.close()
            self.job = None
        proc = self.proc
        if proc is None: return
        # Stop only our own helper; never terminate or write to the observed process.
        if proc.poll() is None:
            proc.terminate()
        try: proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait(timeout=3)
        if self.reader is not None: self.reader.join(timeout=1)
        for stream in (proc.stdin, proc.stdout):
            if stream:
                try: stream.close()
                except OSError: pass
        self.proc = None
