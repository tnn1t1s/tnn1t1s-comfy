import copy
from pathlib import Path
import shlex
import subprocess
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy'))
from bundle import command
from build_spec import build


class BundleTests(unittest.TestCase):
    def test_compressed_bootstrap_executes_package(self):
        cmd, revision = command('tnn1t1s_comfy.cli', sys.executable)
        result = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn('doctor', result.stderr)
        self.assertEqual(len(revision), 64)
        self.assertLess(len(cmd.encode()), 65536)

    def test_large_environment_rejected_before_deployment(self):
        profile = {'gateway_app': {'gpus': 0, 'container': {'image': 'image@sha256:pin'}},
                   'gateway_config': {'worker_spec': {'image': 'image@sha256:pin'}}}
        self.assertIn('cmd', build(profile))
        profile['gateway_app']['env'] = {'LARGE': 'x' * 65536}
        with self.assertRaisesRegex(ValueError, '64 KiB'):
            build(profile)
