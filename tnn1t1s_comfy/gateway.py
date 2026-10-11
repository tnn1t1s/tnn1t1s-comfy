"""Run pinned upstream ComfyUI with a durable remote queue."""
import fcntl
import importlib.machinery
import json
import runpy
import time
import os
from pathlib import Path
import sys


class GuardLoader:
    """Disable the upstream local consumer even if custom-node loading fails."""
    def __init__(self, wrapped): self.wrapped = wrapped
    def create_module(self, spec): return self.wrapped.create_module(spec)
    def exec_module(self, module):
        self.wrapped.exec_module(module)
        def no_local_work(self, timeout=None):
            time.sleep(.25)
            return None
        module.PromptQueue.get = no_local_work


class GuardFinder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname != 'execution': return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None: raise ImportError('Pinned ComfyUI execution module unavailable')
        spec.loader = GuardLoader(spec.loader)
        return spec


def main():
    config = json.loads(os.environ['COMFY_GATEWAY_CONFIG'])
    journal = Path(config['journal'])
    journal.parent.mkdir(parents=True, exist_ok=True)
    lock = open(str(journal) + '.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.set_inheritable(lock.fileno(), True)
    comfy = Path(config['comfy_dir'])
    plugin = comfy / 'custom_nodes' / 'tnn1t1s_cue_gateway'
    plugin.mkdir(exist_ok=True)
    (plugin / '__init__.py').write_text('from tnn1t1s_comfy.plugin import *\n')
    worker = config['worker_config']
    for key in ('input_root', 'output_root'):
        Path(worker[key]).mkdir(parents=True, exist_ok=True)
    # This gateway never needs a GPU, even when its Cue workers request one.
    args = [sys.executable, 'main.py', '--cpu', '--listen', '0.0.0.0',
            '--port', os.environ['PORT0'], '--input-directory', worker['input_root'],
            '--output-directory', worker['output_root']] + config.get('args', [])
    os.chdir(comfy)
    sys.argv = args[1:]
    sys.path.insert(0, str(comfy))
    sys.meta_path.insert(0, GuardFinder())
    runpy.run_path(str(comfy / "main.py"), run_name="__main__")


if __name__ == '__main__':
    main()
