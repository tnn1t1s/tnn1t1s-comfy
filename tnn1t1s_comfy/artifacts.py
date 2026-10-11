"""Validate worker publication before exposing success through the gateway."""
import hashlib
import json
from pathlib import Path


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def confined(root, relative):
    root = Path(root).resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError('Artifact path escapes the configured output directory')
    return candidate


def read_manifest(root, job):
    instances = job.get('instances', [])
    if not instances:
        raise ValueError('Successful job has no attempt evidence')
    attempt = max(instances, key=lambda i: i['attempt'])
    if attempt.get('result_truncated') or attempt.get('result_error'):
        raise ValueError('Worker manifest pointer is incomplete')
    pointer = json.loads(attempt['result'])
    path = confined(root, pointer['manifest'])
    if digest(path) != pointer['sha256']:
        raise ValueError('Manifest digest mismatch')
    manifest = json.loads(path.read_text())
    if manifest['job_id'] != job['id']:
        raise ValueError('Manifest belongs to a different job')
    # The selected Cue attempt supplies the only accepted manifest pointer.
    # Every worker invocation writes a new directory; late attempts cannot overwrite it.
    for artifact in manifest['artifacts']:
        file = confined(root, artifact['path'])
        if file.stat().st_size != artifact['bytes'] or digest(file) != artifact['sha256']:
            raise ValueError('Published artifact failed verification')
    return manifest
