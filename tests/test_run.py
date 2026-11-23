import hashlib
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from tnn1t1s_comfy import cli
from tnn1t1s_comfy.run import load_graph, parse_binding, run

GRAPH = {
    'face': {'class_type': 'LoadImage', 'inputs': {'image': 'placeholder.png'}},
    'clip': {'class_type': 'LoadVideo', 'inputs': {'file': 'placeholder.mp4'}},
    'custom': {'class_type': 'SomePackLoader', 'inputs': {'path': 'x', 'other': 1}},
    'prompt': {'class_type': 'CLIPTextEncode', 'inputs': {'text': 'old', 'clip': ['c', 0]}},
    'save': {'class_type': 'SaveImage', 'inputs': {'images': ['face', 0]}},
}


class FakeComfy(BaseHTTPRequestHandler):
    state = None

    def log_message(self, *args): pass

    def reply(self, body, code=200):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        if self.path == '/upload/image':
            message = BytesParser(policy=default).parsebytes(
                b'Content-Type: ' + self.headers['Content-Type'].encode() + b'\r\n\r\n' + body)
            fields = {p.get_param('name', header='content-disposition'): p for p in message.iter_parts()}
            image = fields['image']
            name = image.get_filename()
            self.state['uploads'][name] = image.get_payload(decode=True)
            self.state['upload_fields'] = {k: v.get_content() for k, v in fields.items() if k != 'image'}
            return self.reply({'name': self.state.get('rename', name), 'subfolder': '', 'type': 'input'})
        if self.path == '/prompt':
            self.state['prompt'] = json.loads(body)['prompt']
            return self.reply({'prompt_id': 'p-1', 'number': 0})
        self.reply({'error': 'no route'}, 404)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == '/history/p-1':
            self.state['polls'] += 1
            if self.state['polls'] < 2 or self.state.get('never'):
                return self.reply({})
            return self.reply({'p-1': {'status': self.state['status'], 'outputs': {
                'save': {'images': [{'filename': 'out_0001.png', 'subfolder': 'faces', 'type': 'output'},
                                    {'filename': 'preview.png', 'subfolder': '', 'type': 'temp'}]}}}})
        if url.path == '/view':
            self.state['views'].append(parse_qs(url.query))
            return self.reply(b'PNGDATA')
        self.reply({'error': 'no route'}, 404)


class RunCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.graph = self.tmp / 'graph.json'
        self.graph.write_text(json.dumps({'prompt': GRAPH}))
        self.face = self.tmp / 'Face.PNG'
        self.face.write_bytes(b'face-bytes')
        FakeComfy.state = {'uploads': {}, 'views': [], 'polls': 0,
                           'status': {'completed': True, 'status_str': 'success', 'messages': []}}
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeComfy)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_upload_bind_submit_and_download(self):
        out = self.tmp / 'out'
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = cli.main(['run', str(self.graph), '--comfy-url', self.url, '--input', f'face={self.face}',
                             '--set', 'prompt.text=a portrait', '--out', str(out), '--wait', '10'])
        self.assertEqual(code, 0)
        digest = hashlib.sha256(b'face-bytes').hexdigest()
        name = f'cli-{digest[:16]}.png'
        self.assertEqual(FakeComfy.state['uploads'], {name: b'face-bytes'})
        self.assertEqual(FakeComfy.state['upload_fields'], {'type': 'input', 'overwrite': 'true'})
        sent = FakeComfy.state['prompt']
        self.assertEqual(sent['face']['inputs']['image'], name)
        self.assertEqual(sent['prompt']['inputs'], {'text': 'a portrait', 'clip': ['c', 0]})
        self.assertEqual(sent['clip'], GRAPH['clip'])
        # Only the output-type file is downloaded; the temp preview is not a result.
        self.assertEqual(FakeComfy.state['views'],
                         [{'filename': ['out_0001.png'], 'subfolder': ['faces'], 'type': ['output']}])
        self.assertEqual((out / 'save' / 'out_0001.png').read_bytes(), b'PNGDATA')
        record = json.loads((out / 'run.json').read_text())
        self.assertEqual(record['status'], 'success')
        self.assertEqual(record['inputs'][0]['sha256'], digest)
        self.assertEqual(record['outputs'][0]['sha256'], hashlib.sha256(b'PNGDATA').hexdigest())
        self.assertEqual(json.loads(stdout.getvalue())['prompt_id'], 'p-1')

    def test_error_history_exits_nonzero_with_messages(self):
        FakeComfy.state['status'] = {'completed': False, 'status_str': 'error',
                                     'messages': [['execution_error', {'exception_message': 'boom'}]]}
        with redirect_stdout(io.StringIO()):
            code = cli.main(['run', str(self.graph), '--comfy-url', self.url,
                             '--out', str(self.tmp / 'o'), '--wait', '10'])
        self.assertEqual(code, 1)
        record = json.loads((self.tmp / 'o' / 'run.json').read_text())
        self.assertEqual(record['status'], 'error')
        self.assertIn('boom', json.dumps(record['messages']))

    def test_timeout_keeps_prompt_id_and_says_it_may_run(self):
        FakeComfy.state['never'] = True
        with self.assertRaisesRegex(TimeoutError, 'may still be running'):
            run(self.url, self.graph, [], [], self.tmp / 't', wait=0.2, poll=0.05)
        record = json.loads((self.tmp / 't' / 'run.json').read_text())
        self.assertEqual((record['prompt_id'], record['status']), ('p-1', 'timeout'))

    def test_server_rename_is_refused(self):
        FakeComfy.state['rename'] = 'something (1).png'
        with self.assertRaisesRegex(RuntimeError, 'expected'):
            run(self.url, self.graph, [f'face={self.face}'], [], self.tmp / 'r', wait=1)
        self.assertNotIn('prompt', FakeComfy.state)


class Bindings(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.graph = self.tmp / 'g.json'
        self.graph.write_text(json.dumps(GRAPH))
        self.file = self.tmp / 'a.png'
        self.file.write_bytes(b'x')

    def check(self, inputs=(), sets=(), pattern=''):
        # Validation must fail before any network call; the URL is unroutable on purpose.
        with self.assertRaisesRegex(ValueError, pattern):
            run('http://invalid.test', self.graph, list(inputs), list(sets), self.tmp / 'o', wait=1)

    def test_parse(self):
        self.assertEqual(parse_binding('face=/a=b.png'), ('face', None, '/a=b.png'))
        self.assertEqual(parse_binding('custom.path=x'), ('custom', 'path', 'x'))
        with self.assertRaises(ValueError):
            parse_binding('face')

    def test_rejections_happen_before_upload(self):
        self.check([f'nope={self.file}'], pattern='not in the graph')
        self.check([f'custom={self.file}'], pattern='name its file field')
        self.check([f'face.missing={self.file}'], pattern='no input')
        self.check(['face=/does/not/exist.png'], pattern='not found')
        self.check(sets=['prompt.clip=x'], pattern='link from another node')
        self.check(sets=['prompt=x'], pattern='needs NODE.FIELD')

    def test_ui_format_graph_is_rejected(self):
        self.graph.write_text(json.dumps({'nodes': [], 'links': []}))
        with self.assertRaisesRegex(ValueError, 'API format'):
            load_graph(self.graph)


if __name__ == '__main__':
    unittest.main()
