import json,pathlib,time,uuid,urllib.request,urllib.parse,hashlib
from tnn1t1s_comfy.client import API
import argparse
p=argparse.ArgumentParser();p.add_argument('--url',required=True);p.add_argument('--evidence',required=True);args=p.parse_args()
api=API(args.url);pid=str(uuid.uuid4())
graph={'1':{'class_type':'EmptyImage','inputs':{'width':16,'height':16,'batch_size':1,'color':65280}},'2':{'class_type':'SaveImage','inputs':{'images':['1',0],'filename_prefix':'acceptance'}}}
print('submit',pid,api.request('/prompt',{'prompt_id':pid,'prompt':graph}),flush=True)
for _ in range(100):
 h=api.request('/history/'+pid)
 if pid in h:
  r=h[pid];print(json.dumps(r),flush=True)
  assert r['status']['status_str']=='success'
  file=r['outputs']['2']['images'][0]
  data=urllib.request.urlopen(api.url+'/view?'+urllib.parse.urlencode(file),timeout=10).read()
  assert data.startswith(b'\x89PNG')
  proof={'prompt_id':pid,'history':r,'png_bytes':len(data),'png_sha256':hashlib.sha256(data).hexdigest(),'adapter':api.request('/cue/status')}
  pathlib.Path(args.evidence).write_text(json.dumps(proof,indent=2)+'\n')
  print('PASS',len(data),flush=True);break
 time.sleep(1)
else:
 print(api.request('/cue/status'));raise TimeoutError('history')
