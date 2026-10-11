"""CPU-only acceptance fixture. Never included in the application package."""
import time
from PIL import Image
from comfy.utils import ProgressBar


class RelayProbe:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {'image': ('IMAGE',)}}
    RETURN_TYPES = ('IMAGE',)
    FUNCTION = 'run'
    CATEGORY = 'acceptance'

    def run(self, image):
        bar = ProgressBar(50)
        preview = Image.new('RGB', (32, 32), (32, 128, 192))
        for step in range(50):
            bar.update_absolute(step + 1, 50, ('JPEG', preview, None))
            time.sleep(.2)
        return (image,)


NODE_CLASS_MAPPINGS = {'CueRelayProbe': RelayProbe}
