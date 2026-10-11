"""Build a content-addressed source bootstrap for a pinned runtime image.
Application code uses no host filesystem or SSH; the task extracts its own bundle.
"""
import base64
import gzip
import hashlib
import json
from pathlib import Path
import shlex


def command(module, python='/opt/venv/bin/python'):
    package = Path(__file__).resolve().parents[1] / 'tnn1t1s_comfy'
    files = {p.name: p.read_text() for p in sorted(package.glob('*.py'))}
    blob = json.dumps(files, sort_keys=True).encode()
    revision = hashlib.sha256(blob).hexdigest()
    source = '''import base64,gzip,json,os,pathlib,sys
files=json.loads(gzip.decompress(base64.b64decode(BLOB)))
root=pathlib.Path('/tmp/tnn1t1s-comfy-REVISION')
pkg=root/'tnn1t1s_comfy';pkg.mkdir(parents=True,exist_ok=True)
for name,content in files.items():(pkg/name).write_text(content)
os.environ['PYTHONPATH']=str(root)
os.execv(sys.executable,[sys.executable,'-m',MODULE])
'''.replace('BLOB', repr(base64.b64encode(gzip.compress(blob, mtime=0)).decode())).replace('REVISION', revision[:16]).replace('MODULE', repr(module))
    return python + ' -c ' + shlex.quote(source), revision
