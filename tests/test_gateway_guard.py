import types
import unittest
from unittest.mock import patch
from tnn1t1s_comfy.gateway import GuardLoader


class GuardTest(unittest.TestCase):
    def test_plugin_failure_cannot_enable_local_execution(self):
        class OriginalQueue:
            def get(self, timeout=None):
                raise AssertionError('A local prompt was consumed')
        class Loader:
            def exec_module(self, module): module.PromptQueue = OriginalQueue
        module = types.SimpleNamespace()
        GuardLoader(Loader()).exec_module(module)
        # No remote plugin is installed: fallback still cannot consume a prompt.
        with patch('tnn1t1s_comfy.gateway.time.sleep'):
            self.assertIsNone(module.PromptQueue().get())
