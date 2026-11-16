"""Measure the sleeve's corners in shot 2 (Zooey's close-up, frames 34-53), where the sleeve's back is so far out of
focus that neither SIFT nor ECC can align the artwork. Writes quads_s2.json next to this script.

The sleeve's top and left edges are found as halfway crossings between the dark background and the white back.
Its on-screen width comes from the top of Ringo Starr's hair, whose position across the back cover is measured on
the 1981 scan. The sleeve is square and its left edge is taken as vertical.
Usage: measure_s2.py FOOTAGE.mp4 RINGO_BACK.jpg"""
import json, os, subprocess, sys

import cv2
import numpy as np

footage, back_scan = sys.argv[1:3]
FIRST, N = 34, 20          # shot 2 in the trimmed clip, from the cut analysis
TOP_COLS = (760, 1250)     # px: columns where the dark shelf sits directly above the sleeve's top edge
LEFT_ROW = 515             # px: row where the dark jacket sits directly left of the sleeve's left edge
SMOOTH = 5                 # frames, centred moving average. Heuristic: a defocused edge moves its halfway crossing by a few px


def hair_fraction(path):
    """Ringo's hair top as a share of the back cover's width: centre of the topmost dark region's first rows."""
    a = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    g = cv2.resize(a, (600, int(600 * a.shape[0] / a.shape[1])))
    dark = (g < 80).astype(np.uint8)
    dark[:, :60] = 0   # the label logo column on the left
    _, lab, st, _ = cv2.connectedComponentsWithStats(dark)
    ys, xs = np.nonzero(lab == 1 + np.argmax(st[1:, 4]))
    top = xs[ys < ys.min() + 5]
    return (top.min() + top.max()) / 2 / g.shape[1]


def frames(path):
    """Shot 2's frames as BGR arrays, decoded by ffmpeg. OpenCV's decoder converts colour slightly differently
    (about 2 levels on average), enough to move a halfway crossing on this soft edge by a few px."""
    w, h = 1280, 544
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", f"select='between(n,{FIRST},{FIRST + N - 1})'",
                          "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                         capture_output=True, check=True).stdout
    if len(raw) != N * w * h * 3:
        sys.exit(f"measure_s2: expected {N} frames of {w}x{h} from {path}, got {len(raw)} bytes")
    return list(np.frombuffer(raw, np.uint8).reshape(N, h, w, 3))


u = hair_fraction(back_scan)
rows = []
for f in frames(footage):
    g = cv2.GaussianBlur(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(float), (0, 0), 2)

    def crossing(c):   # first index where c passes halfway between its start (background) and its end (sleeve)
        lo, hi = np.percentile(c[:15], 50), np.percentile(c[-30:], 50)
        return int(np.argmax(c > (lo + hi) / 2))
    yl = 440 + crossing(g[440:, TOP_COLS[0]])
    yr = 330 + crossing(g[330:, TOP_COLS[1]])
    c = g[LEFT_ROW, 640:900]
    lx = 640 + int(np.argmax(c > (c[:15].mean() + c[-30:].mean()) / 2))
    slope = (yr - yl) / (TOP_COLS[1] - TOP_COLS[0])
    tly = yl + slope * (lx - TOP_COLS[0])
    d = g < 90
    d[:430] = 0; d[:, :850] = 0; d[:, 1220:] = 0   # the hair sits below the edge, right of Zooey, left of Tom
    ys, xs = np.nonzero(d)
    hx = xs[ys < ys.min() + 6].mean()
    rows.append([lx, tly, slope, hx])
R = np.array(rows)
sm = lambda v: np.convolve(np.pad(v, SMOOTH // 2, mode="edge"), np.ones(SMOOTH) / SMOOTH, "valid")
lx, tly, slope, hx = (sm(R[:, i]) for i in range(4))
quads = []
for i in range(N):
    along = np.array([1, slope[i]]) / np.sqrt(1 + slope[i] ** 2)
    down = np.array([0.0, 1.0])
    w = (hx[i] - lx[i]) * np.sqrt(1 + slope[i] ** 2) / u
    tl = np.array([lx[i], tly[i]])
    quads.append([tl.tolist(), (tl + w * along).tolist(), (tl + w * along + w * down).tolist(), (tl + w * down).tolist()])
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quads_s2.json")
json.dump(quads, open(out, "w"))
print(f"hair at {u:.3f} of the width; top-left {quads[0][0][0]:.0f},{quads[0][0][1]:.0f} -> "
      f"{quads[-1][0][0]:.0f},{quads[-1][0][1]:.0f}; width {np.linalg.norm(np.subtract(quads[0][1], quads[0][0])):.0f} px; -> {out}")
