"""Small clients for the Cue and ComfyUI HTTP APIs."""
import json
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


class APIError(RuntimeError):
    def __init__(self, status, body):
        self.status, self.body = status, body
        super().__init__(f"HTTP {status}: {body}")


class API:
    def __init__(self, url, timeout=10):
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.netloc:
            raise ValueError('Specify an http(s) service URL')
        self.url, self.timeout = url.rstrip('/'), timeout

    def request(self, path, body=None, method=None):
        data = None if body is None else json.dumps(body).encode()
        req = Request(self.url + path, data=data, method=method,
                      headers={'Content-Type': 'application/json'})
        try:
            with urlopen(req, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            raw = exc.read().decode(errors='replace')
            try:
                raw = json.loads(raw)
            except ValueError:
                pass
            raise APIError(exc.code, raw) from exc
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode(errors='replace')


TERMINAL = {'succeeded', 'failed', 'cancelled'}


class Cue(API):
    def job(self, job_id):
        return self.request('/jobs/' + quote(job_id, safe=''))

    def submit(self, spec):
        """On a duplicate ID, require the submitted fields to match the old job."""
        try:
            return self.request('/jobs', spec)
        except APIError as exc:
            if exc.status != 409:
                raise
            existing = self.job(spec['id'])
            if any(existing.get(key) != value for key, value in spec.items()):
                raise ValueError('Cue job ID already belongs to a different specification') from exc
            return existing

    def cancel(self, job_id, submitter):
        if ':' not in submitter or not all(submitter.split(':', 1)):
            raise ValueError('Cancellation requires the caller identity, e.g. team:comfy')
        try:
            return self.request('/jobs/' + quote(job_id, safe=''),
                                {'submitter': submitter}, 'DELETE')
        except APIError as exc:
            if exc.status != 409:
                raise
            current = self.job(job_id)
            if current['status'] not in TERMINAL:
                raise
            return current

    def wait(self, job_id, timeout=60, poll=1):
        deadline = time.monotonic() + timeout
        while True:
            current = self.job(job_id)
            if current['status'] in TERMINAL:
                return current
            if time.monotonic() >= deadline:
                raise TimeoutError('Job has not reached a terminal state; it may still be running')
            time.sleep(min(poll, max(0, deadline - time.monotonic())))


def explain(job):
    """Report what Cue says; missing diagnostics must not become invented causes."""
    state = job['status']
    reason = job.get('reason')
    if state == 'pending' and not reason:
        reason = 'Cue has not supplied a waiting reason; capacity or placement is not yet established.'
    instances = job.get('instances', [])
    latest = max(instances, key=lambda item: item['attempt']) if instances else {}
    return {'id': job['id'], 'status': state,
            'reason_code': job.get('reason_code') or latest.get('reason_code'),
            'reason': reason or latest.get('reason'),
            'cancelled_by': job.get('cancelled_by'),
            'attempts': len(instances), 'latest_attempt': latest.get('attempt'),
            'requested': {key: job.get(key) for key in ('cpus', 'mem', 'gpus', 'placement', 'data')}}


def doctor(comfy_url, cue_url=None, required_nodes=()):
    checks = []
    api = API(comfy_url)
    for name, path in (('ComfyUI API', '/system_stats'), ('ComfyUI queue', '/queue')):
        try:
            result = api.request(path)
            if not isinstance(result, dict):
                raise ValueError('Expected a JSON object')
            if path == '/queue':
                detail = {'running': len(result['queue_running']), 'pending': len(result['queue_pending'])}
            else:
                detail = 'Responsive; this does not test model availability or a CUDA render'
            checks.append({'check': name, 'ok': True, 'detail': detail})
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
            checks.append({'check': name, 'ok': False, 'detail': str(exc)})
    if required_nodes:
        try:
            info = api.request('/object_info')
            missing = sorted(set(required_nodes) - set(info))
            checks.append({'check': 'Required nodes', 'ok': not missing, 'missing': missing})
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            checks.append({'check': 'Required nodes', 'ok': False, 'detail': str(exc)})
    if cue_url:
        try:
            Cue(cue_url).request('/healthz')
            checks.append({'check': 'Cue API', 'ok': True})
        except (OSError, RuntimeError, ValueError) as exc:
            checks.append({'check': 'Cue API', 'ok': False, 'detail': str(exc)})
    return {'ok': all(c['ok'] for c in checks), 'checks': checks}
