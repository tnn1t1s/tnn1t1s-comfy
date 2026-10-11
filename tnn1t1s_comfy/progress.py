"""Bounded, replaceable live telemetry; never evidence of task completion."""
import asyncio
import base64
import json
import os
from pathlib import Path
import threading
import uuid

MAX_FRAME = 4 * 1024 * 1024
EVENTS = ('execution_cached', 'executing', 'progress', 'progress_state')


class Snapshot:
    def __init__(self, path, prompt_id):
        self.path = Path(path)
        self.value = {'prompt_id': prompt_id, 'generation': uuid.uuid4().hex,
                      'sequence': 0, 'events': {}, 'preview': None}
        self.save()

    def save(self):
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.value))
        os.replace(temporary, self.path)

    def accept(self, message):
        if isinstance(message, bytes):
            if not 4 < len(message) <= MAX_FRAME:
                return False
            # Comfy's already encoded binary websocket frame, including type.
            self.value['preview'] = base64.b64encode(message).decode('ascii')
        else:
            event, data = message.get('type'), message.get('data')
            if event not in EVENTS or not isinstance(data, dict):
                return False
            if data.get('prompt_id', self.value['prompt_id']) != self.value['prompt_id']:
                return False
            if event == 'executing':
                if data.get('node') is None:
                    return False  # Only Cue + manifest may announce completion.
                self.value['events'].pop('progress', None)
                self.value['preview'] = None
            self.value['events'][event] = dict(data, prompt_id=self.value['prompt_id'])
        self.value['sequence'] += 1
        self.save()
        return True


class Capture:
    """Private worker websocket; latest state survives gateway disconnection."""
    def __init__(self, url, path, prompt_id):
        self.url, self.snapshot = url, Snapshot(path, prompt_id)
        self.ready, self.stop = threading.Event(), threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        try:
            asyncio.run(self.listen())
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.ready.set()

    async def listen(self):
        from aiohttp import ClientSession, WSMsgType
        async with ClientSession() as session:
            async with session.ws_connect(self.url, max_msg_size=MAX_FRAME, heartbeat=20) as ws:
                await ws.send_json({'type': 'feature_flags',
                                    'data': {'supports_preview_metadata': True}})
                while not self.stop.is_set():
                    try:
                        msg = await ws.receive(timeout=.25)
                    except asyncio.TimeoutError:
                        continue
                    if msg.type == WSMsgType.TEXT:
                        value = json.loads(msg.data)
                        if value.get('type') == 'feature_flags':
                            self.ready.set()
                        else:
                            self.snapshot.accept(value)
                    elif msg.type == WSMsgType.BINARY:
                        self.snapshot.accept(msg.data)
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                        break

    def start(self):
        self.thread.start()
        if not self.ready.wait(10) or self.error:
            raise RuntimeError('Progress websocket did not connect: ' + str(self.error))

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)


class Relay:
    """Send latest node/progress/preview to the owner, including new sockets."""
    def __init__(self):
        self.sent = {}

    async def publish(self, instance, records, root):
        active = set()
        for record in records:
            if record['state'] != 'running':
                continue
            client = record['item'][3].get('client_id')
            # Never broadcast one client's previews to another client.
            ws = instance.sockets.get(client) if client else None
            if ws is None or ws.closed:
                continue
            key = (record['id'], id(ws))
            active.add(key)
            path = Path(root) / record['spec']['id'] / 'live.json'
            try:
                if path.stat().st_size > MAX_FRAME * 2:
                    continue
                value = json.loads(path.read_text())
                if value['prompt_id'] != record['id']:
                    continue
                version = (value['generation'], value['sequence'])
                if self.sent.get(key) == version:
                    continue
                for event in EVENTS:
                    if event in value['events']:
                        await ws.send_json({'type': event, 'data': value['events'][event]})
                if value.get('preview'):
                    await ws.send_bytes(base64.b64decode(value['preview'], validate=True))
                self.sent[key] = version
            except (OSError, ValueError, KeyError, ConnectionError):
                continue
        self.sent = {key: value for key, value in self.sent.items() if key in active}
