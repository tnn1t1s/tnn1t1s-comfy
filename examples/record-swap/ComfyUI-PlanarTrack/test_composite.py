"""Blend-mask rules of CompositeIntoQuads on synthetic frames, without ComfyUI or torch.
Run: uv run --with numpy --with opencv-python-headless python -m unittest test_composite.py (from this directory)."""
import importlib.util
import json
import os
import sys
import types
import unittest

import numpy as np


class _Tensor:
    """The few torch.Tensor methods the node calls on its inputs."""

    def __init__(self, a):
        self.a = a

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self.a

    def __getitem__(self, i):
        return _Tensor(self.a[i])


def _load_pack():
    torch = types.ModuleType("torch")
    torch.from_numpy = lambda a: a
    sys.modules.setdefault("torch", torch)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__init__.py")
    spec = importlib.util.spec_from_file_location("planar_track", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CompositeBlendMask(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = _load_pack().CompositeIntoQuads()
        # A sleeve mask 2 px wider than the quad on each side, with a hole where segmentation missed part of the print.
        cls.sleeve = np.zeros((1, 400, 600), np.float32)
        cls.sleeve[0, 98:303, 298:483] = 1
        cls.sleeve[0, 150:200, 350:400] = 0
        # A hand over the sleeve's lower left.
        cls.hand = np.zeros((1, 400, 600), np.float32)
        cls.hand[0, 250:300, 300:340] = 1
        cls.frames = np.zeros((1, 400, 600, 3), np.float32)
        cls.art = np.ones((1, 100, 100, 3), np.float32)

    def composite(self, quad):
        # blur 0, grade 0, feather 0, sleeve masks, hand masks, grow 3 px
        _, alpha = self.node.run(_Tensor(self.frames), _Tensor(self.art), json.dumps([quad]), 0.0, 0.0, 0.0,
                                 _Tensor(self.sleeve), False, _Tensor(self.hand), False, 3.0)
        return alpha[0]

    def test_quad_on_sleeve(self):
        a = self.composite([[300, 100], [480, 100], [480, 300], [300, 300]])
        self.assertGreater(a[175, 375], 0.9, "a hole in the sleeve mask inside the quad gets the new art")
        self.assertGreater(a[200, 299], 0.9, "the sleeve strip just outside the quad gets the new art")
        self.assertLess(a[200, 296], 0.1, "background outside the sleeve keeps the footage")
        self.assertLess(a[275, 320], 0.1, "the hand stays in front of the new art")

    def test_quad_overshooting_the_sleeve(self):
        # The right edge sits 77 px past the sleeve, as when a tracked corner overshoots onto the background.
        a = self.composite([[300, 100], [560, 100], [560, 300], [300, 300]])
        self.assertLess(a[200, 530], 0.1, "the overshoot outside the sleeve's hull keeps the footage")
        self.assertGreater(a[175, 375], 0.9, "the hole inside the sleeve still gets the new art")


if __name__ == "__main__":
    unittest.main()
