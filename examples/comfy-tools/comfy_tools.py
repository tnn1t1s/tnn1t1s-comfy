#!/usr/bin/env python3
"""Put two people from photos into a catalogued clip, and change its song and the artist it names.

    comfy_tools.py swap --clip the-smiths --male M.jpg --female F.jpg --out DIR
    comfy_tools.py dub --clip the-smiths --video RENDER.mp4 --song S.m4a --artist NAME --out DIR
    comfy_tools.py clips

Each person in the clip is assigned by left-to-right position (clips.json says which role
sits where) and replaced in one pass per person. Three engines:

  wan (default)  Wan 2.2 Animate replace mode on ComfyUI, through meme_swap's swap.py. Regenerates
                 the whole person (head, hair, body) with the clip's motion. Needs COMFY_URL,
                 MEME_SWAP_DIR and --wan-reference redraw|photo. About 60 s per 4.5 s of clip per
                 person on an A100 (observed); the GB10 logged about 1170 s for the same.
  facefusion     FaceFusion 3.9.2 headless, run in the meme-swap-comfy image on spark over ssh.
                 Swaps the inner face only, per frame. Needs SPARK_SSH (or --ssh-host).
  reactor        ComfyUI + ComfyUI-ReActor, inner face only. Needs COMFY_URL (or --comfy-url).

What the catalog adds to a Wan render of a clip:
  reference changes  Wan draws a person from one reference image, so something they wear or hold that
                     changes state (headphones pulled down to the neck) stays as the reference shows it.
                     A person's "reference_changes" switch to a second reference, redrawn from a later
                     frame, and a prompt that states the new state, where a sampling pass starts.
  captions           Wan garbles burned-in caption text near a person. "captions" lists each line; the
                     old text is removed and every line drawn again (captions.py).

--song replaces the clip's music; --artist makes the captions and the spoken lines name another artist
(dub.py, speech.py). Changing spoken words gives one take per seed: without --take every take is written
as its own video and opened, to be chosen by ear; --take N writes that one. `swap` applies these to its
render; `dub` applies them to a render made earlier (the file swap wrote as swap-render.mp4), which takes
about a minute because nothing is rendered.

Needs yt-dlp, ffmpeg and ffprobe on PATH; opencv, pillow and numpy for captions and spoken words.
"""
import argparse
import hashlib
import json
import math
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tnn1t1s_comfy.client import API  # noqa: E402
from tnn1t1s_comfy.run import run  # noqa: E402
import captions  # noqa: E402
import dub as audio_dub  # noqa: E402

CATALOG = Path(__file__).with_name('clips.json')
# Default swapper: FaceFusion describes 1b as best quality in most cases (self-reported,
# not independently benchmarked). A hypothesis for the probe to test against 1a/1c/inswapper.
DEFAULT_SWAPPER = 'hyperswap_1b_256.onnx'
# Clip faces are ~100 px tall and the swapper outputs 256 px, so restoration starts off.
DEFAULT_RESTORE = 'none'
# Trim encode quality: visually lossless at this size; the swap, not the trim, should set quality.
TRIM_CRF = 16

# Wan engine. Short side in pixels of the clip Wan renders: 480 is the size meme_swap's logged
# renders used (demo/attempts.jsonl, about 1170 s per 5-8 s pass on the GB10). Parameter; larger is slower.
WAN_SHORT_SIDE = 480
# Redraw prompt for --wan-outfit: swap.py's built-in prompt carries over face and hair only, so the
# reference keeps the clip person's clothes and Wan reproduces them. This one also asks for the photo's
# clothing. Hypothesis: untested wording; the photo must show the clothing for it to have any effect.
WAN_OUTFIT_PROMPT = (
    'Image 1 is a still from a film. Image 2 is a photo of a person. Return image 1 with the same '
    'composition, camera angle, framing, lighting, background, and every other person unchanged. '
    'Replace {who} entirely with the person from image 2: their exact face, hair, facial hair, glasses, '
    'accessories, and the clothing they wear in image 2, with the same garments, colours and patterns. '
    "Keep the replaced person's pose, position, size in frame, and anything in their hands. "
    'Photorealistic, natural colour, matching the film grain of image 1.')
