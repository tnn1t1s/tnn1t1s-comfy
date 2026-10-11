import tempfile
from pathlib import Path
import unittest
from tnn1t1s_comfy.worker import publish_outputs
from tnn1t1s_comfy.artifacts import digest


class PublicationTests(unittest.TestCase):
    def test_loader_input_reference_is_retained_and_served_as_output(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);root=base/'results';attempt=root/'job'/'attempt'
            output=attempt/'output';temp=attempt/'temp';inputs=base/'inputs'
            for p in (output,temp,inputs):p.mkdir(parents=True)
            (inputs/'clip.mp4').write_bytes(b'original input')
            (output/'render.mp4').write_bytes(b'render')
            history={'loader':{'images':[{'filename':'clip.mp4','subfolder':'','type':'input'}]},
                     'save':{'images':[{'filename':'render.mp4','subfolder':'','type':'output'}]}}
            artifacts=publish_outputs(history,root,output,temp,inputs,attempt)
            (inputs/'clip.mp4').write_bytes(b'replaced input')
            ref=history['loader']['images'][0]
            self.assertEqual(ref['type'],'output')
            retained=root/ref['subfolder']/ref['filename']
            self.assertEqual(retained.read_bytes(),b'original input')
            self.assertEqual(artifacts[0]['sha256'],digest(retained))
            self.assertEqual(len(artifacts),2)

    def test_input_reference_cannot_escape_upload_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);inputs=root/'inputs';inputs.mkdir()
            (root/'secret').write_text('private')
            outputs={'images':[{'filename':'../secret','type':'input'}]}
            with self.assertRaisesRegex(ValueError,'escapes'):
                publish_outputs(outputs,root,root,root,inputs,root/'attempt')
