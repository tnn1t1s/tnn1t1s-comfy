"""Replace a clip's music with another song and have the people say another artist's name.

The clip's audio is split into voices and everything else by the Mel-Band RoFormer node on ComfyUI.
The voices are kept whole. With a song, everything else is replaced by the song, played low with reverb
and its bass removed so it sits behind the dialogue. With an artist, the words that name the clip's
artist are regenerated in the speakers' own voices (speech.py); each seed gives one take and every take
becomes its own track, because which take sounds right is judged by ear.
Mixing is ffmpeg on this machine: a few seconds of audio filtering per run.
"""
import json
import re
import subprocess
from pathlib import Path

from tnn1t1s_comfy.run import run

import speech

# The one separation model in meme_swap's models.lock.tsv (fp16: half the download of fp32;
# no quality comparison of the two was run here).
SEPARATION_MODEL = 'MelBandRoformer_fp16.safetensors'
# Seconds to wait for the separation job. Observation: 24 s of audio took under 30 s on an A100,
# model load included; the default leaves room for a cold server.
SEPARATION_WAIT_S = 300
# Sample rate of every intermediate file; the separation node works at 44.1 kHz.
RATE_HZ = 44100
# How the song is treated so it reads as music in the room behind the dialogue: three echoes for
# reverb, then a high-pass that removes the bass. Default: the treatment the operator accepted by ear
# on one song ("low, with reverb and a 200 Hz high-pass"); echo delays and decays were not varied.
SONG_FILTER = 'aecho=0.8:0.7:35|80|140:0.45|0.3|0.2,highpass=f=200'
# Mean level in dB of the treated song over the cut. Default: the operator's pick among -37, -31, -27
# and -23 on one song under this clip's dialogue (dialogue about -23.6 dB). Parameter: --song-level.
SONG_LEVEL_DB = -27.0
AUDIO_BITRATE = '192k'


def _ffmpeg(*args):
    """Run ffmpeg and return what it logged (measurement filters report on stderr)."""
    done = subprocess.run(['ffmpeg', '-hide_banner', '-nostdin', '-y', *args], capture_output=True, text=True)
    if done.returncode:
        raise SystemExit(f'ffmpeg failed: {" ".join(args)}\n{done.stderr[-2000:]}')
    return done.stderr


def audio_seconds(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0',
                          str(path)], check=True, capture_output=True, text=True).stdout
    return float(out)


def mean_db(path):
    """Mean level of a file in dB."""
    log = _ffmpeg('-i', str(path), '-af', 'volumedetect', '-f', 'null', '-')
    found = re.search(r'mean_volume: (-?[\d.]+|-inf) dB', log)
    if not found or found.group(1) == '-inf':
        raise SystemExit(f'No measurable audio in {path}; cannot set a level from it')
    return float(found.group(1))


def separate(comfy_url, audio, out):
    """Split audio into (voices, everything else) on ComfyUI; returns two local paths."""
    graph = {
        'audio': {'class_type': 'LoadAudio', 'inputs': {'audio': 'unset.wav'}},
        'model': {'class_type': 'MelBandRoFormerModelLoader', 'inputs': {'model_name': SEPARATION_MODEL}},
        'split': {'class_type': 'MelBandRoFormerSampler', 'inputs': {'model': ['model', 0], 'audio': ['audio', 0]}},
        'voices': {'class_type': 'SaveAudio', 'inputs': {'audio': ['split', 0], 'filename_prefix': 'comfy-tools/voices'}},
        'rest': {'class_type': 'SaveAudio', 'inputs': {'audio': ['split', 1], 'filename_prefix': 'comfy-tools/rest'}},
    }
    graph_path = out / 'separate-graph.json'
    graph_path.write_text(json.dumps(graph, indent=2))
    record = run(comfy_url, graph_path, [f'audio.audio={audio}'], [], out / 'separate', SEPARATION_WAIT_S)
    if record['status'] != 'success':
        print(json.dumps(record, indent=2))
        raise SystemExit('ComfyUI audio separation failed')
    paths = {o['node']: Path(o['path']) for o in record['outputs']}
    if sorted(paths) != ['rest', 'voices']:
        raise SystemExit(f'Separation returned outputs for {sorted(paths)}, expected rest and voices')
    return paths['voices'], paths['rest']