# Per-render wait in seconds. Default: swap.py's own --comfy-timeout default, about 3x a logged pass.
RENDER_WAIT_S = 3600
# Sampling seed of every Wan pass. Default: the seed of the one full render the operator accepted; the
# same reference and switch with other preceding frames lost a prop, so results are not seed-independent.
WAN_SEED = 1

# FaceFusion engine. The image is the one /svc/meme-swap-comfy runs (by digest), so the venv FaceFusion
# installed on the store matches its Python. Store paths are the host side and container side of the
# `results` store mount used by that app.
FF_IMAGE = ('localhost:15000/meme-swap-comfy@sha256:'
            'bafd5e87f8d46d33f99a947a5408ae70f864fc9aa2b69372983be62d7a4c81df')
FF_HOST_STORE = '/var/lib/medios-results'
FF_CONTAINER_STORE = '/models'
FF_RUNS = 'meme-swap/runs'
FF_SCRIPT = Path(__file__).with_name('facefusion_swap.sh')
# Default swapper for FaceFusion (FaceFusion's own description: best quality in most cases; unbenchmarked here).
FF_DEFAULT_MODEL = 'hyperswap_1b_256'
# Pixel boost runs the 256 px swapper on a 512 px crop in tiles. Hypothesis: helps ~100 px clip faces.
FF_DEFAULT_BOOST = '512x512'


def sh(*cmd):
    subprocess.run(cmd, check=True)


def probe(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-count_frames', '-show_entries',
                          'stream=codec_type,nb_read_frames,width,height,r_frame_rate:format=duration',
                          '-of', 'json', str(path)], check=True, capture_output=True, text=True).stdout
    data = json.loads(out)
    video = next(s for s in data['streams'] if s['codec_type'] == 'video')
    return {'duration': float(data['format']['duration']), 'frames': int(video['nb_read_frames']),
            'size': f"{video['width']}x{video['height']}", 'fps': video['r_frame_rate'],
            'audio': any(s['codec_type'] == 'audio' for s in data['streams'])}


