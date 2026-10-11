#!/usr/bin/env python3
"""Generate an explicit Fennel app spec from a caller-owned deployment profile."""
import argparse
import copy
import json
from pathlib import Path
from bundle import command


def build(profile):
    app = copy.deepcopy(profile['gateway_app'])
    config = copy.deepcopy(profile['gateway_config'])
    if app.get('gpus', 0) != 0:
        raise ValueError('Gateway must request zero GPUs')
    if '@sha256:' not in app['container']['image'] or '@sha256:' not in config['worker_spec']['image']:
        raise ValueError('Gateway and worker images must be pinned by digest')
    app['cmd'], revision = command('tnn1t1s_comfy.gateway', profile.get('python', '/opt/venv/bin/python'))
    config['worker_spec']['command'], _ = command('tnn1t1s_comfy.worker', profile.get('python', '/opt/venv/bin/python'))
    app.setdefault('env', {})['COMFY_GATEWAY_CONFIG'] = json.dumps(config)
    app.setdefault('labels', {})['source.sha256'] = revision
    # Stay below Linux's per-argument/environment-string exec limit.
    if len(app['cmd'].encode()) > 65536 or any(
            len((key + '=' + str(value)).encode()) > 65536
            for key, value in app['env'].items()):
        raise ValueError('Deployment command/environment exceeds 64 KiB; move large data to artifacts')
    return app


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('profile'); p.add_argument('--output', required=True)
    args = p.parse_args()
    Path(args.output).write_text(json.dumps(build(json.loads(Path(args.profile).read_text())), indent=2)+'\n')
