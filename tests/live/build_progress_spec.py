#!/usr/bin/env python3
"""Add the CPU-only progress fixture to an explicit isolated deployment profile."""
import argparse
import base64
import copy
import json
from pathlib import Path
import shlex
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'deploy'))
from build_spec import build


def fixture_spec(profile):
    profile = copy.deepcopy(profile)
    config = profile['gateway_config']
    if config['worker_spec'].get('gpus', 0) != 0:
        raise ValueError('Progress fixture must use a zero-GPU worker')
    for section in (config, config['worker_config']):
        args = section.setdefault('args', [])
        if '--disable-all-custom-nodes' not in args:
            args.append('--disable-all-custom-nodes')
        if '--whitelist-custom-nodes' not in args:
            args.extend(['--whitelist-custom-nodes', 'cue_relay_probe'])
        else:
            args.insert(args.index('--whitelist-custom-nodes') + 1, 'cue_relay_probe')
    if 'tnn1t1s_cue_gateway' not in config['args']:
        config['args'].insert(config['args'].index('--whitelist-custom-nodes') + 1, 'tnn1t1s_cue_gateway')
    if '--cpu' not in config['worker_config']['args']:
        config['worker_config']['args'].insert(0, '--cpu')
    app = build(profile)
    fixture = base64.b64encode(Path(__file__).with_name('progress_node.py').read_bytes()).decode()
    def prefix(comfy_dir):
        directory = str(Path(comfy_dir) / 'custom_nodes' / 'cue_relay_probe')
        setup = ('import base64,pathlib;p=pathlib.Path(' + repr(directory) +
                 ');p.mkdir(exist_ok=True);(p/"__init__.py").write_bytes(base64.b64decode(' + repr(fixture) + '))')
        return shlex.quote(profile.get('python', '/opt/venv/bin/python')) + ' -c ' + shlex.quote(setup) + ' && '
    app['cmd'] = prefix(config['comfy_dir']) + app['cmd']
    config = json.loads(app['env']['COMFY_GATEWAY_CONFIG'])
    config['worker_spec']['command'] = prefix(config['worker_config']['comfy_dir']) + config['worker_spec']['command']
    app['env']['COMFY_GATEWAY_CONFIG'] = json.dumps(config)
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile'); parser.add_argument('--output', required=True)
    args = parser.parse_args()
    Path(args.output).write_text(json.dumps(fixture_spec(json.loads(Path(args.profile).read_text())), indent=2)+'\n')
