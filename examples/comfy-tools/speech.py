"""Make the people in a clip say another artist's name, in their own recorded voices.

The catalog's "speech_edits" lists, per edit: stretches of the clip's voice track that hold the line
("context"), the words of those stretches with the new name in place ("text"), and the spans to replace
("spans"). The F5-TTS speech-edit model regenerates only the spans, conditioned on the rest of the
context, so the new words come out in the voice that says the line. Each seed gives one take; the takes
differ, and which one sounds right is judged by ear, so the caller keeps every take.

The model runs as a Cue job on a host where meme_swap's deploy/host/speech_edit_install.sh has been run,
on the CPU (observation: about 15 s per take on the A100 host's CPUs, so a GPU render can run beside it).
Files reach that host through a directory the Cue node and an ssh host both see.
"""
import hashlib
import json
import subprocess
import wave
from pathlib import Path

from tnn1t1s_comfy.client import Cue, explain

from captions import fill

# Sample rate F5-TTS works at; the job's audio must be mono at this rate (speech_edit.py checks).
MODEL_RATE_HZ = 24000
# One take per seed. Default: three, the number whose takes differed audibly in pitch and voicing on the
# measured clip (first word voiced in 47, 11 and 59 frames across seeds 1-3).
SEEDS = (1, 2, 3)
# Cue resources for the job. Observation: three takes of a 5 s line ran in about 50 s on 8 CPUs, model
# load included, within 12 GB.
JOB_CPUS, JOB_MEM_MB = 8, 12000
# Seconds Cue lets the job run, and this client waits. Default: about 6x the observed run per edit.
JOB_RUNTIME_S = 300
# Each new span is blended in over this long at both edges. Heuristic: 30 ms hides the joins on the
# measured clip without letting the old word through.
CROSSFADE_S = 0.03


def edits_in_cut(clip, start, duration, artist):
    """The catalog's speech edits whose spans fall in the cut, with times moved to the cut and each span's
    position in the job's audio worked out. A span cut in half by the cut's edge is an error."""
    end, kept = start + duration, []
    for edit in clip.get('speech_edits', []):
        inside = [start <= a and b <= end for a, b in edit['spans']]
        if not any(inside) and all(b <= start or a >= end for a, b in edit['spans']):
            continue
        if not all(inside):
            raise SystemExit(f'--artist: the cut {start:g}-{end:g} s splits a line that names the artist '
                             f'(spans {edit["spans"]}); move --start or --end off it')
        context = [(max(a, start), min(b, end)) for a, b in edit['context'] if min(b, end) > max(a, start)]
        spans, offset = [], 0.0
        placed = {}
        for a, b in context:
            for s, e in edit['spans']:
                if a <= s and e <= b:
                    placed[(s, e)] = offset + s - a
            offset += b - a
        for s, e in edit['spans']:
            if (s, e) not in placed:
                raise SystemExit(f'--artist: span {s}-{e} s is not inside its context {context} in this cut')
            spans.append({'cut': [s - start, e - start], 'audio': [placed[(s, e)], placed[(s, e)] + e - s]})
        kept.append({'who': edit['who'], 'context': [[a - start, b - start] for a, b in context],
                     'text': fill(edit['text'], artist, clip['artist']), 'spans': spans})
    return kept


def _ffmpeg(*args):
    done = subprocess.run(['ffmpeg', '-hide_banner', '-nostdin', '-y', '-loglevel', 'error', *args],
                          capture_output=True, text=True)
    if done.returncode:
        raise SystemExit(f'ffmpeg failed: {" ".join(args)}\n{done.stderr[-2000:]}')


def _remote(*cmd):
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode:
        raise SystemExit(f'{" ".join(cmd)} failed: {done.stderr.strip()[-1000:]}')


def _read(path):
    """A 16-bit wav as float samples, shape (frames, channels), with its rate."""
    import numpy as np
    with wave.open(str(path)) as w:
        if w.getsampwidth() != 2:
            raise SystemExit(f'{path} is not 16-bit PCM')
        data = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').reshape(-1, w.getnchannels())
        return data.astype('float64') / 32768, w.getframerate()


def _write(path, samples, rate):
    import numpy as np
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(samples.shape[1])
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes())