def prepare_clip(name, clip, out, start, duration):
    cache = out / 'cache'
    cache.mkdir(parents=True, exist_ok=True)
    sources = list(cache.glob(f'{name}.*'))
    if not sources:
        sh('yt-dlp', '-q', '-o', str(cache / f'{name}.%(ext)s'), clip['url'])
        sources = list(cache.glob(f'{name}.*'))
    trimmed = out / f'{name}-{start:g}-{duration:g}s.mp4'
    sh('ffmpeg', '-v', 'error', '-y', '-ss', str(start), '-t', str(duration), '-i', str(sources[0]),
       '-c:v', 'libx264', '-crf', str(TRIM_CRF), '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(trimmed))
    return trimmed


def node_schema(comfy, cls):
    info = comfy.request('/object_info/' + quote(cls, safe=''))
    if cls not in (info or {}):
        raise SystemExit(f'ComfyUI at {comfy.url} has no {cls} node; install ComfyUI-ReActor first')
    return info[cls]['input']


def check_choice(schema, field, value):
    options = schema['required'][field][0]
    if value not in options:
        raise SystemExit(f'{field}={value!r} is not installed; available: {options}')


def build_graph(roles, swap_model, restore):
    graph = {
        'clip': {'class_type': 'LoadVideo', 'inputs': {'file': 'unset.mp4'}},
        'parts': {'class_type': 'GetVideoComponents', 'inputs': {'video': ['clip', 0]}},
    }
    image = ['parts', 0]
    for index, role in enumerate(roles):
        graph[f'{role}_face'] = {'class_type': 'LoadImage', 'inputs': {'image': 'unset.png'}}
        graph[f'{role}_options'] = {'class_type': 'ReActorOptions', 'inputs': {
            'input_faces_order': 'left-right', 'input_faces_index': str(index),
            'detect_gender_input': 'no', 'source_faces_order': 'large-small',
            'source_faces_index': '0', 'detect_gender_source': 'no',
            'console_log_level': 1, 'restore_swapped_only': True}}
        graph[f'{role}_swap'] = {'class_type': 'ReActorFaceSwapOpt', 'inputs': {
            'enabled': True, 'input_image': image, 'swap_model': swap_model,
            'facedetection': 'retinaface_resnet50', 'face_restore_model': restore,
            'face_restore_visibility': 1.0, 'codeformer_weight': 0.5,
            'source_image': [f'{role}_face', 0], 'options': [f'{role}_options', 0]}}
        image = [f'{role}_swap', 0]
    graph['video'] = {'class_type': 'CreateVideo', 'inputs': {
        'images': image, 'audio': ['parts', 1], 'fps': ['parts', 2]}}
    graph['save'] = {'class_type': 'SaveVideo', 'inputs': {
        'video': ['video', 0], 'filename_prefix': 'comfy-tools/swap',
        'format': 'mp4', 'format.codec': 'auto'}}
    return graph


def contact_sheet(source, result, path, count=6):
    """Top row source frames, bottom row swapped frames, at the same timestamps."""
    fps = f'fps={count}/{probe(source)["duration"]}'
    rows = []
    for name, clip in (('src', source), ('out', result)):
        row = path.with_name(f'{path.stem}-{name}.jpg')
        sh('ffmpeg', '-v', 'error', '-y', '-i', str(clip), '-vf', f'{fps},scale=320:-1,tile={count}x1',
           '-frames:v', '1', str(row))
        rows.append(row)
    sh('ffmpeg', '-v', 'error', '-y', '-i', str(rows[0]), '-i', str(rows[1]),
       '-filter_complex', 'vstack', str(path))
    for row in rows:
        row.unlink()


def review_sheet(refs, roles, sheet, path, height=240):
    """References in clip order (left to right) above the source/swapped contact sheet."""
    width = int(probe_image_width(sheet))
    inputs = [arg for role in roles for arg in ('-i', str(refs[role]))]
    scaled = ''.join(f'[{i}]scale=-2:{height}[r{i}];' for i in range(len(roles)))
    stacked = ''.join(f'[r{i}]' for i in range(len(roles))) + f'hstack={len(roles)},pad={width}:{height}:(ow-iw)/2:0[refs];'
    sh('ffmpeg', '-v', 'error', '-y', *inputs, '-i', str(sheet), '-filter_complex',
       scaled + stacked + f'[refs][{len(roles)}]vstack', str(path))


def probe_image_width(path):
    return subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=width', '-of', 'csv=p=0', str(path)],
                          check=True, capture_output=True, text=True).stdout.strip()


def reactor_render(args, roles, refs, trimmed, out):
    if not args.comfy_url:
        raise SystemExit('--engine reactor needs COMFY_URL or --comfy-url')
    comfy = API(args.comfy_url, timeout=30)
    schema = node_schema(comfy, 'ReActorFaceSwapOpt')
    node_schema(comfy, 'ReActorOptions')
    check_choice(schema, 'swap_model', args.swap_model)
    check_choice(schema, 'face_restore_model', args.restore)
    graph_path = out / 'graph.json'
    graph_path.write_text(json.dumps(build_graph(roles, args.swap_model, args.restore), indent=2))
    inputs = [f'clip={trimmed}'] + [f'{role}_face={refs[role]}' for role in roles]
    record = run(args.comfy_url, graph_path, inputs, [], out / 'render', args.wait)
    if record['status'] != 'success':
        print(json.dumps(record, indent=2))
        raise SystemExit('ComfyUI render failed')
    return (Path(next(o['path'] for o in record['outputs'] if o['node'] == 'save')),
            {'engine': 'reactor', 'swap_model': args.swap_model, 'restore': args.restore,
             'prompt_id': record['prompt_id']})


