import json, pathlib, subprocess, sys, tempfile, unittest, time, socket, urllib.request
ROOT=pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import collector as C
from native_scanner import NativeScanner, EXECUTABLE

class ParserTests(unittest.TestCase):
    def parse(self, payload):
        with tempfile.TemporaryDirectory() as d:
            file=pathlib.Path(d)/'fixture.bin';file.write_bytes(payload)
            return json.loads(subprocess.check_output([EXECUTABLE,'--fixture',str(file)]))

    def test_boundaries_and_nested_fields(self):
        request={'type':'response.create','input':[{'model':'nested-wrong','content':'x'*16000}],
                 'model':'actual-request','previous_response_id':'resp_prev'}
        response={'id':'resp_real','object':'response','output':[{'model':'nested-wrong'}],
                  'model':'actual-response','completed_at':100,'previous_response_id':'resp_prev'}
        data=json.dumps(request).encode()+b'\x00'+json.dumps(response).encode()
        out=self.parse(data)
        self.assertEqual(out['requests'][0]['model'],'actual-request')
        self.assertEqual(out['requests'][0]['prev'],'resp_prev')
        self.assertEqual(out['responses'][0]['model'],'actual-response')
        # Adjacent objects must not donate server fields or models to one another.
        out=self.parse(b'{"id":"resp_bad","model":"wrong"}\x00{"object":"response","completed_at":100}')
        self.assertEqual(out['responses'],[])
        out=self.parse(b'{"id":"resp_missing","object":"response","completed_at":100,"output":[{"model":"nested"}]}')
        self.assertEqual(out['responses'],[])

    def test_first_request_and_ambiguity_preserved(self):
        data=b'{"type":"response.create","model":"a"}\0'+b'{"type":"response.create","model":"a","previous_response_id":"resp_p"}\0'+b'{"type":"response.create","model":"b","previous_response_id":"resp_p"}'
        out=self.parse(data)
        self.assertEqual(len(out['requests']),3)
        self.assertNotIn('prev',out['requests'][0])
        self.assertEqual({r['model'] for r in out['requests'][1:]},{'a','b'})

class ReadOnlyScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.child=subprocess.Popen([sys.executable,str(ROOT/'cpp_collector/tests/fixture_process.py')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,creationflags=subprocess.CREATE_NO_WINDOW)
        assert cls.child.stdout.readline().strip()=='ready'
    @classmethod
    def tearDownClass(cls):
        cls.child.stdin.close();cls.child.wait(timeout=5);cls.child.stdout.close()

    def test_native_memory_scan_and_cleanup(self):
        sc=NativeScanner(self.child.pid,4)
        try:
            self.assertTrue(sc.open());self.assertTrue(sc.is_alive())
            helper=sc.proc.pid
            for _ in range(3):
                batch=sc.sweep_records();self.assertEqual(sc.proc.pid,helper)
            rows={r['response_id']:r for r in batch['responses']}
            self.assertIn('resp_boundary',rows)
            self.assertTrue(any(r.get('prev')=='resp_p1' for r in batch['requests']))
            self.assertEqual(rows['resp_down']['model'],'luna-mini')
            self.assertGreater(batch['bytes'],64*1024*1024)
            self.assertEqual(batch['workers'],4)
        finally:
            proc=sc.proc;sc.close()
            self.assertIsNotNone(proc.poll())

    def test_http_sse_and_launchers(self):
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        # 这个用例会 POST /api/config，而配置路径固定在仓库里：先备份，跑完还原，别改掉本机设置。
        config_path=ROOT/'config.json'
        saved_config=config_path.read_bytes() if config_path.exists() else None
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        code="import collector as C; C.enumerate_codex=lambda:[{'pid':%d,'cmd':'app-server','mb':64}]; C.main()" % self.child.pid
        # 显式给 --expect：fixture 的 resp_sub 要"请求模型 == 响应模型 != 预期模型"才判 subtask，
        # 不能让用例结果取决于本机 config.json 里的 expect。
        server=subprocess.Popen([sys.executable,'-c',code,'--db',':memory:','--port',str(port),'--no-open','--idle','0.03','--expect','gpt-6-astra','--log',str(ROOT/'_verify'/'native-integration.log')],cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
        def get(path,body=None):
            data=json.dumps(body).encode() if body is not None else None
            request=urllib.request.Request(f'http://127.0.0.1:{port}'+path,data=data,headers={'Content-Type':'application/json'})
            with opener.open(request,timeout=5) as r:return json.load(r)
        try:
            for _ in range(100):
                try:
                    snap=get('/api/snapshot')
                    if snap['stats']['rounds']>=2:break
                except OSError:pass
                time.sleep(.05)
            else:self.fail('server failed to start')
            self.assertEqual(get('/api/health')['backend'],'cpp')
            self.assertEqual(snap['stats']['backend'],'cpp')
            self.assertEqual(snap['stats']['status'],'running')
            rows={r['rid']:r for r in snap['responses']}
            self.assertEqual(rows['resp_down']['verdict'],'downgrade')
            self.assertEqual(rows['resp_sub']['verdict'],'subtask')
            self.assertEqual(rows['resp_unknown']['verdict'],'incomplete')
            self.assertEqual(rows['resp_normal']['req_model'],'gpt-6-astra')
            with opener.open(f'http://127.0.0.1:{port}/api/stream',timeout=5) as stream:
                frame=stream.readline().decode()
                self.assertTrue(frame.startswith('data: '))
                self.assertEqual(json.loads(frame[6:])['type'],'snapshot')
            self.assertTrue(get('/api/config',{'idle':0.05})['ok'])
            self.assertTrue(get('/api/diagnose')['ok'])
            result=subprocess.run([sys.executable,str(ROOT/'start.py'),'--db',':memory:','--port',str(port),'--status'],capture_output=True)
            self.assertEqual(result.returncode,0)
            self.assertTrue(get('/api/shutdown',{})['ok']);server.wait(timeout=8)
            self.assertEqual(server.returncode,0)
        finally:
            if server.poll() is None:server.terminate();server.wait(timeout=5)
            if saved_config is not None:config_path.write_bytes(saved_config)

    def test_late_pairing_and_conflict(self):
        class FakeScanner:
            # workers 是 _ensure_process 比对线程配置时读的字段：假扫描器也要有。
            workers=4;last_regions=1;last_workers=1;region_cost=0;metrics={}
            def is_alive(self):return True
            def close(self):pass
            def sweep_records(self):return self.batch
        sc=FakeScanner();m=C.Monitor('expected',0);m.scanner=sc;m.pid=123
        response={'response_id':'resp_late','model':'different','prev':'resp_parent','status':'completed'}
        def tick(requests,responses):
            sc.batch={'requests':requests,'responses':responses,'hit_blocks':1,'bytes':100}
            m.tick()
        tick([], [response]);self.assertEqual(m.seen['resp_late']['_verdict'],'incomplete')
        tick([{'prev':'resp_parent','model':'expected'}], [])
        self.assertEqual(m.seen['resp_late']['_req_model'],'expected')
        self.assertEqual(m.seen['resp_late']['_verdict'],'downgrade')
        tick([{'prev':'resp_parent','model':'another'}], [])
        self.assertIsNone(m.seen['resp_late']['_req_model'])
        self.assertEqual(m.seen['resp_late']['_pairing_status'],'ambiguous_previous_response_id')
        self.assertEqual(m.stats()['paired'],0)
        self.assertEqual(m.alerts,[])
        tick([{'prev':'resp_parent','model':'expected'}], [])
        self.assertIsNone(m.seen['resp_late']['_req_model'])
        m.stop()

if __name__=='__main__':unittest.main(verbosity=2)
