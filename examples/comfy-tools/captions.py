"""Redraw a clip's burned-in captions on a render of it.

Wan Animate repaints the picture around each person in blocks, and caption text that falls in or near a
block comes out as garbled letters. This removes the old text and draws each caption again, with new
wording where the catalog's text names the artist. Old text is found by its colour inside one horizontal
strip of the picture and painted over from its surroundings (OpenCV inpainting); the rest of every frame
is untouched before encoding. The picture is re-encoded once, on this machine: seconds for a 24 s clip.

The catalog's "captions" entry holds, for the original clip: its size, each line's time, box and text,
the text colour, the colour range that marks old text, and the strip that holds every caption.
"""
import subprocess
from pathlib import Path

# The typeface drawn. Cosmetic default: Helvetica Neue Medium is close in weight to the captions of the
# one clip catalogued so far. A clip with other captions wants another face; the file must exist.
FONT, FONT_INDEX = '/System/Library/Fonts/HelveticaNeue.ttc', 10
# Old text is cleaned from this long before a caption to this long after, in seconds. Observation: the
# Wan render blends a caption into the frames next to it; 0.2 s cleared every trace on the measured render.
LINGER_S = 0.2
# The colour mask is grown by this many pixels before inpainting, to take the soft edge of each letter.
# Heuristic: 5 left a faint outline on stray marks on the measured render, 7 did not.
MASK_GROW_PX = 7
# Inpainting radius in pixels (cv2.INPAINT_TELEA). Default: about a letter stroke at 480 px height.
INPAINT_RADIUS_PX = 4
# x264 quality of the re-encode. Default: 14 is visually lossless at 864x480, so this pass does not show.
CRF = 14


def fill(text, artist, spoken):
    """A catalog text with the artist's name put in: {artist} as given, {Artist} with a capital first
    letter, {spoken} the name the clip itself says."""
    return text.format(artist=artist, Artist=artist[:1].upper() + artist[1:], spoken=spoken)


def lines_in_cut(captions, start, duration, artist, spoken):
    """The catalog's caption lines that show during the cut, in cut time, with the artist's name filled in."""
    kept = []
    for line in captions['lines']:
        a, b = max(line['start'], start) - start, min(line['end'], start + duration) - start
        if b > a:
            kept.append({'start': a, 'end': b, 'box': line['box'], 'text': fill(line['text'], artist, spoken)})
    return kept


def redraw(video, target, captions, lines):
    """Write target as video with the old caption text removed and `lines` drawn; returns facts for the report."""
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    if not Path(FONT).is_file():
        raise SystemExit(f'captions: font {FONT} not found; set FONT in captions.py to a font on this machine')
    cap = cv2.VideoCapture(str(video))
    fps, width, height = cap.get(cv2.CAP_PROP_FPS), int(cap.get(3)), int(cap.get(4))
    if not fps or not width:
        raise SystemExit(f'captions: cannot read {video}')
    sx, sy = width / captions['size'][0], height / captions['size'][1]
    font = ImageFont.truetype(FONT, round(captions['font_px'] * sx), index=FONT_INDEX)
    top, bottom = (round(f * height) for f in captions['strip'])
    low, high = (tuple(v) for v in captions['old_text_hsv'])
    grow = np.ones((MASK_GROW_PX, MASK_GROW_PX), np.uint8)
    encoder = subprocess.Popen(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'bgr24',
         '-s', f'{width}x{height}', '-r', str(fps), '-i', '-', '-c:v', 'libx264', '-crf', str(CRF),
         '-preset', 'slow', '-pix_fmt', 'yuv420p', str(target)], stdin=subprocess.PIPE)
    index = cleaned = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        t = (index + 0.5) / fps
        if any(line['start'] - LINGER_S <= t <= line['end'] + LINGER_S for line in lines):
            strip = frame[top:bottom]
            mask = cv2.dilate(cv2.inRange(cv2.cvtColor(strip, cv2.COLOR_BGR2HSV), low, high), grow)
            frame[top:bottom] = cv2.inpaint(strip, mask, INPAINT_RADIUS_PX, cv2.INPAINT_TELEA)
            cleaned += 1
        for line in lines:
            if line['start'] <= t < line['end']:
                x0, y0, x1, y1 = line['box']
                image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                ImageDraw.Draw(image).text(((x0 + x1) / 2 * sx, (y0 + y1) / 2 * sy), line['text'], font=font,
                                           fill=tuple(captions['text_rgb']), anchor='mm')
                frame = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
        encoder.stdin.write(frame.tobytes())
        index += 1
    encoder.stdin.close()
    if encoder.wait():
        raise SystemExit(f'captions: ffmpeg failed encoding {target}')
    return {'lines': [{'at': round(line['start'], 2), 'text': line['text']} for line in lines],
            'frames': index, 'frames_cleaned': cleaned, 'strip_px': [top, bottom]}
