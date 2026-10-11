"""Pinned ComfyUI queue adapter, installed by gateway.py."""
import copy
import json
import os
import threading
import time
from pathlib import Path
from aiohttp import web
import execution
import server
from tnn1t1s_comfy.client import Cue, TERMINAL
from tnn1t1s_comfy.engine import Engine

config = json.loads(os.environ['COMFY_GATEWAY_CONFIG'])


def spec_factory(prompt_id, item):
    spec = copy.deepcopy(config['worker_spec'])
    spec.update(id='comfy-' + prompt_id, retries=0, result_max_bytes=4096,
                submitter=config['submitter'])
    spec.setdefault('env', {}).update(COMFY_WORKER_CONFIG=json.dumps(config['worker_config']),
        COMFY_PAYLOAD=json.dumps({'job_id': spec['id'], 'prompt_id': prompt_id,
                                  'graph': item[2], 'extra_data': item[3], 'outputs': item[4]}))
    return spec


engine = Engine(config['journal'], Cue(config['cue_url']), spec_factory,
                config['worker_config']['output_root'], config['submitter'])


class RemoteQueue(execution.PromptQueue):
    def __init__(self, instance):
        super().__init__(instance)
        self.refresh()
        threading.Thread(target=self.watch, daemon=True).start()

    def refresh(self):
        records = engine.records()
        with self.mutex:
            self.queue = [r['item'] for r in records if r['state'] == 'pending']
            self.currently_running = {r['id']: r['item'] for r in records if r['state'] == 'running'}
            self.history = {r['id']: r['history'] for r in records if r['history'] and not r['hidden']}

    def watch(self):
        prior = {}
        while True:
            engine.tick()
            self.refresh()
            for record in engine.records():
                state = record['state']
                if prior.get(record['id']) != state:
                    client = record['item'][3].get('client_id')
                    if state == 'running':
                        self.server.send_sync('execution_start', {'prompt_id': record['id']}, client)
                    if state in TERMINAL:
                        if state != 'succeeded':
                            self.server.send_sync('execution_interrupted' if state == 'cancelled' else 'execution_error',
                                {'prompt_id': record['id'], 'exception_message': record['error']}, client)
                        self.server.send_sync('executing', {'node': None, 'prompt_id': record['id']}, client)
                    prior[record['id']] = state
                    self.server.queue_updated()
            time.sleep(.5)

    def get(self, timeout=None):
        time.sleep(.25)
        return None  # The local execution thread must never consume a prompt.

    def put(self, item):
        engine.put(item)
        self.refresh()
        self.server.queue_updated()

    def interrupt_if_running(self, prompt_id):
        return engine.cancel(prompt_id)

    def delete_queue_item(self, predicate):
        for record in engine.records():
            if record['state'] == 'pending' and predicate(record['item']):
                return engine.cancel(record['id'])
        return False

    def wipe_queue(self):
        for record in engine.records():
            if record['state'] == 'pending': engine.cancel(record['id'])

    def delete_history_item(self, prompt_id):
        with engine.lock:
            record = engine.journal.get(prompt_id)
            if record and record['state'] in TERMINAL:
                record['hidden'] = True
                engine.journal.save(record)
        self.refresh()

    def wipe_history(self):
        for record in engine.records(): self.delete_history_item(record['id'])


@web.middleware
async def remote_routes(request, handler):
    path = request.path.removeprefix('/api')
    if request.method == 'POST' and path == '/prompt':
        body = await request.json()
        if any(k in body.get('extra_data', {}) for k in execution.SENSITIVE_EXTRA_DATA_KEYS):
            return web.json_response({'error': 'Remote credential transport is not configured'}, status=400)
        try:
            return await handler(request)
        except ValueError as exc:
            return web.json_response({'error': str(exc)}, status=409)
    if request.method == 'POST' and path == '/interrupt':
        try: body = await request.json()
        except (ValueError, TypeError): body = {}
        ids = [body['prompt_id']] if body.get('prompt_id') else [r['id'] for r in engine.records() if r['state'] == 'running']
        accepted = False
        for pid in ids: accepted = engine.cancel(pid) or accepted
        return web.json_response({'cancel_requested': accepted})
    if request.method == 'GET' and path == '/cue/status':
        return web.json_response([{'prompt_id': r['id'], 'job_id': r['spec']['id'],
            'state': r['state'], 'cancel_requested': r['cancel_requested'], 'error': r['error'],
            'attempts': r['job'].get('instances', []) if r['job'] else []} for r in engine.records()])
    return await handler(request)


instance = server.PromptServer.instance
instance.app.middlewares.append(remote_routes)
instance.prompt_queue = RemoteQueue(instance)
NODE_CLASS_MAPPINGS = {}
