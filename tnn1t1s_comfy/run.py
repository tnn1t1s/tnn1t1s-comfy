"""Submit an API-format graph with local input files and collect its outputs."""
import copy
import hashlib
import json
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from .client import API, APIError

# Loader classes whose file field is known. Any other class must name its
# field explicitly (NODE.FIELD=PATH) so a file never lands in a guessed input.
LOADER_FIELDS = {'LoadImage': 'image', 'LoadImageMask': 'image', 'LoadVideo': 'file'}
# Output kinds this command downloads; 'temp' previews are not results.
OUTPUT_TYPE = 'output'


def load_graph(path):
    graph = json.loads(Path(path).read_text())
    # Accept a saved /prompt request body; a node may itself have the ID 'prompt'.
    wrapped = graph.get('prompt') if isinstance(graph, dict) else None
    if isinstance(wrapped, dict) and 'class_type' not in wrapped:
        graph = wrapped
    if not isinstance(graph, dict) or not graph or not all(
            isinstance(node, dict) and 'class_type' in node for node in graph.values()):
        raise ValueError('Graph must be ComfyUI API format: {node_id: {class_type, inputs}}')
    return graph


def parse_binding(text):
    """NODE=PATH or NODE.FIELD=PATH. Node IDs may not contain '=' or '.'."""
    target, sep, value = text.partition('=')
    if not sep or not target or not value:
        raise ValueError(f'Expected NODE=VALUE or NODE.FIELD=VALUE, got {text!r}')
    node, _, field = target.partition('.')
    return node, field or None, value


def _field(graph, node, field, purpose):
    if node not in graph:
        raise ValueError(f'Node {node!r} is not in the graph; nodes: {", ".join(sorted(graph))}')
    entry = graph[node]
    if field is None:
        if purpose != 'input':
            raise ValueError(f'--set needs NODE.FIELD for node {node!r}')
        field = LOADER_FIELDS.get(entry['class_type'])
        if field is None:
            raise ValueError(f'Node {node!r} is {entry["class_type"]}; name its file field as {node}.FIELD=PATH')
    inputs = entry.get('inputs', {})
    if field not in inputs:
        raise ValueError(f'Node {node!r} has no input {field!r}; inputs: {", ".join(sorted(inputs))}')
    if isinstance(inputs[field], list):
        raise ValueError(f'{node}.{field} is a link from another node, not a value')
    return field


def upload_name(path, digest):
    """Shared input directories are not snapshotted per attempt, so name by content."""
    return f'cli-{digest[:16]}{path.suffix.lower()}'


def _multipart(fields, file_field, filename, data):
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
                 f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
                 + data + b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())
    return b''.join(parts), f'multipart/form-data; boundary={boundary}'


class Comfy(API):
    def raw(self, path, data=None, content_type=None):
        headers = {'Content-Type': content_type} if content_type else {}
        try:
            with urlopen(Request(self.url + path, data=data, headers=headers), timeout=self.timeout) as r:
                return r.read()
        except Exception as exc:
            if hasattr(exc, 'code') and hasattr(exc, 'read'):
                raise APIError(exc.code, exc.read().decode(errors='replace')) from exc
            raise

    def upload(self, path):
        path = Path(path)
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        name = upload_name(path, digest)
        # overwrite is safe: the name is derived from the content.
        body, ctype = _multipart({'type': 'input', 'overwrite': 'true'}, 'image', name, data)
        result = json.loads(self.raw('/upload/image', body, ctype))
        if result.get('name') != name:
            raise RuntimeError(f'ComfyUI stored {path.name} as {result.get("name")!r}, expected {name!r}')
        ref = f'{result["subfolder"]}/{name}' if result.get('subfolder') else name
        return {'source': str(path), 'sha256': digest, 'bytes': len(data), 'uploaded_as': ref}

    def view(self, item):
        query = urlencode({k: item.get(k, '') for k in ('filename', 'subfolder', 'type')})
        return self.raw('/view?' + query)


def bind(graph, inputs, sets):
    """Return a copy of graph with uploaded references and --set values applied."""
    graph = copy.deepcopy(graph)
    for (node, field, ref) in inputs:
        graph[node]['inputs'][field] = ref
    for (node, field, value) in sets:
        graph[node]['inputs'][field] = value
    return graph


def outputs_of(history):
    for node, output in sorted(history.get('outputs', {}).items()):
        for kind, items in sorted(output.items()):
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and 'filename' in item and item.get('type') == OUTPUT_TYPE:
                    yield node, kind, item


def run(comfy_url, graph_path, input_specs, set_specs, out_dir, wait, poll=2, timeout=60):
    graph = load_graph(graph_path)
    planned_inputs, planned_sets = [], []
    for spec in input_specs:
        node, field, path = parse_binding(spec)
        if not Path(path).is_file():
            raise ValueError(f'Input file not found: {path}')
        planned_inputs.append((node, _field(graph, node, field, 'input'), path))
    for spec in set_specs:
        node, field, value = parse_binding(spec)
        planned_sets.append((node, _field(graph, node, field, 'set'), value))

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    comfy = Comfy(comfy_url, timeout=timeout)
    uploads, bound = [], []
    for node, field, path in planned_inputs:
        record = comfy.upload(path) | {'node': node, 'field': field}
        uploads.append(record)
        bound.append((node, field, record['uploaded_as']))
    graph = bind(graph, bound, planned_sets)

    client_id = uuid.uuid4().hex
    response = comfy.request('/prompt', {'prompt': graph, 'client_id': client_id})
    prompt_id = response['prompt_id']
    record = {'comfy_url': comfy_url, 'prompt_id': prompt_id, 'client_id': client_id,
              'graph': str(graph_path), 'inputs': uploads,
              'sets': [{'node': n, 'field': f, 'value': v} for n, f, v in planned_sets],
              'status': 'submitted', 'outputs': []}
    manifest = out / 'run.json'
    # Written before waiting so a timeout or crash still leaves the prompt ID.
    manifest.write_text(json.dumps(record, indent=2))

    deadline = time.monotonic() + wait
    while True:
        history = comfy.request('/history/' + quote(prompt_id, safe='')) or {}
        entry = history.get(prompt_id)
        status = (entry or {}).get('status', {})
        if entry and status.get('completed') is not None and (
                status.get('completed') or status.get('status_str') == 'error'):
            break
        if time.monotonic() >= deadline:
            record['status'] = 'timeout'
            manifest.write_text(json.dumps(record, indent=2))
            raise TimeoutError(f'Prompt {prompt_id} has no terminal history after {wait}s; '
                               'it may still be running')
        time.sleep(min(poll, max(0, deadline - time.monotonic())))

    record['status'] = status.get('status_str') or ('success' if status.get('completed') else 'error')
    if record['status'] != 'success':
        record['messages'] = status.get('messages', [])
    for node, kind, item in outputs_of(entry):
        data = comfy.view(item)
        target = out / node / Path(item['filename']).name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        record['outputs'].append({'node': node, 'kind': kind, 'path': str(target),
                                  'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)})
    manifest.write_text(json.dumps(record, indent=2))
    return record
