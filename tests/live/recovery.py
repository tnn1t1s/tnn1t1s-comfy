import json,pathlib,time,uuid,subprocess,importlib.util
from tnn1t1s_comfy.client import API,Cue
spec=importlib.util.spec_from_file_location('url','deploy/fennel_url.py');discovery=importlib.util.module_from_spec(spec);spec.loader.exec_module(discovery)
import argparse
p=argparse.ArgumentParser();p.add_argument('--fennel-url',required=True);p.add_argument('--cue-url',required=True);p.add_argument('--vars',required=True);p.add_argument('--evidence',required=True);args=p.parse_args()
f=API(args.fennel_url);cue=Cue(args.cue_url)
app='/v2/apps/svc/tnn1t1s-comfy-gateway-test';proof={}

def gateway():return API(discovery.resolve(f.request(app)))
def until(fn,timeout=100):
 deadline=time.monotonic()+timeout
 while time.monotonic()<deadline:
  try:
   r=fn()
   if r:return r
  except (OSError,RuntimeError,ValueError,KeyError):pass
  time.sleep(.5)
 raise TimeoutError('condition')
graph={'1':{'class_type':'EmptyImage','inputs':{'width':16,'height':16,'batch_size':1,'color':42}},'2':{'class_type':'SaveImage','inputs':{'images':['1',0],'filename_prefix':'recovery'}}}
pid=str(uuid.uuid4());g=until(gateway);g.request('/prompt',{'prompt_id':pid,'prompt':graph});job='comfy-'+pid
until(lambda:cue.job(job)['status']=='running');print('running',pid,flush=True)
# Change only this isolated app via its existing declarative Ansible recipe.
import os
env={**os.environ,'LC_ALL':'en_US.UTF-8','LANG':'en_US.UTF-8'}
subprocess.run(['ansible-playbook','-i','localhost,','tests/live/restart.yml','-e','@'+args.vars],env=env,check=True,stdout=subprocess.DEVNULL)
g=until(gateway);h=until(lambda:g.request('/history/'+pid).get(pid));j=cue.job(job)
assert h['status']['status_str']=='success',h
assert len(j['instances'])==1,j
proof['restart']={'prompt_id':pid,'cue_job_id':job,'instances':j['instances'],'history_status':h['status']}
print('RESTART PASS: same job, one attempt',flush=True)
for route in ('legacy','job'):
 pid=str(uuid.uuid4());g.request('/prompt',{'prompt_id':pid,'prompt':graph});job='comfy-'+pid
 until(lambda:cue.job(job)['status']=='running')
 until(lambda:any(r['prompt_id']==pid and r['state']=='running' for r in g.request('/cue/status')))
 result=g.request('/interrupt',{'prompt_id':pid}) if route=='legacy' else g.request('/api/jobs/'+pid+'/cancel',{})
 terminal=cue.wait(job,timeout=60);assert terminal['status']=='cancelled',terminal
 h=until(lambda:g.request('/history/'+pid).get(pid));assert h['status']['status_str']=='error'
 proof[route]={'prompt_id':pid,'response':result,'terminal':terminal['status'],'instances':terminal['instances'],'history_status':h['status']}
 print(route,'CANCEL PASS',flush=True)
pathlib.Path(args.evidence).write_text(json.dumps(proof,indent=2)+'\n')
print('ALL PASS',flush=True)
