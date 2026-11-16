"""Clients for the ComfyUI and Cue HTTP APIs.

``API`` sends JSON requests to one base URL and raises ``APIError`` with the
status code and decoded body when the server answers with an HTTP error.

``Cue`` adds the job calls a ComfyUI tool needs: read a job, submit one, cancel
one on behalf of a team, and wait for a job to finish. Submitting a job ID that
already exists returns the existing job only when its fields match the new
specification, so a retried submission cannot attach to someone else's job.

``doctor`` checks that a ComfyUI server answers, reports its queue, and
optionally confirms that given node classes are installed. ``explain`` turns a
Cue job record into a short summary of its state and the reason Cue gives.

Callers: the ``tnn1t1s-comfy`` command in ``cli.py``, and tools that drive a
ComfyUI server or run Cue jobs directly. Uses only the standard library.
"""
import json
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


class APIError(RuntimeError):
    """An HTTP error response; ``status`` is the code and ``body`` the decoded reply."""

    def __init__(self, status, body):
        self.status, self.body = status, body
        super().__init__(f"HTTP {status}: {body}")


class API:
    """JSON over HTTP against one base URL, with a per-request timeout in seconds."""

    def __init__(self, url, timeout=10):
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.netloc:
            raise ValueError('Specify an http(s) service URL')
        self.url, self.timeout = url.rstrip('/'), timeout

    def request(self, path, body=None, method=None):
        """Send ``body`` as JSON (POST when given) and return the decoded reply.

        The reply is parsed as JSON when possible and returned as text otherwise;
        an empty reply returns None.
        """
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


# Cue job states after which a job never changes again.
TERMINAL = {'succeeded', 'failed', 'cancelled'}


class Cue(API):
    """Calls to Cue's job API."""

    def job(self, job_id):
        """Return Cue's record for one job."""
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
        """Ask Cue to cancel a job as ``submitter`` (for example ``team:comfy``).

        Cue answers once it accepts the request, before the job has stopped; call
        ``wait`` to see the outcome. If the job finished first, this returns the
        finished record instead of raising.
        """
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
        """Poll every ``poll`` seconds until the job ends; raise TimeoutError after ``timeout`` seconds."""
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
    """Check a ComfyUI server, and optionally Cue, and return each check's result.

    A server that answers has not shown that its models load or that a GPU
    render works; the result says so rather than reporting the server as ready.
    """
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
