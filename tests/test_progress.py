import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from tnn1t1s_comfy.progress import Snapshot, Relay, MAX_FRAME


class Socket:
    closed = False
    def __init__(self): self.messages = []
    async def send_json(self, value): self.messages.append(value)
    async def send_bytes(self, value): self.messages.append(value)


class ProgressTests(unittest.TestCase):
    def test_telemetry_cannot_finish_or_cross_prompt(self):
        with tempfile.TemporaryDirectory() as root:
            snapshot = Snapshot(Path(root) / 'live.json', 'prompt')
            for message in [
                {'type': 'executing', 'data': {'node': None}},
                {'type': 'execution_success', 'data': {}},
                {'type': 'progress', 'data': {'prompt_id': 'other', 'value': 1}},
                b'x' * (MAX_FRAME + 1),
            ]:
                self.assertFalse(snapshot.accept(message))
            self.assertEqual(snapshot.value['sequence'], 0)

    def test_reconnect_replays_latest_only_to_owner(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'job'; path.mkdir()
            snapshot = Snapshot(path / 'live.json', 'prompt')
            snapshot.accept({'type': 'executing', 'data': {'node': '9'}})
            snapshot.accept({'type': 'progress', 'data': {'value': 3, 'max': 10}})
            preview = b'\x00\x00\x00\x01\x00\x00\x00\x02png'
            snapshot.accept(preview)
            record = {'id': 'prompt', 'state': 'running', 'spec': {'id': 'job'},
                      'item': [0, 'prompt', {}, {'client_id': 'owner'}]}
            owner, other = Socket(), Socket()
            server = SimpleNamespace(sockets={'owner': owner, 'other': other})
            relay = Relay()
            asyncio.run(relay.publish(server, [record], root))
            self.assertEqual(owner.messages[-1], preview)
            self.assertEqual(other.messages, [])
            count = len(owner.messages)
            asyncio.run(relay.publish(server, [record], root))
            self.assertEqual(len(owner.messages), count)
            replacement = Socket(); server.sockets['owner'] = replacement
            asyncio.run(relay.publish(server, [record], root))
            self.assertEqual(replacement.messages, owner.messages)
            record['state'] = 'succeeded'
            asyncio.run(relay.publish(server, [record], root))
            self.assertEqual(relay.sent, {})

    def test_node_change_discards_old_preview_and_progress(self):
        with tempfile.TemporaryDirectory() as root:
            snapshot = Snapshot(Path(root) / 'live.json', 'prompt')
            snapshot.accept({'type': 'progress', 'data': {'value': 7}})
            snapshot.accept(b'12345')
            snapshot.accept({'type': 'executing', 'data': {'node': 'next'}})
            self.assertIsNone(snapshot.value['preview'])
            self.assertNotIn('progress', snapshot.value['events'])
            self.assertEqual(json.loads(snapshot.path.read_text()), snapshot.value)