def take_files(target, voices, edits, work, seeds=SEEDS):
    """Run every edit for every seed; returns {seed: [take wav per edit]} at the model's rate.
    target: {'cue_url', 'submitter', 'stage' (user@host:/dir), 'root', 'node' {attribute: value}}."""
    host, _, stage_dir = target['stage'].partition(':')
    if not host or not stage_dir.startswith('/'):
        raise SystemExit(f'--speech-stage must be USER@HOST:/ABSOLUTE/DIR (got {target["stage"]!r})')
    cue, takes = Cue(target['cue_url']), {seed: [] for seed in seeds}
    for index, edit in enumerate(edits):
        job = work / f'edit-{index}'
        job.mkdir(parents=True, exist_ok=True)
        cuts = ';'.join(f'[0:a]atrim={a:.3f}:{b:.3f},asetpts=N/SR/TB[s{n}]' for n, (a, b) in enumerate(edit['context']))
        joined = ''.join(f'[s{n}]' for n in range(len(edit['context'])))
        _ffmpeg('-i', str(voices), '-filter_complex', f'{cuts};{joined}concat=n={len(edit["context"])}:v=0:a=1[a]',
                '-map', '[a]', '-ac', '1', '-ar', str(MODEL_RATE_HZ), '-c:a', 'pcm_s16le', str(job / 'line.wav'))
        spec = {'audio': 'line.wav', 'text': edit['text'], 'parts': [s['audio'] for s in edit['spans']],
                'seeds': list(seeds)}
        (job / 'spec.json').write_text(json.dumps(spec, indent=2))
        wanted = [job / f'take-{seed}.wav' for seed in seeds]
        # The job is named by its inputs, so the same line, words and seeds reuse takes already made.
        digest = hashlib.sha256((job / 'line.wav').read_bytes() + json.dumps(spec, sort_keys=True).encode()).hexdigest()
        job_id = f'speech-edit-{digest[:12]}'
        if not (all(p.is_file() for p in wanted) and (job / 'job-id').is_file()
                and (job / 'job-id').read_text() == job_id):
            remote = f'{stage_dir}/jobs/{job_id}'
            _remote('ssh', host, 'mkdir', '-p', remote)
            _remote('scp', '-q', str(job / 'line.wav'), str(job / 'spec.json'), f'{host}:{remote}/')
            cue.submit({'id': job_id, 'submitter': target['submitter'],
                        'command': f'sh {stage_dir}/host/speech_edit.sh {target["root"]} {remote} 2>&1',
                        'cpus': JOB_CPUS, 'mem': JOB_MEM_MB, 'gpus': 0, 'priority': 50, 'retries': 0,
                        'max_runtime_seconds': JOB_RUNTIME_S, 'result_max_bytes': 4096,
                        'placement': {'node_attributes': target['node']}})
            print(f'speech edit {index + 1}/{len(edits)}: Cue job {job_id} ({edit["text"]!r})', flush=True)
            done = cue.wait(job_id, timeout=JOB_RUNTIME_S + 60, poll=5)
            if done['status'] != 'succeeded':
                raise SystemExit(f'speech edit job {job_id}: {explain(done)}')
            _remote('scp', '-q', *[f'{host}:{remote}/{p.name}' for p in wanted], str(job) + '/')
            (job / 'job-id').write_text(job_id)
        for seed, path in zip(seeds, wanted):
            takes[seed].append(path)
    return takes


def splice(voices, edits, takes, target):
    """Write target: the voice track with each edit's spans replaced from that edit's take. Each new span is
    levelled to the loudness (RMS) of the span it replaces, so a take cannot come out louder than the line."""
    import numpy as np
    track, rate = _read(voices)
    out, gains = track.copy(), []
    fade = round(CROSSFADE_S * rate)
    for edit, take_path in zip(edits, takes):
        resampled = take_path.with_name(take_path.stem + f'-{rate}.wav')
        _ffmpeg('-i', str(take_path), '-ar', str(rate), '-ac', str(track.shape[1]), '-c:a', 'pcm_s16le', str(resampled))
        take, _ = _read(resampled)
        for span in edit['spans']:
            a, b = (round(t * rate) for t in span['cut'])
            src = round(span['audio'][0] * rate)
            new, old = take[src:src + b - a], track[a:b]
            if len(new) != b - a or b - a <= 2 * fade:
                raise SystemExit(f'speech edit: take {take_path} does not cover span {span} of the line')
            new_rms = float(np.sqrt(np.mean(new ** 2)))
            if new_rms == 0:
                raise SystemExit(f'speech edit: take {take_path} is silent over span {span}')
            gain = float(np.sqrt(np.mean(old ** 2))) / new_rms
            weight = np.ones(b - a)
            weight[:fade], weight[-fade:] = np.linspace(0, 1, fade), np.linspace(1, 0, fade)
            out[a:b] = old * (1 - weight[:, None]) + new * gain * weight[:, None]
            gains.append(round(20 * float(np.log10(gain)), 1))
    _write(target, out, rate)
    return gains
