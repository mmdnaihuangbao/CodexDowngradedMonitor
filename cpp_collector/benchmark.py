"""Reproducible read-only backend comparison; fixture process is the default target."""
import argparse,json,pathlib,statistics,subprocess,sys,time
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import collector as C
from native_scanner import NativeScanner

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--pid',type=int,help='Optional existing process PID; read-only scan')
    ap.add_argument('--rounds',type=int,default=8)
    args=ap.parse_args();child=None
    if not args.pid:
        child=subprocess.Popen([sys.executable,str(ROOT/'cpp_collector/tests/fixture_process.py')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,creationflags=subprocess.CREATE_NO_WINDOW)
        assert child.stdout.readline().strip()=='ready';args.pid=child.pid
    py=C.ParallelScanner(args.pid,4);native=NativeScanner(args.pid,4)
    measurements={'python':[],'cpp':[]};last={}
    try:
        assert py.open() and native.open()
        for i in range(args.rounds+2):
            for name in (['python','cpp'] if i%2==0 else ['cpp','python']):
                responses=[];requests=[];t=time.perf_counter()
                if name=='python':
                    def consume(data):
                        responses.extend(C.extract_responses(data));requests.extend(C.extract_requests(data).items())
                    size=py.sweep(consume)
                else:
                    batch=native.sweep_records();size=batch['bytes'];responses=batch['responses'];requests=batch['requests']
                elapsed=time.perf_counter()-t
                if i>=2:measurements[name].append(elapsed)
                last[name]={'bytes':size,'unique_responses':len({r['response_id'] for r in responses}),'request_records':len(requests)}
        medians={k:round(statistics.median(v),6) for k,v in measurements.items()}
        print(json.dumps({'target':'controlled_fixture' if child else 'live_process','workers':4,'measured_rounds':args.rounds,'median_seconds':medians,'speedup':round(medians['python']/medians['cpp'],2),'last_scan':last},indent=2))
    finally:
        py.close();native.close()
        if child:child.stdin.close();child.wait(timeout=5);child.stdout.close()
if __name__=='__main__':main()
