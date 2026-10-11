"""Prompt-to-Cue bridge. No broker calls and no local execution."""
import copy
import json
import threading
import time
import uuid
from .artifacts import read_manifest
from .client import APIError, TERMINAL
from .journal import Journal


class Engine:
    def __init__(self, path, cue, spec_factory, output_root, submitter):
        self.journal = Journal(path)
        self.cue, self.spec_factory = cue, spec_factory
        self.output_root, self.submitter = output_root, submitter
        self.lock = threading.RLock()

    def put(self, item):
        if len(item) > 5 and item[5]:
            raise ValueError('Remote credential transport is not configured')
        prompt_id = str(uuid.UUID(item[1]))
        public = copy.deepcopy(list(item[:5]))
        with self.lock:
            existing = self.journal.get(prompt_id)
            if existing:
                if existing['item'][2] != public[2] or existing['item'][4] != public[4]:
                    raise ValueError('Prompt ID already belongs to another graph')
                return existing
            spec = self.spec_factory(prompt_id, public)
            record = {'id': prompt_id, 'item': public, 'spec': spec,
                      'attempted': False, 'cancel_requested': False, 'state': 'pending',
                      'job': None, 'history': None, 'hidden': False, 'error': None}
            self.journal.save(record)
            return record

    def cancel(self, prompt_id):
        with self.lock:
            record = self.journal.get(prompt_id)
            if not record or record['state'] in TERMINAL:
                return False
            record['cancel_requested'] = True
            self.journal.save(record)
            return True

    def finish(self, record, state, message=None, outputs=None):
        record['state'] = state
        record['error'] = message
        record['history'] = {'prompt': record['item'], 'outputs': outputs or {},
            'status': {'status_str': 'success' if state == 'succeeded' else 'error',
                       'completed': state == 'succeeded',
                       'messages': [] if state == 'succeeded' else [
                           ['execution_interrupted' if state == 'cancelled' else 'execution_error',
                            {'prompt_id': record['id'], 'exception_message': message or state}]]}}
        self.journal.save(record)

    def step(self, record):
        if record['state'] in TERMINAL:
            return
        if not record['attempted']:
            if record['cancel_requested']:
                self.finish(record, 'cancelled', 'Cancelled before dispatch')
                return
            # A crash or ambiguous POST must reconcile, never mint another job.
            record['attempted'] = True
            self.journal.save(record)
            try:
                job = self.cue.submit(record['spec'])
            except APIError as exc:
                if exc.status == 400:
                    self.finish(record, 'failed', str(exc))
                    return
                raise
        else:
            job = self.cue.job(record['spec']['id'])
        record['job'] = job
        record['error'] = None
        if job['status'] in TERMINAL:
            if job['status'] == 'succeeded':
                instances = job.get('instances', [])
                latest = max(instances, key=lambda i: i['attempt']) if instances else {}
                if 'result' not in latest and not latest.get('result_error'):
                    # Cue terminal status can precede asynchronous stdout capture.
                    started = record.setdefault('publication_started_at', time.time())
                    if time.time() - started < 60:
                        record['state'] = 'running'
                        record['error'] = 'Waiting for worker manifest capture'
                        self.journal.save(record)
                        return
                try:
                    manifest = read_manifest(self.output_root, job)
                    self.finish(record, 'succeeded', outputs=manifest['outputs'])
                except (OSError, ValueError, KeyError) as exc:
                    self.finish(record, 'failed', 'Output publication failed: ' + str(exc))
            else:
                self.finish(record, job['status'], job.get('reason') or job['status'])
            return
        record['state'] = job['status']
        self.journal.save(record)
        if record['cancel_requested']:
            self.cue.cancel(job['id'], self.submitter)

    def tick(self):
        with self.lock:
            for record in self.journal.all():
                if record['state'] in TERMINAL:
                    continue
                try:
                    self.step(record)
                except (OSError, RuntimeError, ValueError, KeyError) as exc:
                    # Keep active/unknown jobs visible and retain cancel intent.
                    record['error'] = str(exc)
                    self.journal.save(record)

    def records(self):
        return self.journal.all()