def facefusion_render(args, roles, refs, trimmed, out):
    if not args.ssh_host:
        raise SystemExit('--engine facefusion needs SPARK_SSH or --ssh-host (user@host of the spark worker)')
    if len(roles) != 2:
        raise SystemExit(f'facefusion engine selects faces as leftmost/rightmost; clip has {len(roles)} faces')
    files = [trimmed] + [refs[role] for role in roles]
    digest = hashlib.sha256(b''.join(p.read_bytes() for p in files)).hexdigest()[:12]
    run_id = f'{time.strftime("%Y%m%dT%H%M%S")}-{digest}'
    host_dir = f'{FF_HOST_STORE}/{FF_RUNS}/{run_id}'
    box_dir = f'{FF_CONTAINER_STORE}/{FF_RUNS}/{run_id}'
    names = ['target' + trimmed.suffix] + [f'{role}{refs[role].suffix}' for role in roles]
    staging = f'/tmp/comfy-tools-{run_id}'
    ssh = ['ssh', '-o', 'BatchMode=yes', args.ssh_host]
    sh(*ssh, f'mkdir -p {staging}')
    sh('scp', '-q', str(FF_SCRIPT), *[str(p) for p in files], f'{args.ssh_host}:{staging}/')
    moves = ' && '.join(f'sudo mv {staging}/{shlex.quote(p.name)} {host_dir}/in/{n}' for p, n in zip(files, names))
    sh(*ssh, f'sudo mkdir -p {host_dir}/in && {moves} && sudo mv {staging}/{FF_SCRIPT.name} {host_dir}/ && rmdir {staging}')
    container = ' '.join(shlex.quote(a) for a in [
        'sh', f'{box_dir}/{FF_SCRIPT.name}', f'{box_dir}/in/{names[0]}', f'{box_dir}/in/{names[1]}',
        f'{box_dir}/in/{names[2]}', f'{box_dir}/out', args.ff_model, args.ff_pixel_boost])
    print(f'facefusion run {run_id} on {args.ssh_host}', flush=True)
    sh(*ssh, f'sudo docker run --rm --gpus all -v {FF_HOST_STORE}:{FF_CONTAINER_STORE} -e HOME=/tmp '
             f'{FF_IMAGE} {container}')
    result = out / 'swap.mp4'
    sh('scp', '-q', f'{args.ssh_host}:{host_dir}/out/swap.mp4', str(result))
    return result, {'engine': 'facefusion', 'swap_model': args.ff_model, 'pixel_boost': args.ff_pixel_boost,
                    'run_id': run_id, 'remote_dir': f'{args.ssh_host}:{host_dir}'}


def wan_render(args, roles, refs, trimmed, out, clip):
    """Wan 2.2 Animate replace mode, one pass per person, through meme_swap's swap.py on ComfyUI."""
    if not args.meme_swap_dir:
        raise SystemExit('--engine wan needs MEME_SWAP_DIR or --meme-swap-dir (the meme_swap checkout)')
    swap_py = Path(args.meme_swap_dir).expanduser() / 'swap.py'
    if not swap_py.is_file():
        raise SystemExit(f'--engine wan: {swap_py} not found')
    if not args.comfy_url:
        raise SystemExit('--engine wan needs COMFY_URL or --comfy-url')
    if args.wan_reference is None:
        raise SystemExit('--engine wan needs --wan-reference redraw|photo. redraw sends the clip frame and '
                         'your photo to OpenRouter (needs OPENROUTER_API_KEY); photo keeps them on your network')
    if args.wan_outfit and args.wan_reference != 'redraw':
        raise SystemExit('--wan-outfit changes the redraw prompt, so it needs --wan-reference redraw')
    people = clip.get('people') or {}
    if any(role not in people for role in roles):
        raise SystemExit(f'--engine wan: clips.json needs "people" (who, points) for {roles}')
    # swap.py scales the short side to a multiple of 16 and the long side down to one (prep_clip);
    # --points are pixels of that scaled clip, so the catalog's fractional points are converted here.
    width, height = (int(v) for v in probe(trimmed)['size'].split('x'))
    short = args.wan_short_side // 16 * 16
    if width > height:
        scaled = (int(width * short / height / 16) * 16, short)
    else:
        scaled = (short, int(height * short / width / 16) * 16)
    current, started, effective = trimmed, time.time(), {}
    start, end = args.cut
    for role in roles:
        points = ';'.join(f'{round(x * scaled[0])},{round(y * scaled[1])}' for x, y in people[role]['points'])
        target = out / ('swap.mp4' if role == roles[-1] else f'pass-{role}.mp4')
        cmd = [sys.executable, str(swap_py), '--engine', 'comfy', '--comfy-url', args.comfy_url,
               '--clip', str(current), '--character', str(refs[role]), '--who', people[role]['who'],
               '--points', points, '--short-side', str(args.wan_short_side),
               '--max-seconds', str(math.ceil(probe(trimmed)['duration'])),
               '--comfy-timeout', str(int(args.wait)), '--seed', str(args.seed), '--out', str(target)]
        # A change at or before the cut's start is the state the whole cut shows from its first frame: its
        # note and prompt go to the first reference. One after the cut's end does not apply. The rest switch
        # mid-render; swap.py refuses one that no pass start is near.
        later = []
        for change in people[role].get('reference_changes', []):
            if change['at'] <= start:
                cmd += ['--redraw-note', change['note'], '--prompt', change['prompt']]
                later = []
            elif change['at'] < end:
                later.append({'at_s': change['at'] - start, 'frame_s': change['frame'] - start,
                              'note': change['note'], 'prompt': change['prompt']})
        for change in later:
            if not 0 <= change['frame_s'] < end - start:
                raise SystemExit(f'--engine wan: {role} changes at {change["at_s"] + start:g} s and the frame that '
                                 f'shows the new state, {change["frame_s"] + start:g} s, is outside the cut '
                                 f'{start:g}-{end:g} s; extend --end past it')
        if later:
            changes_file = out / f'reference-changes-{role}.json'
            changes_file.write_text(json.dumps(later, indent=2))
            cmd += ['--reference-changes', str(changes_file)]
            effective[role] = later
        if args.wan_reference == 'photo':
            cmd.append('--no-redraw')
        if args.wan_outfit:
            cmd += ['--redraw-prompt', WAN_OUTFIT_PROMPT.format(who=people[role]['who'])]
        print(f'wan pass: {role} ({people[role]["who"]}), points {points}', flush=True)
        sh(*cmd)
        current = target
    return out / 'swap.mp4', {'engine': 'wan', 'reference': args.wan_reference, 'outfit': args.wan_outfit, 'short_side': args.wan_short_side,
                              'seed': args.seed, 'reference_changes': effective,
                              'scaled_size': f'{scaled[0]}x{scaled[1]}',
                              'render_seconds': round(time.time() - started)}


