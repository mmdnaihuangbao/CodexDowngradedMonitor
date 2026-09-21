"""Allocate controlled fixtures in THIS test process; scanners only read them."""
import ctypes, json, sys
from ctypes import wintypes as W

def response(rid,model,prev=None,effort='xhigh'):
    out={'id':rid,'object':'response','model':model,'created_at':1700000000,
         'completed_at':1700000010,'status':'completed','reasoning':{'effort':effort}}
    if prev:out['previous_response_id']=prev
    return json.dumps(out,separators=(',',':')).encode()

def request(model,prev=None,long=False):
    out={'type':'response.create','model':model}
    if long:out['input']=[{'role':'user','content':'x'*12000}]
    if prev:out['previous_response_id']=prev
    return json.dumps(out,separators=(',',':')).encode()

k=ctypes.WinDLL('kernel32',use_last_error=True)
k.VirtualAlloc.argtypes=[ctypes.c_void_p,ctypes.c_size_t,W.DWORD,W.DWORD]
k.VirtualAlloc.restype=ctypes.c_void_p
size=64*1024*1024
base=k.VirtualAlloc(None,size,0x3000,4)
if not base:raise ctypes.WinError(ctypes.get_last_error())
ctypes.memset(base,ord('P'),size)
objects=[request('gpt-6-astra','resp_p1',True),response('resp_normal','gpt-6-astra','resp_p1'),
         request('gpt-6-astra','resp_p2'),response('resp_down','luna-mini','resp_p2'),
         request('luna-mini','resp_p3'),response('resp_sub','luna-mini','resp_p3','low'),
         response('resp_unknown','other-model','resp_absent'),request('gpt-6-astra'),
         response('resp_first','other-model')]
for i,obj in enumerate(objects):ctypes.memmove(base+65536*i,obj,len(obj))
# Deliberately straddle the scanner's 1 MiB block boundary.
boundary=response('resp_boundary','gpt-6-astra','resp_p1')
ctypes.memmove(base+1024*1024-35,boundary,len(boundary))
print('ready',flush=True)
sys.stdin.readline()
