"""Fetch a specific output node's video and record media/decoded-content evidence."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from urllib.parse import urlencode
from urllib.request import urlopen


def files(value):
    if isinstance(value, dict):
        if 'filename' in value:
            yield value
        else:
            for child in value.values(): yield from files(child)
    elif isinstance(value, list):
        for child in value: yield from files(child)


def decoded_hash(path, stream):
    args = ['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:' + stream]
    args += ['-pix_fmt', 'rgb24', '-f', 'rawvideo'] if stream == 'v:0' else ['-ac', '2', '-ar', '48000', '-f', 's16le']
    process = subprocess.Popen(args + ['pipe:1'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    digest = hashlib.sha256(); size = 0
    for chunk in iter(lambda: process.stdout.read(1024 * 1024), b''):
        digest.update(chunk); size += len(chunk)
    error = process.stderr.read(); code = process.wait()
    if code: raise RuntimeError(error.decode())
    return {'sha256': digest.hexdigest(), 'bytes': size}


def inspect(path):
    probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-count_frames',
        '-show_entries', 'stream=codec_name,codec_type,width,height,r_frame_rate,duration,nb_read_frames',
        '-of', 'json', str(path)]))
    probe['file_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    probe['video_decoded'] = decoded_hash(path, 'v:0')
    probe['audio_decoded'] = decoded_hash(path, 'a:0')
    volume = subprocess.run(['ffmpeg','-hide_banner','-i',str(path),'-af','volumedetect','-vn','-f','null','-'],capture_output=True,text=True,check=True)
    probe['audio_levels'] = [line.strip() for line in volume.stderr.splitlines() if 'mean_volume:' in line or 'max_volume:' in line]
    return probe


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('comfy-url','prompt-id','output-node','source','output-dir'):
        p.add_argument('--'+name,required=True)
    p.add_argument('--reference')
    args=p.parse_args();root=Path(args.output_dir);root.mkdir(parents=True,exist_ok=True)
    history=json.load(urlopen(args.comfy_url.rstrip('/')+'/history/'+args.prompt_id,timeout=15))[args.prompt_id]
    assert history['status']['status_str']=='success'
    video=next(f for f in files(history['outputs'][args.output_node]) if f['filename'].endswith('.mp4'))
    url=args.comfy_url.rstrip('/')+'/view?'+urlencode({k:video[k] for k in ('filename','subfolder','type')})
    path=root/'rendered.mp4';path.write_bytes(urlopen(url,timeout=30).read())
    result={'prompt_id':args.prompt_id,'output_node':args.output_node,'output_reference':video,'rendered':inspect(path),'source':inspect(Path(args.source))}
    if args.reference:result['reference']=inspect(Path(args.reference))
    streams=result['rendered']['streams'];v=next(s for s in streams if s['codec_type']=='video');a=next(s for s in streams if s['codec_type']=='audio')
    source_v=next(s for s in result['source']['streams'] if s['codec_type']=='video')
    assert v['width']==source_v['width'] and v['height']==source_v['height']
    assert v['nb_read_frames']==source_v['nb_read_frames'] and v['r_frame_rate']==source_v['r_frame_rate']
    assert abs(float(v['duration'])-float(a['duration']))<.1
    result['media_shape_pass']=True
    (root/'verification.json').write_text(json.dumps(result,indent=2)+'\n')
    subprocess.run(['ffmpeg','-v','error','-y','-i',str(path),'-vf',"select='eq(n,0)+eq(n,40)+eq(n,79)',scale=424:-1,tile=3x1",'-frames:v','1',str(root/'frames.jpg')],check=True)
    print(json.dumps({'prompt_id':args.prompt_id,'media_shape_pass':True,'output_reference':video,'audio_levels':result['rendered']['audio_levels']}))


if __name__=='__main__': main()
