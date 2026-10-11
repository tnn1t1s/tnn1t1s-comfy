#!/usr/bin/env python3
"""Bounded CPU websocket relay/reconnect acceptance; requires progress_node.py."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time
import uuid
from aiohttp import ClientSession, ClientTimeout, WSMsgType


async def run(args):
    prompt = str(uuid.uuid4()); client = uuid.uuid4().hex
    evidence = {'prompt_id': prompt, 'client_id': client, 'events': [], 'previews': []}
    print(prompt, flush=True)
    terminal = False
    async with ClientSession(timeout=ClientTimeout(total=10)) as session:
        async def request(path, body=None):
            async with session.request('GET' if body is None else 'POST', args.url + path, json=body) as r:
                r.raise_for_status()
                return await r.json()
        async def observe(ws):
            deadline = time.monotonic() + 90
            progress = preview = False
            while time.monotonic() < deadline:
                msg = await ws.receive(timeout=30)
                if msg.type == WSMsgType.TEXT:
                    value = json.loads(msg.data)
                    evidence['events'].append(value)
                    progress |= value['type'] == 'progress'
                elif msg.type == WSMsgType.BINARY:
                    evidence['previews'].append({'bytes': len(msg.data), 'sha256': hashlib.sha256(msg.data).hexdigest()})
                    preview = True
                elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                    raise RuntimeError('Websocket closed before progress')
                if progress and preview:
                    return
            raise TimeoutError('No progress and preview')
        try:
            async with session.ws_connect(args.url + '/ws?clientId=' + client) as ws:
                await request('/prompt', {'prompt_id': prompt, 'client_id': client, 'prompt': {
                    '1': {'class_type': 'EmptyImage', 'inputs': {'width': 32, 'height': 32, 'batch_size': 1, 'color': 0}},
                    '2': {'class_type': 'CueRelayProbe', 'inputs': {'image': ['1', 0]}},
                    '3': {'class_type': 'SaveImage', 'inputs': {'images': ['2', 0], 'filename_prefix': 'relay-acceptance'}}}})
                await observe(ws)
            evidence['reconnect_after_events'] = len(evidence['events'])
            async with session.ws_connect(args.url + '/ws?clientId=' + client) as ws:
                await observe(ws)
                evidence['reconnect_pass'] = True
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    history = await request('/history/' + prompt)
                    if prompt in history:
                        terminal = True
                        evidence['history'] = history[prompt]
                        assert history[prompt]['status']['status_str'] == 'success', history
                        break
                    await asyncio.sleep(.5)
                else:
                    raise TimeoutError('No terminal history')
        finally:
            if not terminal:
                await request('/interrupt', {'prompt_id': prompt})
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    history = await request('/history/' + prompt)
                    if prompt in history:
                        evidence['cleanup_history'] = history[prompt]; break
                    await asyncio.sleep(1)
                else:
                    evidence['cleanup_unconfirmed'] = True
            Path(args.evidence).write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps({'prompt_id': prompt, 'reconnect': evidence.get('reconnect_pass'),
                      'previews': len(evidence['previews']), 'terminal': terminal}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True); parser.add_argument('--evidence', required=True)
    asyncio.run(run(parser.parse_args()))
