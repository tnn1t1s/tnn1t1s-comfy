"""One invocation, one private ComfyUI process, one immutable result directory."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from .artifacts import confined, digest
from .client import API
from .progress import Capture


def atomic_json(path, value):
    temp = path.with_suffix('.tmp')
    with open(temp, 'w') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_outputs(outputs, root, output, temporary, input_root, attempt):
    root, output, temporary, attempt = (Path(p).resolve() for p in (root, output, temporary, attempt))
    artifacts = []
    def rewrite(value):
        if isinstance(value, dict):
            if 'filename' in value and 'type' in value:
                origin = {'output': output, 'temp': temporary,
                          'input': Path(input_root).resolve()}.get(value['type'])
                if origin is None:
                    raise ValueError('Unsupported output reference type')
                file = confined(origin, str(Path(value.get('subfolder', '')) / value['filename']))
                if value['type'] == 'input':
                    # Upstream loaders publish input references in history too.
                    # Retain a private copy instead of exposing a mutable upload.
                    retained = attempt / 'input' / file.relative_to(origin)
                    retained.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(file, retained)
                    file = retained
                relative = file.relative_to(root)
                with open(file, 'rb') as stream:
                    os.fsync(stream.fileno())
                artifacts.append({'path': str(relative), 'bytes': file.stat().st_size, 'sha256': digest(file)})
                value.update(type='output', subfolder=str(relative.parent), filename=relative.name)
            else:
                for v in value.values(): rewrite(v)
        elif isinstance(value, list):
            for v in value: rewrite(v)
    rewrite(outputs)
    return artifacts


def execute():
    config = json.loads(os.environ['COMFY_WORKER_CONFIG'])
    payload = json.loads(os.environ['COMFY_PAYLOAD'])
    root = Path(config['output_root']).resolve()
    attempt = root / payload['job_id'] / uuid.uuid4().hex
    attempt.mkdir(parents=True)
    # Serialize retry telemetry writers as well as private render processes.
    live_lock = open(attempt.parent / 'live.lock', 'a')
    fcntl.flock(live_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    output, temporary = attempt / 'output', attempt / 'temp'
    output.mkdir(); temporary.mkdir()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    api = API(f'http://127.0.0.1:{port}', timeout=5)
    args = [sys.executable, 'main.py', '--listen', '127.0.0.1', '--port', str(port),
            '--output-directory', str(output), '--temp-directory', str(temporary),
            '--input-directory', config['input_root']] + config.get('args', [])
    child_env = dict(os.environ)
    child_env.pop('COMFY_GATEWAY_CONFIG', None)
    with open(attempt / 'comfy.log', 'w') as log:
        child = subprocess.Popen(args, cwd=config['comfy_dir'], env=child_env,
                                 stdout=log, stderr=log, start_new_session=True)
        def interrupted(signum, frame):
            raise InterruptedError('Worker cancelled')
        signal.signal(signal.SIGTERM, interrupted)
        capture = None
        try:
            deadline = time.monotonic() + config.get('timeout', 600)
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError('ComfyUI exited before readiness; see attempt comfy.log')
                try:
                    api.request('/system_stats')
                    break
                except OSError:
                    time.sleep(.25)
            else:
                raise TimeoutError('ComfyUI startup timed out')
            capture = Capture(f'http://127.0.0.1:{port}/ws?clientId=' + payload['prompt_id'],
                              attempt.parent / 'live.json', payload['prompt_id'])
            capture.start()
            response = api.request('/prompt', {'prompt_id': payload['prompt_id'],
                'prompt': payload['graph'], 'extra_data': payload['extra_data'],
                'client_id': payload['prompt_id'],
                'partial_execution_targets': payload['outputs']})
            if response.get('prompt_id') != payload['prompt_id']:
                raise RuntimeError('ComfyUI did not accept the requested prompt')
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError('ComfyUI exited while rendering')
                history = api.request('/history/' + payload['prompt_id'])
                if payload['prompt_id'] in history:
                    result = history[payload['prompt_id']]
                    if result['status']['status_str'] != 'success':
                        raise RuntimeError('ComfyUI graph failed: ' + json.dumps(result['status']))
                    break
                time.sleep(.25)
            else:
                raise TimeoutError('Render timed out')
            artifacts = publish_outputs(result['outputs'], root, output, temporary, config['input_root'], attempt)
            manifest = {'job_id': payload['job_id'], 'prompt_id': payload['prompt_id'],
                        'outputs': result['outputs'], 'artifacts': artifacts}
            path = attempt / 'manifest.json'
            atomic_json(path, manifest)
            return {'manifest': str(path.relative_to(root)), 'sha256': digest(path)}
        finally:
            if capture is not None:
                capture.close()
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            live_lock.close()


if __name__ == '__main__':
    print(json.dumps(execute()), flush=True)
