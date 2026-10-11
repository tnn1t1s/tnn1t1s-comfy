import json
from pathlib import Path
import tempfile
import unittest
import uuid
from tnn1t1s_comfy.artifacts import digest, read_manifest
from tnn1t1s_comfy.client import APIError
from tnn1t1s_comfy.engine import Engine


class Backend:
    def __init__(self):
        self.jobs = {}; self.posts = 0; self.cancels = []; self.drop_response = False
    def submit(self, spec):
        self.posts += 1
        self.jobs[spec['id']] = {**spec, 'status': 'running', 'instances': []}
        if self.drop_response: raise OSError('Response lost after accept')
        return self.jobs[spec['id']]
    def job(self, key):
        if key not in self.jobs: raise APIError(404, 'unknown')
        return self.jobs[key]
    def cancel(self, key, submitter):
        self.cancels.append((key, submitter))
        return self.jobs[key]


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.backend = Backend(); self.pid = str(uuid.uuid4()); self.engines = []
        self.item = [0, self.pid, {'1': {'class_type': 'EmptyImage'}}, {}, ['1'], {}]
        self.engine = self.open()
    def tearDown(self):
        for e in self.engines: e.journal.close()
        self.tmp.cleanup()
    def open(self):
        e = Engine(self.root/'journal.sqlite', self.backend,
                   lambda pid, item: {'id': 'comfy-'+pid, 'command': 'render'}, self.root, 'team:test')
        self.engines.append(e); return e
    def test_ambiguous_post_then_restart_uses_same_job(self):
        self.backend.drop_response = True
        self.engine.put(self.item); self.engine.tick()
        restarted = self.open(); restarted.tick()
        self.assertEqual(self.backend.posts, 1)
        self.assertEqual(restarted.records()[0]['state'], 'running')
    def test_cancel_intent_survives_restart_and_requires_terminal(self):
        self.engine.put(self.item); self.engine.tick(); self.engine.cancel(self.pid)
        restarted = self.open(); restarted.tick()
        self.assertEqual(self.backend.cancels, [('comfy-'+self.pid, 'team:test')])
        self.assertEqual(restarted.records()[0]['state'], 'running')
        self.backend.jobs['comfy-'+self.pid]['status'] = 'cancelled'
        restarted.tick(); self.assertEqual(restarted.records()[0]['state'], 'cancelled')
    def test_cancel_before_dispatch_never_submits(self):
        self.engine.put(self.item); self.engine.cancel(self.pid); self.engine.tick()
        self.assertEqual(self.backend.posts, 0)
        self.assertEqual(self.engine.records()[0]['state'], 'cancelled')
    def test_unknown_previously_attempted_job_is_not_relaunched(self):
        self.engine.put(self.item); self.engine.tick(); self.backend.jobs.clear()
        self.open().tick()
        self.assertEqual(self.backend.posts, 1)
        self.assertIn('404', self.engine.records()[0]['error'])
    def test_conflicting_prompt_id_rejected(self):
        self.engine.put(self.item)
        self.item[2] = {'different': 'graph'}
        with self.assertRaises(ValueError): self.engine.put(self.item)
    def test_secret_not_persisted(self):
        self.item[5] = {'token': 'private'}
        with self.assertRaises(ValueError): self.engine.put(self.item)
        self.assertEqual(self.engine.records(), [])
    def test_success_requires_verified_publication(self):
        self.engine.put(self.item); self.engine.tick()
        job = self.backend.jobs['comfy-'+self.pid]
        image = self.root/'image.png'; image.write_bytes(b'png')
        manifest = self.root/'manifest.json'
        manifest.write_text(json.dumps({'job_id': job['id'], 'outputs': {'1': {}},
           'artifacts': [{'path': 'image.png', 'bytes': 3, 'sha256': digest(image)}]}))
        job.update(status='succeeded', instances=[{'attempt': 0, 'result': json.dumps(
            {'manifest': 'manifest.json', 'sha256': digest(manifest)})}])
        image.write_bytes(b'bad')
        self.engine.tick()
        self.assertEqual(self.engine.records()[0]['state'], 'failed')
        self.assertIn('verification', self.engine.records()[0]['error'])
    def test_terminal_status_before_stdout_capture_waits(self):
        self.engine.put(self.item); self.engine.tick()
        job = self.backend.jobs['comfy-'+self.pid]
        job.update(status='succeeded', instances=[{'attempt': 0}])
        self.engine.tick()
        record = self.engine.records()[0]
        self.assertEqual(record['state'], 'running')
        self.assertIsNone(record['history'])
        restarted = self.open(); restarted.tick()
        self.assertEqual(restarted.records()[0]['publication_started_at'], record['publication_started_at'])

    def test_newest_attempt_selects_manifest(self):
        job = {'id': 'job', 'instances': [{'attempt': 0, 'result': '{}'},
                                        {'attempt': 1, 'result_truncated': True}]}
        with self.assertRaisesRegex(ValueError, 'incomplete'): read_manifest(self.root, job)


if __name__ == '__main__': unittest.main()
