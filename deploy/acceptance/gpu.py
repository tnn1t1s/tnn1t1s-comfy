import asyncio,copy,hashlib,json,os,subprocess,time,uuid,urllib.request
from pathlib import Path
from aiohttp import ClientSession,ClientTimeout,WSMsgType,FormData
import argparse
parser=argparse.ArgumentParser(description='Ops acceptance: full video, active-sampling cancellation and CUDA/resource reuse. Requires an idle capacity window.')
for option in ('comfy-url','cue-url','controller-url','graph','inputs-dir','worker-spec','evidence-dir','ssh-host','output-node'):
 parser.add_argument('--'+option,required=True)
parser.add_argument('--ssh-user',required=True)
args=parser.parse_args()
ROOT=Path(args.evidence_dir);ROOT.mkdir(parents=True,exist_ok=True)
URL=args.comfy_url.rstrip('/');CUE=args.cue_url.rstrip('/');CTRL=args.controller_url.rstrip('/')
WORKER=json.loads(Path(args.worker_spec).read_text())
report={'cases':[]}
def save(): (ROOT/'report.json').write_text(json.dumps(report,indent=2)+'\n')
def req(base,path,body=None,method=None):
 r=urllib.request.Request(base+path,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'},method=method)
 return json.load(urllib.request.urlopen(r,timeout=15))
def node():
 n=next(n for n in req(CTRL,'/state')['nodes'] if all(n.get('attributes',{}).get(k)==v for k,v in WORKER['placement']['node_attributes'].items()))
 return {k:n.get(k) for k in ('id','resources','used_resources','stores')}
def scan(prompt):
 code='''import pathlib,json
rows=[]
for p in pathlib.Path('/proc').iterdir():
 if not p.name.isdigit():continue
 try:
  env=(p/'environ').read_bytes().split(b'\\0')
  if any(x.startswith(b'COMFY_PAYLOAD=') and MARKER.encode() in x for x in env):
   rows.append({'pid':int(p.name),'cmd':(p/'cmdline').read_bytes().replace(b'\\0',b' ').decode(errors='replace'),'cgroup':(p/'cgroup').read_text()})
 except (OSError,PermissionError):pass
print(json.dumps(rows))
'''.replace('MARKER',repr(prompt))
 return json.loads(subprocess.check_output(['ssh','-o','BatchMode=yes',args.ssh_user+'@'+args.ssh_host,'sudo -n python3 -'],input=code,text=True,timeout=20))
async def case(mode):
 pid=str(uuid.uuid4()); graph=json.loads(Path(args.graph).read_text());sampling_nodes={k for k,n in graph.items() if n['class_type'] in ('KSampler','KSamplerAdvanced','SamplerCustom','SamplerCustomAdvanced')};r={'mode':mode,'prompt_id':pid,'events':[],'preview_count':0,'baseline':node()};report['cases'].append(r);save();terminal=False
 async with ClientSession(timeout=ClientTimeout(total=20)) as s:
  async def api(path,body=None):
   async with s.request('GET' if body is None else 'POST',URL+path,json=body) as response:
    if response.status>=400:raise RuntimeError(await response.text())
    return await response.json()
  try:
   for n in graph.values():
    if n['class_type'] in ('LoadImage','LoadVideo'):
     key='image' if n['class_type']=='LoadImage' else 'file';name=n['inputs'][key];data=FormData();data.add_field('image',(Path(args.inputs_dir)/name).read_bytes(),filename=pid+'-'+name);data.add_field('type','input')
     async with s.post(URL+'/upload/image',data=data) as resp:
      assert resp.status==200,await resp.text();n['inputs'][key]=(await resp.json())['name']
   r['graph']=graph
   async with s.ws_connect(URL+'/ws?clientId='+pid,max_msg_size=8*1024*1024) as ws:
    await ws.send_json({'type':'feature_flags','data':{'supports_preview_metadata':True}})
    r['submitted_at']=time.time();r['submission']=await api('/prompt',{'prompt_id':pid,'client_id':pid,'prompt':graph});save();print(mode,'SUBMITTED',pid,flush=True)
    deadline=time.monotonic()+1700;last_poll=0;current=None
    while time.monotonic()<deadline:
     try: m=await ws.receive(timeout=.5)
     except asyncio.TimeoutError:m=None
     if m and m.type==WSMsgType.TEXT:
      e=json.loads(m.data)
      if e['type'] in ('executing','progress','execution_error','execution_success','executed'):
       r['events'].append({'at':time.time(),**e})
       if e['type']=='executing' and e['data'].get('node')!=current:
        current=e['data'].get('node');print(mode,'NODE',current,flush=True)
       if mode=='cancel' and not r.get('cancel_at') and e['type']=='progress' and current in sampling_nodes and e['data'].get('value',0)>=1:
        r['processes_before_cancel']=scan(pid);r['allocated']=node();r['cancel_at']=time.time();r['cancel_response']=await api('/interrupt',{'prompt_id':pid});save();print('CANCEL DURING SAMPLING',pid,flush=True)
     elif m and m.type==WSMsgType.BINARY:
      r['preview_count']+=1
     if time.monotonic()-last_poll>2:
      last_poll=time.monotonic();h=await api('/history/'+pid);save()
      if pid in h:
       r['history']=h[pid];terminal=True;break
    else:raise TimeoutError('GPU acceptance exceeded deadline')
   r['finished_at']=time.time();r['cue_job']=req(CUE,'/jobs/comfy-'+pid)
   expected='succeeded' if mode=='success' else 'cancelled';assert r['cue_job']['status']==expected,r['cue_job'];assert r['history']['status']['status_str']==('success' if mode=='success' else 'error'),r['history']
   if mode=='cancel':assert r.get('cancel_at') and r['processes_before_cancel']
   if mode=='success':
    def files(x):
     if isinstance(x,dict):
      if 'filename' in x:yield x
      else:
       for v in x.values():yield from files(v)
     elif isinstance(x,list):
      for v in x:yield from files(v)
    from urllib.parse import urlencode
    video=next(f for f in files(r['history']['outputs'][args.output_node]) if f['filename'].endswith('.mp4'))
    async with s.get(URL+'/view?'+urlencode({k:video[k] for k in ('filename','subfolder','type')})) as resp:
     resp.raise_for_status();data=await resp.read();(ROOT/'output.mp4').write_bytes(data)
    r['video_sha256']=hashlib.sha256(data).hexdigest()
   for _ in range(20):
    remaining=scan(pid)
    if not remaining:break
    await asyncio.sleep(.5)
   r['remaining_processes']=remaining;assert not remaining,remaining
   r['after']=node()
   for key in ('cpus','mem','gpus'):assert r['baseline']['used_resources'].get(key,0)==r['after']['used_resources'].get(key,0),(key,r['baseline'],r['after'])
   assert r['baseline']['stores']==r['after']['stores']
   r['passed']=True;save();print(mode,'PASS previews',r['preview_count'],'wall',r['finished_at']-r['submitted_at'],flush=True)
  except Exception as exc:r['error']=repr(exc);save();raise
  finally:
   if not terminal:
    try:
     await api('/interrupt',{'prompt_id':pid})
     for _ in range(60):
      job=req(CUE,'/jobs/comfy-'+pid)
      if job['status'] in ('succeeded','failed','cancelled'):r['cleanup_job']=job;break
      await asyncio.sleep(1)
    except Exception as exc:r['cleanup_error']=repr(exc)
    save()
async def main():
 await case('success');await case('cancel')
 # Same-size follower proves compute is schedulable and CUDA is usable again.
 spec=copy.deepcopy(WORKER)
 spec.update(id='ops-comfy-gpu-reuse-'+uuid.uuid4().hex[:10],submitter=WORKER['submitter'],retries=0,max_runtime_seconds=120,result_max_bytes=4096,command="/opt/venv/bin/python -c \"import torch; x=torch.ones(16,device='cuda'); assert x.sum().item()==16; print('CUDA_REUSE_PASS')\"")
 report['follower_spec']=spec;req(CUE,'/jobs',spec);save()
 for _ in range(150):
  job=req(CUE,'/jobs/'+spec['id'])
  if job['status'] in ('succeeded','failed','cancelled'):break
  await asyncio.sleep(1)
 if job['status'] not in ('succeeded','failed','cancelled'):
  req(CUE,'/jobs/'+spec['id'],{'submitter':spec['submitter']},method='DELETE')
  for _ in range(60):
   job=req(CUE,'/jobs/'+spec['id'])
   if job['status'] in ('succeeded','failed','cancelled'):break
   await asyncio.sleep(1)
 report['follower']=job;save();assert job['status']=='succeeded',job
 print('CUDA FOLLOWER PASS',flush=True)
if __name__=='__main__':asyncio.run(main())