def finish(args, clip, picture, sound, tracks, out, stem, start, duration):
    """Redraw the captions on picture and give it its sound. tracks is [(name, audio)] from dub.make_audio,
    or empty to keep the sound of `sound`. Returns ([final videos], caption facts)."""
    caption_info = None
    if clip.get('captions'):
        lines = captions.lines_in_cut(clip['captions'], start, duration, args.artist or clip['artist'], clip['artist'])
        captioned = out / f'{stem}-captioned.mp4'
        caption_info = captions.redraw(picture, captioned, clip['captions'], lines)
        picture = captioned
    videos = [audio_dub.mux(picture, track, out / f'{name or stem}.mp4') for name, track in tracks or [('', sound)]]
    return videos, caption_info


def swap(args):
    catalog = json.loads(CATALOG.read_text())
    if args.clip not in catalog:
        raise SystemExit(f'Unknown clip {args.clip!r}; catalog: {", ".join(sorted(catalog))}')
    clip = catalog[args.clip]
    # Only the people a photo was given for are replaced; with no photo the clip's own picture is kept.
    refs = {role: Path(photo) for role, photo in (('male', args.male), ('female', args.female)) if photo}
    faces = clip['faces_left_to_right']
    if any(role not in faces for role in refs):
        raise SystemExit(f'Clip faces {faces} do not include the references given {sorted(refs)}')
    roles = [role for role in faces if role in refs]
    if not roles and not (args.song or args.artist):
        raise SystemExit('Nothing to do: give --male or --female to replace a person, --song or --artist to '
                         'change the sound')
    if roles and len(roles) < len(faces) and args.engine != 'wan':
        raise SystemExit(f'--engine {args.engine} replaces every face in the clip; replacing only {roles} '
                         'needs --engine wan')
    for role, path in refs.items():
        if not path.is_file():
            raise SystemExit(f'--{role} file not found: {path}')

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    start = clip['start'] if args.start is None else args.start
    duration = clip['duration'] if args.duration is None else args.duration
    args.cut = (start, start + duration)
    trimmed = prepare_clip(args.clip, clip, out, start, duration)
    # The sound is built before the render so a song that cannot fit, or a speech job that fails, stops
    # the run in about a minute rather than after the render.
    tracks, audio_info = (audio_dub.make_audio(args, clip, trimmed, out, start, duration)
                          if args.song or args.artist else ([], None))
    renames = bool(args.artist) and args.artist != clip['artist']
    if not roles:
        # The clip's own picture: its captions are intact, so they are redrawn only to name another artist.
        render_file, engine_info = trimmed, {'engine': None}
        videos, caption_info = finish(args, clip if renames else {}, trimmed, trimmed, tracks, out, 'dub',
                                      start, duration)
    else:
        if args.engine == 'wan':
            result, engine_info = wan_render(args, roles, refs, trimmed, out, clip)
        else:
            render = facefusion_render if args.engine == 'facefusion' else reactor_render
            result, engine_info = render(args, roles, refs, trimmed, out)
        # Kept under its own name: `dub --video` takes this file to change song or artist without rendering.
        render_file = result.rename(out / 'swap-render.mp4')
        videos, caption_info = finish(args, clip if args.engine == 'wan' else {}, render_file, render_file,
                                      tracks, out, 'swap', start, duration)
    engine_info.update(audio=audio_info, captions=caption_info, render=str(render_file))

    source_info, result_info = probe(trimmed), probe(videos[0])
    # A swapper that drops frames (ReActor's NSFW filter did) would desync audio; check rather than assume.
    checks = {'frames_match': source_info['frames'] == result_info['frames'],
              'duration_delta_s': round(result_info['duration'] - source_info['duration'], 3),
              'audio': result_info['audio']}
    opened = [str(v) for v in videos]
    if roles:
        contact_sheet(trimmed, videos[0], out / 'contact-sheet.jpg')
        review_sheet(refs, roles, out / 'contact-sheet.jpg', out / 'review.jpg')
        opened.append(str(out / 'review.jpg'))
    report = {'clip': args.clip, 'start': start, 'duration': duration, **engine_info,
              'references': {role: str(refs[role]) for role in roles}, 'source': source_info,
              'result': result_info, 'checks': checks, 'videos': [str(v) for v in videos],
              'review': str(out / 'review.jpg') if roles else None}
    (out / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    if args.open:
        sh('open', *opened)
    # Wan Animate regenerates the clip at its own frame rate, so only the per-frame swappers must keep the count.
    frames_ok = checks['frames_match'] or args.engine == 'wan'
    return 0 if frames_ok and checks['audio'] else 1


def dub(args):
    """Apply --song and --artist to a render made earlier from the same cut of the clip."""
    catalog = json.loads(CATALOG.read_text())
    if args.clip not in catalog:
        raise SystemExit(f'Unknown clip {args.clip!r}; catalog: {", ".join(sorted(catalog))}')
    if not (args.song or args.artist):
        raise SystemExit('dub needs --song, --artist or both')
    clip, video = catalog[args.clip], Path(args.video).expanduser()
    if not video.is_file():
        raise SystemExit(f'--video file not found: {video}')
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    start = clip['start'] if args.start is None else args.start
    duration = probe(video)['duration']
    # The voices come from the original clip, not from --video, whose sound may already be changed.
    trimmed = prepare_clip(args.clip, clip, out, start, duration)
    tracks, audio_info = audio_dub.make_audio(args, clip, trimmed, out, start, duration)
    videos, caption_info = finish(args, clip, video, video, tracks, out, 'dub', start, duration)
    report = {'clip': args.clip, 'start': start, 'duration': duration, 'picture': str(video),
              'audio': audio_info, 'captions': caption_info, 'result': probe(videos[0]),
              'videos': [str(v) for v in videos]}
    (out / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    if args.open:
        sh('open', *[str(v) for v in videos])
    return 0


def add_audio_options(parser):
    a = parser.add_argument_group('song and artist')
    a.add_argument('--song', help='audio file that replaces the music in the clip')
    a.add_argument('--song-start', type=float, default=0.0, help='second of --song where playback begins')
    a.add_argument('--song-level', type=float, default=audio_dub.SONG_LEVEL_DB,
                   help='mean level of the song in dB (default %(default)s); higher is louder')
    a.add_argument('--artist', help="artist the captions and the spoken lines name instead of the clip's own; "
                                    'spoken in the speakers\' voices, one take per seed; mouths are not changed')
    a.add_argument('--take', type=int, help='with --artist: write only this take (a seed) instead of every take')
    s = parser.add_argument_group('where spoken words are regenerated (required with --artist)')
    s.add_argument('--cue-url', default=os.getenv('CUE_URL'), help='Cue API base URL')
    s.add_argument('--cue-submitter', default=os.getenv('CUE_SUBMITTER'), help='Cue submitter identity, KIND:NAME')
    s.add_argument('--speech-stage', help='USER@HOST:/DIR that ssh reaches and the Cue node sees at /DIR; holds '
                                          "host/speech_edit.sh from meme_swap's deploy/host and receives job files")
    s.add_argument('--speech-root', help='directory on the Cue node where speech_edit_install.sh installed F5-TTS')
    s.add_argument('--speech-node', help='node attribute the job is placed by, ATTRIBUTE=VALUE')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('clips', help='List catalogued clips')
    s = commands.add_parser('swap', help='Swap reference faces into a catalogued clip')
    s.add_argument('--clip', required=True)
    s.add_argument('--male', help='photo of the person who replaces the man; without it he is kept')
    s.add_argument('--female', help='photo of the person who replaces the woman; without it she is kept')
    s.add_argument('--out', required=True)
    s.add_argument('--engine', choices=['wan', 'facefusion', 'reactor'], default='wan')
    w = s.add_argument_group('wan engine')
    w.add_argument('--meme-swap-dir', default=os.getenv('MEME_SWAP_DIR'), help='meme_swap checkout (has swap.py)')
    w.add_argument('--wan-reference', choices=['redraw', 'photo'],
                   help='redraw: an OpenRouter image model redraws the first frame with your photo (better '
                        'likeness; sends both images to OpenRouter). photo: use your photo as the reference as-is')
    w.add_argument('--wan-outfit', action='store_true',
                   help="also carry over the clothing in each photo, not only face and hair (redraw only)")
    w.add_argument('--wan-short-side', type=int, default=WAN_SHORT_SIDE)
    w.add_argument('--seed', type=int, default=WAN_SEED, help='sampling seed of every Wan pass (default %(default)s)')
    s.add_argument('--start', type=float, help='Override the catalog start (seconds), e.g. for a probe')
    s.add_argument('--duration', type=float, help='Override the catalog duration (seconds)')
    s.add_argument('--no-open', dest='open', action='store_false', help='Do not open the video and review sheet')
    f = s.add_argument_group('facefusion engine')
    f.add_argument('--ssh-host', default=os.getenv('SPARK_SSH'), help='user@host of the spark worker')
    f.add_argument('--ff-model', default=FF_DEFAULT_MODEL, help='FaceFusion face swapper model name')
    f.add_argument('--ff-pixel-boost', default=FF_DEFAULT_BOOST, help='e.g. 256x256, 512x512, 768x768')
    s.add_argument('--comfy-url', default=os.getenv('COMFY_URL'), help='ComfyUI base URL (wan and reactor engines)')
    r = s.add_argument_group('reactor engine')
    r.add_argument('--swap-model', default=DEFAULT_SWAPPER)
    r.add_argument('--restore', default=DEFAULT_RESTORE, help="'none' or an installed face restore model")
    s.add_argument('--wait', type=float, default=RENDER_WAIT_S, help='Seconds to wait for each ComfyUI render')
    add_audio_options(s)
    d = commands.add_parser('dub', help='Replace the music and the artist name in a video swapped earlier')
    d.add_argument('--clip', required=True)
    d.add_argument('--video', required=True, help='swapped video of the same cut (its picture is kept)')
    d.add_argument('--out', required=True)
    d.add_argument('--start', type=float, help='Clip second the video starts at, if not the catalog start')
    d.add_argument('--comfy-url', default=os.getenv('COMFY_URL'), required=not os.getenv('COMFY_URL'),
                   help='ComfyUI base URL (runs the voice and music separation)')
    d.add_argument('--no-open', dest='open', action='store_false', help='Do not open the result')
    add_audio_options(d)
    args = parser.parse_args(argv)
    if args.command == 'clips':
        print(CATALOG.read_text())
        return 0
    if args.command == 'dub':
        return dub(args)
    return swap(args)


if __name__ == '__main__':
    sys.exit(main())