def speech_target(args):
    """Where the speech-edit job runs, from the command line; every part is required, none is guessed."""
    need = {'--cue-url': args.cue_url, '--cue-submitter': args.cue_submitter, '--speech-stage': args.speech_stage,
            '--speech-root': args.speech_root, '--speech-node': args.speech_node}
    missing = [flag for flag, value in need.items() if not value]
    if missing:
        raise SystemExit(f'--artist changes spoken words, which runs a Cue job; missing {", ".join(missing)}')
    key, sep, value = args.speech_node.partition('=')
    if not (key and sep and value):
        raise SystemExit(f'--speech-node must be ATTRIBUTE=VALUE, e.g. gpu=a100 (got {args.speech_node!r})')
    return {'cue_url': args.cue_url, 'submitter': args.cue_submitter, 'stage': args.speech_stage,
            'root': args.speech_root, 'node': {key: value}}


def make_audio(args, clip, trimmed, out, start, duration):
    """Build the replacement sound for the cut. Returns ([(name, track)], facts for the report): one track
    named '' when the words are unchanged or --take picks one, else one per take named 'take-<seed>'."""
    work = out / 'audio'
    work.mkdir(parents=True, exist_ok=True)
    source = work / 'clip.wav'
    _ffmpeg('-i', str(trimmed), '-vn', '-ar', str(RATE_HZ), '-ac', '2', str(source))
    voices_flac, rest = separate(args.comfy_url, source, work)
    voices = work / 'voices.wav'
    _ffmpeg('-i', str(voices_flac), '-ar', str(RATE_HZ), '-ac', '2', '-c:a', 'pcm_s16le', str(voices))
    facts = {'separation_model': SEPARATION_MODEL, 'voices': str(voices), 'rest': str(rest)}

    if args.song:
        song = Path(args.song).expanduser()
        if not song.is_file():
            raise SystemExit(f'--song file not found: {song}')
        available = audio_seconds(song) - args.song_start
        if available < duration:
            raise SystemExit(f'--song has {available:.1f} s after --song-start {args.song_start:g}; '
                             f'the cut needs {duration:g} s')
        bed = work / 'song-treated.wav'
        _ffmpeg('-ss', f'{args.song_start:.3f}', '-t', f'{duration:.3f}', '-i', str(song),
                '-af', SONG_FILTER, '-ar', str(RATE_HZ), '-ac', '2', str(bed))
        gain = args.song_level - mean_db(bed)
        facts['song'] = {'file': str(song), 'start': args.song_start, 'filter': SONG_FILTER,
                         'level_db': args.song_level, 'gain_db': round(gain, 1)}
    else:
        bed, gain = rest, 0.0

    # Words change only when the name asked for differs from the one the clip says.
    edits = (speech.edits_in_cut(clip, start, duration, args.artist)
             if args.artist and args.artist != clip['artist'] else [])
    if edits:
        takes = speech.take_files(speech_target(args), voices, edits, work / 'speech')
        if args.take is not None and args.take not in takes:
            raise SystemExit(f'--take {args.take}: takes are {sorted(takes)}')
        chosen = [args.take] if args.take is not None else sorted(takes)
        spoken = []
        for seed in chosen:
            edited = work / f'voices-take-{seed}.wav'
            gains = speech.splice(voices, edits, takes[seed], edited)
            spoken.append(('' if args.take is not None else f'take-{seed}', edited, gains))
        facts['speech'] = {'artist': args.artist, 'model': 'F5-TTS speech edit (meme_swap deploy/host/speech_edit.py)',
                           'edits': [{'who': e['who'], 'text': e['text'], 'spans_s': [s['cut'] for s in e['spans']]}
                                     for e in edits],
                           'takes': {name or f'take-{args.take}': {'span_gains_db': gains} for name, _, gains in spoken}}
    else:
        spoken = [('', voices, None)]

    tracks = []
    for name, voice_track, _ in spoken:
        track = work / f'track{"-" + name if name else ""}.m4a'
        _ffmpeg('-i', str(voice_track), '-i', str(bed), '-filter_complex',
                f'[0:a]aresample={RATE_HZ}[v];[1:a]aresample={RATE_HZ},volume={gain:.2f}dB[bed];'
                '[v][bed]amix=inputs=2:normalize=0:duration=first[a]',
                '-map', '[a]', '-t', f'{duration:.3f}', '-c:a', 'aac', '-b:a', AUDIO_BITRATE, str(track))
        tracks.append((name, track))
    return tracks, facts


def mux(video, track, target):
    """Write target as video's picture with track as its sound; the picture is copied, not re-encoded."""
    _ffmpeg('-i', str(video), '-i', str(track), '-map', '0:v:0', '-map', '1:a:0', '-c', 'copy', '-shortest',
            str(target))
    return target
