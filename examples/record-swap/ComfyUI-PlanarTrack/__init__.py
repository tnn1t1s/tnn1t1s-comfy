"""Planar replacement for ComfyUI: put a flat image (an album cover, a sign, a screen) onto a tracked flat object
in a video, behind whatever covers it.

PlanarTrack          four corners set on one frame -> four corners per frame, following the object's texture
MaskToQuads          per-frame object mask (e.g. from SAM2 video segmentation) -> four corners per frame, as JSON
CompositeIntoQuads   warps the image into each frame's corners and composites it inside the object mask, so
                     occluders the mask leaves out (fingers over a sleeve) stay in front

The corner JSON is a list with one entry per frame: [[x, y] x4] ordered top-left, top-right, bottom-right,
bottom-left in frame pixels, or null where the frame has no usable mask.
"""
import json

import cv2
import numpy as np
import torch


def _order(pts):
    """Order four points TL, TR, BR, BL by coordinate sums and differences."""
    pts = np.asarray(pts, dtype=np.float64)
    s, d = pts.sum(1), pts[:, 1] - pts[:, 0]
    return np.array([pts[s.argmin()], pts[d.argmin()], pts[s.argmax()], pts[d.argmax()]])


def _four_sides(hull, perimeter):
    """Corners from the four longest edges of a lightly simplified hull, intersected in order. An occluder over a
    corner makes the hull cut it with a short chamfer edge; dropping the short edges and extending the long ones
    puts the hidden corner back. minAreaRect when the hull has fewer than four edges."""
    poly = cv2.approxPolyDP(hull, 0.01 * perimeter, True).reshape(-1, 2).astype(np.float64)
    if len(poly) < 4:
        return cv2.boxPoints(cv2.minAreaRect(hull)).astype(np.float64)
    edges = [(poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly))]
    keep = sorted(sorted(range(len(edges)), key=lambda i: -np.linalg.norm(edges[i][1] - edges[i][0]))[:4])
    lines = [_line(*edges[i]) for i in keep]
    out = []
    for i in range(4):
        c = np.cross(lines[i - 1], lines[i])
        if abs(c[2]) < 1e-9:
            return cv2.boxPoints(cv2.minAreaRect(hull)).astype(np.float64)
        out.append(c[:2] / c[2])
    return np.array(out)


def _line(p, q):
    """Homogeneous line through two points."""
    return np.cross([p[0], p[1], 1.0], [q[0], q[1], 1.0])


def _refine(quad, contour, tol_px):
    """Re-fit each side to the contour points within tol_px of it, then intersect adjacent sides.
    Notches cut by occluders sit farther than tol_px from the side, so they drop out of the fit, and a corner
    hidden under a finger comes back as the intersection of its two visible sides."""
    lines = []
    for i in range(4):
        p, q = quad[i], quad[(i + 1) % 4]
        l = _line(p, q)
        n = np.hypot(l[0], l[1])
        dist = np.abs(contour @ l[:2] + l[2]) / n
        # Only points between the side's endpoints (with 10% slack) belong to this side.
        t = (contour - p) @ (q - p) / max(np.dot(q - p, q - p), 1e-9)
        near = contour[(dist < tol_px) & (t > -0.1) & (t < 1.1)]
        if len(near) < 8:   # too few points to fit; keep the polygon side
            lines.append(l)
            continue
        vx, vy, x0, y0 = cv2.fitLine(near.astype(np.float32), cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
        lines.append(_line((x0, y0), (x0 + vx, y0 + vy)))
    out = []
    for i in range(4):
        c = np.cross(lines[i - 1], lines[i])
        if abs(c[2]) < 1e-9:
            return quad
        out.append(c[:2] / c[2])
    out = np.array(out)
    # A refit that moves a corner by more than a quarter of the quad's size went wrong; keep the polygon.
    size = np.sqrt(abs(cv2.contourArea(quad.astype(np.float32))))
    if np.abs(out - quad).max() > 0.25 * size:
        return quad
    return out


class MaskToQuads:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "masks": ("MASK",),
            # px: smaller blobs are noise or a sliver of the object at the frame edge, not a usable plane
            "min_area_px": ("INT", {"default": 2000, "min": 0, "max": 10_000_000}),
            # px: how far a contour point may sit from a side and still count toward that side's fit
            "side_tolerance_px": ("FLOAT", {"default": 3.0, "min": 0.5, "max": 50.0, "step": 0.5}),
            # frames: half-width of the centred moving average over corners; 0 turns smoothing off
            "smooth_frames": ("INT", {"default": 1, "min": 0, "max": 12}),
            # masks arrive last-frame-first (SAM2 propagated over a reversed batch); flip them to clip order
            "reverse": ("BOOLEAN", {"default": False}),
        }}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("quads_json",)
    FUNCTION = "run"
    CATEGORY = "planar_track"

    def run(self, masks, min_area_px, side_tolerance_px, smooth_frames, reverse):
        m = masks.detach().cpu().numpy()
        if m.ndim == 2:
            m = m[None]
        if reverse:
            m = m[::-1]
        quads = []
        for f in m:
            b = (f > 0.5).astype(np.uint8)
            cs, _ = cv2.findContours(b, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
            if not cs:
                quads.append(None)
                continue
            c = max(cs, key=cv2.contourArea)
            if cv2.contourArea(c) < min_area_px:
                quads.append(None)
                continue
            hull = cv2.convexHull(c)
            q = _order(_four_sides(hull, cv2.arcLength(hull, True)))
            q = _order(_refine(q, c.reshape(-1, 2).astype(np.float64), side_tolerance_px))
            quads.append(q)
        if smooth_frames > 0:
            sm = []
            for i, q in enumerate(quads):
                if q is None:
                    sm.append(None)
                    continue
                win = [quads[j] for j in range(max(0, i - smooth_frames), min(len(quads), i + smooth_frames + 1))
                       if quads[j] is not None]
                sm.append(np.mean(win, axis=0))
            quads = sm
        return (json.dumps([None if q is None else np.round(q, 2).tolist() for q in quads]),)


class CompositeIntoQuads:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "frames": ("IMAGE",),
            "image": ("IMAGE",),
            "quads_json": ("STRING", {"forceInput": True}),
            # Gaussian sigma in output px applied to the warped image, to match the footage's softness
            "blur_sigma_px": ("FLOAT", {"default": 0.8, "min": 0.0, "max": 10.0, "step": 0.1}),
            # 0..1: how far the warped image's per-channel mean and spread move toward the footage's
            # original pixels inside the mask (0 keeps the image's own colours, 1 copies the footage's grade)
            "grade_match": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.05}),
            # px: soften the composite edge; 0 is a hard edge
            "feather_px": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.5}),
        }, "optional": {
            # object mask per frame. Without occluder_masks, pixels outside it keep the footage. With them, it only
            # decides the band between the quad and the grown quad (see grow_px).
            "masks": ("MASK",),
            "masks_reversed": ("BOOLEAN", {"default": False}),
            # things in front of the object (hands); inside the quad everything else is replaced
            "occluder_masks": ("MASK",),
            "occluder_masks_reversed": ("BOOLEAN", {"default": False}),
            # px: the image is warped into the quad grown by this much, and the band outside the quad is replaced
            # where `masks` says object, so a quad a pixel or two inside the real edge leaves no strip of the old one
            "grow_px": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 20.0, "step": 0.5}),
            # a second image for the first lead_frames frames (the sleeve's back before it is turned to its front)
            "lead_image": ("IMAGE",),
            "lead_frames": ("INT", {"default": 0, "min": 0, "max": 100000}),
            # 0..1: grain added to the new image, as a fraction of the footage's own grain measured under the
            # composite (median absolute deviation of the frame minus its 1.5 px blur). 1 matches it; 0 adds none.
            "grain_match": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 2.0, "step": 0.05}),
        }}

    RETURN_TYPES = ("IMAGE", "MASK")
    RETURN_NAMES = ("images", "composite_mask")
    FUNCTION = "run"
    CATEGORY = "planar_track"

    def run(self, frames, image, quads_json, blur_sigma_px, grade_match, feather_px, masks=None, masks_reversed=False,
            occluder_masks=None, occluder_masks_reversed=False, grow_px=0.0, lead_image=None, lead_frames=0,
            grain_match=0.0):
        quads = json.loads(quads_json)
        fr = frames.detach().cpu().numpy().astype(np.float32)
        n, h, w, _ = fr.shape
        if len(quads) != n:
            raise ValueError(f"CompositeIntoQuads: {len(quads)} quads for {n} frames")
        def load(m, rev, name):
            if m is None:
                return None
            m = m.detach().cpu().numpy().astype(np.float32)
            if m.ndim == 2:
                m = m[None]
            if rev:
                m = m[::-1]
            if m.shape[0] != n or m.shape[1:] != (h, w):
                raise ValueError(f"CompositeIntoQuads: {name} {m.shape} do not match frames {fr.shape}")
            return m
        om = load(masks, masks_reversed, "masks")
        occ = load(occluder_masks, occluder_masks_reversed, "occluder_masks")
        main = image[0].detach().cpu().numpy().astype(np.float32)
        lead = None if lead_image is None else lead_image[0].detach().cpu().numpy().astype(np.float32)
        out, cm = fr.copy(), np.zeros((n, h, w), np.float32)
        rng = np.random.default_rng(0)   # fixed seed: reruns give the same grain
        for i, q in enumerate(quads):
            if q is None:
                continue
            src = lead if (lead is not None and i < lead_frames) else main
            q_exact = np.asarray(q, np.float32)
            c = q_exact.mean(0)
            r = np.linalg.norm(q_exact - c, axis=1).mean()
            q = (c + (q_exact - c) * (1 + grow_px / max(r, 1.0))).astype(np.float32)   # corners pushed out ~grow_px
            side = max(np.linalg.norm(q[1] - q[0]), np.linalg.norm(q[2] - q[1]),
                       np.linalg.norm(q[3] - q[2]), np.linalg.norm(q[0] - q[3]))
            # Area-resample the source to about twice the target size first; warpPerspective alone aliases
            # when it shrinks a large image a lot.
            s = int(max(8, min(max(src.shape[:2]), 2 * side)))
            sr = cv2.resize(src, (s, int(round(s * src.shape[0] / src.shape[1]))), interpolation=cv2.INTER_AREA)
            sh, sw = sr.shape[:2]
            H = cv2.getPerspectiveTransform(np.float32([[0, 0], [sw, 0], [sw, sh], [0, sh]]), q)
            warped = cv2.warpPerspective(sr, H, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
            cover = cv2.warpPerspective(np.ones((sh, sw), np.float32), H, (w, h), flags=cv2.INTER_LINEAR)
            if blur_sigma_px > 0:
                warped = cv2.GaussianBlur(warped, (0, 0), blur_sigma_px)
                cover = cv2.GaussianBlur(cover, (0, 0), blur_sigma_px)
                warped = warped / np.maximum(cover[..., None], 1e-3) * (cover[..., None] > 1e-3)
            if occ is not None:
                exact = cv2.warpPerspective(np.ones((sh, sw), np.float32), cv2.getPerspectiveTransform(
                    np.float32([[0, 0], [sw, 0], [sw, sh], [0, sh]]), q_exact), (w, h), flags=cv2.INTER_LINEAR)
                if om is not None:
                    # Inside the quad, replace only within the convex hull of the visible object: holes the mask
                    # leaves (print it mistook for another object) are inside the hull, while a quad corner that
                    # overshoots past an occluder onto the background is outside it.
                    hull = np.zeros((h, w), np.float32)
                    ys, xs = np.nonzero(om[i] > 0.5)
                    if len(xs) >= 3:
                        cv2.fillConvexPoly(hull, cv2.convexHull(np.stack([xs, ys], 1).astype(np.int32)), 1.0)
                        k = 2 * int(round(grow_px)) + 1
                        hull = cv2.dilate(hull, np.ones((k, k), np.uint8))
                    a = cover * np.maximum(exact * hull, om[i]) * (1 - occ[i])
                else:
                    a = cover * exact * (1 - occ[i])
            else:
                a = cover if om is None else cover * om[i]
            if feather_px > 0:
                a = cv2.GaussianBlur(a, (0, 0), feather_px)
            a = np.clip(a, 0, 1)
            sel = a > 0.5
            if grade_match > 0 and sel.sum() > 50:
                mu_f, sd_f = fr[i][sel].mean(0), fr[i][sel].std(0) + 1e-4
                mu_w, sd_w = warped[sel].mean(0), warped[sel].std(0) + 1e-4
                g = grade_match
                warped = (warped - mu_w) * ((1 - g) + g * sd_f / sd_w) + (1 - g) * mu_w + g * mu_f
                warped = np.clip(warped, 0, 1)
            if grain_match > 0 and sel.sum() > 50:
                hp = fr[i] - cv2.GaussianBlur(fr[i], (0, 0), 1.5)
                # grain level per channel: median absolute deviation (x1.4826 = sigma for Gaussian noise). A plain std
                # also counts the old sleeve's printed edges; on this footage it read 0.028-0.035 against 0.007 grain.
                v = hp[sel]
                sd = 1.4826 * np.median(np.abs(v - np.median(v, 0)), 0)
                # the footage's grain is about 1 px wide: noise blurred 0.6 px, rescaled to unit spread
                z = cv2.GaussianBlur(rng.standard_normal((h, w, 3)).astype(np.float32), (0, 0), 0.6)
                z /= z.std() + 1e-6
                warped = np.clip(warped + grain_match * sd * z, 0, 1)
            out[i] = fr[i] * (1 - a[..., None]) + warped * a[..., None]
            cm[i] = a
        return (torch.from_numpy(out), torch.from_numpy(cm))


class PlanarTrack:
    """Follow a flat textured object through a shot from four corners set on one reference frame. Each frame's
    homography comes from SIFT matches inside the object (to the reference frame when enough survive, otherwise
    to the neighbouring frame already solved), so corners hidden by fingers or outside the frame move with the
    printed surface rather than with the visible outline."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "frames": ("IMAGE",),
            "reference_index": ("INT", {"default": 0, "min": 0, "max": 100000}),
            # [[x, y] x4] TL, TR, BR, BL on the reference frame, in frame px; corners may lie outside the frame
            "reference_quad": ("STRING", {"default": "[[0,0],[1,0],[1,1],[0,1]]"}),
            # frames are upsampled by this factor before feature detection; small objects give few features at 1x
            "upscale": ("INT", {"default": 2, "min": 1, "max": 4}),
            # RANSAC inliers needed to accept a homography (heuristic: below ~20 a wrong H fits by chance)
            "min_inliers": ("INT", {"default": 20, "min": 8, "max": 500}),
            "smooth_frames": ("INT", {"default": 1, "min": 0, "max": 12}),
        }, "optional": {
            # object mask per frame; features are taken only inside it, so occluders (hands) do not vote
            "masks": ("MASK",),
            "masks_reversed": ("BOOLEAN", {"default": False}),
        }}

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("quads_json", "track_log")
    FUNCTION = "run"
    CATEGORY = "planar_track"

    def run(self, frames, reference_index, reference_quad, upscale, min_inliers, smooth_frames, masks=None, masks_reversed=False):
        fr = (frames.detach().cpu().numpy() * 255).clip(0, 255).astype(np.uint8)
        n, h, w, _ = fr.shape
        if not 0 <= reference_index < n:
            raise ValueError(f"PlanarTrack: reference_index {reference_index} outside {n} frames")
        om = None
        if masks is not None:
            om = masks.detach().cpu().numpy()
            if om.ndim == 2:
                om = om[None]
            if masks_reversed:
                om = om[::-1]
            if om.shape[0] != n:
                raise ValueError(f"PlanarTrack: {om.shape[0]} masks for {n} frames")
        ref_q = np.asarray(json.loads(reference_quad), np.float64).reshape(4, 2)
        s = upscale
        sift = cv2.SIFT_create(nfeatures=4000)
        matcher = cv2.BFMatcher(cv2.NORM_L2)

        def feats(t, quad):
            g = cv2.cvtColor(fr[t], cv2.COLOR_RGB2GRAY)
            region = np.zeros((h, w), np.uint8)
            cv2.fillPoly(region, [np.round(quad).astype(np.int32)], 1)
            region = cv2.dilate(region, np.ones((31, 31), np.uint8))   # 15 px of slack for motion since the neighbour
            if om is not None:
                region &= (om[t] > 0.5).astype(np.uint8)
            g = cv2.resize(g, (w * s, h * s), interpolation=cv2.INTER_CUBIC)
            region = cv2.resize(region, (w * s, h * s), interpolation=cv2.INTER_NEAREST)
            kp, d = sift.detectAndCompute(g, region)
            pts = np.float32([k.pt for k in kp]) / s if kp else np.zeros((0, 2), np.float32)
            return pts, d

        def homog(pa, da, pb, db):
            if da is None or db is None or len(pa) < min_inliers or len(pb) < min_inliers:
                return None, 0
            good = [m for m, k in (x for x in matcher.knnMatch(da, db, k=2) if len(x) == 2) if m.distance < 0.75 * k.distance]
            if len(good) < min_inliers:
                return None, len(good)
            H, inl = cv2.findHomography(pa[[m.queryIdx for m in good]], pb[[m.trainIdx for m in good]], cv2.RANSAC, 2.0)
            k = int(inl.sum()) if inl is not None else 0
            return (H if H is not None and k >= min_inliers else None), k

        def ecc(p, t, quad):
            """Homography from frame p to frame t by ECC intensity alignment inside p's object region, coarse
            (half size) then full size. Intensity alignment survives motion blur that leaves SIFT with too few
            matches. Returns (H, correlation) or (None, 0)."""
            region = np.zeros((h, w), np.uint8)
            cv2.fillPoly(region, [np.round(quad).astype(np.int32)], 1)
            if om is not None:
                region &= (om[p] > 0.5).astype(np.uint8)
            region = cv2.erode(region, np.ones((5, 5), np.uint8))   # 2 px off the edge, where the background moves differently
            if region.sum() < 500:
                return None, 0.0
            ga = cv2.cvtColor(fr[p], cv2.COLOR_RGB2GRAY).astype(np.float32) / 255
            gb = cv2.cvtColor(fr[t], cv2.COLOR_RGB2GRAY).astype(np.float32) / 255
            W = np.eye(3, dtype=np.float32)
            crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 200, 1e-5)
            cc = 0.0
            try:
                for f in (0.5, 1.0):
                    S = np.diag([f, f, 1]).astype(np.float32)
                    Wf = S @ W @ np.linalg.inv(S)
                    a = cv2.resize(ga, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
                    b = cv2.resize(gb, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
                    m = cv2.resize(region, (a.shape[1], a.shape[0]), interpolation=cv2.INTER_NEAREST)
                    cc, Wf = cv2.findTransformECC(a, b, Wf, cv2.MOTION_HOMOGRAPHY, crit, m, 5)
                    W = np.linalg.inv(S) @ Wf @ S
            except cv2.error:
                return None, 0.0
            # ECC's warp maps template (p) coordinates into input (t) coordinates; a correlation under 0.8 means
            # the alignment did not lock (heuristic threshold, not measured on this footage).
            return (W.astype(np.float64) if cc >= 0.8 else None), float(cc)

        def shape_ecc(p, t):
            """Homography from frame p to frame t by ECC alignment of the two softened object masks, started from
            the shift between their centroids. Uses only the outline, so it works when blur leaves no texture; a
            hand that moves with the object moves its notch with it. Returns (H, correlation) or (None, 0)."""
            if om is None:
                return None, 0.0
            a, b = (om[p] > 0.5).astype(np.float32), (om[t] > 0.5).astype(np.float32)
            if a.sum() < 500 or b.sum() < 500:
                return None, 0.0
            ma, mb = cv2.moments(a), cv2.moments(b)
            W = np.eye(3, dtype=np.float32)
            W[0, 2] = mb["m10"] / mb["m00"] - ma["m10"] / ma["m00"]
            W[1, 2] = mb["m01"] / mb["m00"] - ma["m01"] / ma["m00"]
            crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 300, 1e-6)
            cc = 0.0
            try:
                for sig, f in ((6.0, 0.5), (2.0, 1.0)):   # px blur at each scale: wide basin first, then the edge
                    S = np.diag([f, f, 1]).astype(np.float32)
                    Wf = S @ W @ np.linalg.inv(S)
                    A = cv2.resize(cv2.GaussianBlur(a, (0, 0), sig), None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
                    B = cv2.resize(cv2.GaussianBlur(b, (0, 0), sig), None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
                    cc, Wf = cv2.findTransformECC(A, B, Wf, cv2.MOTION_HOMOGRAPHY, crit, None, 1)
                    W = np.linalg.inv(S) @ Wf @ S
            except cv2.error:
                return None, 0.0
            return (W.astype(np.float64) if cc >= 0.9 else None), float(cc)   # 0.9: heuristic lock threshold for masks

        def apply(H, q):
            return cv2.perspectiveTransform(q.reshape(-1, 1, 2), H).reshape(4, 2)

        def plausible(q, prev):
            a, b = abs(cv2.contourArea(q.astype(np.float32))), abs(cv2.contourArea(prev.astype(np.float32)))
            return cv2.isContourConvex(q.astype(np.float32).reshape(-1, 1, 2)) and 0.6 < a / max(b, 1e-6) < 1.6

        quads, log = [None] * n, [None] * n
        quads[reference_index], log[reference_index] = ref_q, "reference"
        ref_f = feats(reference_index, ref_q)
        cache = {reference_index: ref_f}
        for order in (range(reference_index + 1, n), range(reference_index - 1, -1, -1)):
            for t in order:
                p = t - 1 if t > reference_index else t + 1
                cur = feats(t, quads[p])
                H, k = homog(*ref_f, *cur)
                q = apply(H, ref_q) if H is not None else None
                if q is not None and plausible(q, quads[p]):
                    quads[t], log[t] = q, f"ref:{k}"
                else:
                    H2, k2 = homog(*cache[p], *cur)
                    q2 = apply(H2, quads[p]) if H2 is not None else None
                    if q2 is not None and plausible(q2, quads[p]):
                        quads[t], log[t] = q2, f"chain:{k2} (ref:{k})"
                    else:
                        H3, cc = ecc(p, t, quads[p])
                        q3 = apply(H3, quads[p]) if H3 is not None else None
                        if q3 is not None and plausible(q3, quads[p]):
                            quads[t], log[t] = q3, f"ecc:{cc:.2f} (ref:{k}, chain:{k2})"
                        else:
                            H4, c4 = shape_ecc(p, t)
                            q4 = apply(H4, quads[p]) if H4 is not None else None
                            if q4 is not None and plausible(q4, quads[p]):
                                quads[t], log[t] = q4, f"shape:{c4:.2f} (ref:{k}, chain:{k2}, ecc:{cc:.2f})"
                            else:
                                quads[t], log[t] = quads[p], f"held (ref:{k}, chain:{k2}, ecc:{cc:.2f}, shape:{c4:.2f})"
                cache[t] = cur
        if smooth_frames > 0:
            quads = [np.mean([quads[j] for j in range(max(0, i - smooth_frames), min(n, i + smooth_frames + 1))], axis=0)
                     for i in range(n)]
        return (json.dumps([np.round(q, 2).tolist() for q in quads]), json.dumps(log))


class ArtworkTrack:
    """Tracks a printed flat object (an album sleeve) by aligning its known artwork to every frame.
    The artwork fixes all four corners, so corners hidden by hands or by things in front stay right.
    One frame is located by SIFT matches to the artwork (the frame with the most RANSAC inliers inside the
    object mask); every other frame is aligned by ECC on the artwork, started from its neighbour's result.
    Frames ECC cannot hold (edge-on or motion-blurred) take a hand-set starting quad from keyframes_json,
    refined by ECC when the refinement is trustworthy and used as given otherwise."""
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "frames": ("IMAGE",),
            "artwork": ("IMAGE",),   # the object's own print, upright, cropped to its four corners
            # minimum ECC correlation to accept an alignment. Heuristic: 0.75 separated good fits
            # (0.81-0.97) from drifted ones on the 500 Days of Summer record shot.
            "min_cc": ("FLOAT", {"default": 0.75, "min": 0.0, "max": 1.0, "step": 0.01}),
            # px: an ECC result that moves a keyframe corner further than this is rejected as a wrong basin
            "max_key_shift_px": ("FLOAT", {"default": 40.0, "min": 0.0, "max": 500.0, "step": 1.0}),
            # {"frame index": [[x,y] TL, TR, BR, BL]} starting quads for frames ECC cannot reach from a neighbour
            "keyframes_json": ("STRING", {"default": "{}", "multiline": True}),
        }, "optional": {
            "masks": ("MASK",),             # object mask per frame; limits the SIFT search to the object
            "masks_reversed": ("BOOLEAN", {"default": False}),
            "occluder_masks": ("MASK",),    # hands etc.; excluded from the alignment
            "occluder_masks_reversed": ("BOOLEAN", {"default": False}),
        }}

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("quads_json", "track_log")
    FUNCTION = "run"
    CATEGORY = "planar_track"

    # artwork side in px for the full-resolution ECC pass. Parameter: about the object's on-screen size;
    # finer adds no detail the footage has.
    N = 192
    # (artwork side px, Gaussian sigma px) coarse-to-fine ECC passes. Heuristic: the coarse passes widen the
    # basin for motion-blurred frames.
    PYRAMID = ((64, 2.0), (96, 1.5), (192, 1.0), (192, 1.0))

    def run(self, frames, artwork, min_cc, max_key_shift_px, keyframes_json, masks=None, masks_reversed=False,
            occluder_masks=None, occluder_masks_reversed=False):
        fr = frames.detach().cpu().numpy().astype(np.float32)
        n, h, w, _ = fr.shape
        gray = [cv2.cvtColor(f, cv2.COLOR_RGB2GRAY) for f in fr]
        art = cv2.cvtColor(artwork[0].detach().cpu().numpy().astype(np.float32), cv2.COLOR_RGB2GRAY)
        def load(m, rev, name):
            if m is None:
                return None
            m = m.detach().cpu().numpy().astype(np.float32)
            m = m[None] if m.ndim == 2 else m
            m = m[::-1] if rev else m
            if m.shape != (n, h, w):
                raise ValueError(f"ArtworkTrack: {name} {m.shape} do not match frames {(n, h, w)}")
            return m
        om = load(masks, masks_reversed, "masks")
        occ = load(occluder_masks, occluder_masks_reversed, "occluder_masks")
        # occluders dilated 3 px so their soft edges do not pull the fit
        occ = None if occ is None else np.stack([cv2.dilate((o > 0.5).astype(np.uint8), np.ones((7, 7), np.uint8)) for o in occ])
        keys = {int(k): np.float32(v) for k, v in json.loads(keyframes_json or "{}").items()}
        N = self.N
        sq = np.float32([[0, 0], [N, 0], [N, N], [0, N]])
        log = []

        def ecc(H, i):
            cc = 0.0
            for M, sig in self.PYRAMID:
                A = cv2.getPerspectiveTransform(np.float32([[0, 0], [M, 0], [M, M], [0, M]]), sq)
                Hn = H @ A
                Hi = np.linalg.inv(Hn)
                T = cv2.GaussianBlur(cv2.resize(art, (M, M), interpolation=cv2.INTER_AREA), (0, 0), sig)
                img = cv2.GaussianBlur(cv2.warpPerspective(gray[i], Hi, (M, M)), (0, 0), sig * 0.5)
                ok = cv2.warpPerspective(np.ones((h, w), np.float32), Hi, (M, M)) > 0.99
                if occ is not None:
                    ok &= cv2.warpPerspective(1 - occ[i].astype(np.float32), Hi, (M, M)) > 0.99
                if ok.mean() < 0.1:
                    return H, 0.0
                try:
                    cc, d = cv2.findTransformECC(T, img, np.eye(3, dtype=np.float32), cv2.MOTION_HOMOGRAPHY,
                                                 (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 200, 1e-6), ok.astype(np.uint8), 3)
                except cv2.error:
                    return H, 0.0
                H = Hn @ d @ np.linalg.inv(A)
            return H, float(cc)

        # anchor: SIFT on every frame, inside the object mask's box, at 2x
        sift = cv2.SIFT_create(nfeatures=4000)
        A8 = (np.clip(cv2.resize(art, (2 * N, 2 * N), interpolation=cv2.INTER_CUBIC), 0, 1) * 255).astype(np.uint8)
        ka, da = sift.detectAndCompute(A8, None)
        best = (0, None, None)
        for i in range(n):
            x0, y0, x1, y1 = 0, 0, w, h
            if om is not None and (om[i] > 0.5).any():
                ys, xs = np.nonzero(om[i] > 0.5)
                x0, y0, x1, y1 = max(0, xs.min() - 60), max(0, ys.min() - 60), min(w, xs.max() + 60), min(h, ys.max() + 60)
            c = cv2.resize((gray[i][y0:y1, x0:x1] * 255).astype(np.uint8), None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            kf, df = sift.detectAndCompute(c, None)
            if df is None or len(kf) < 8:
                continue
            ms = [m for m, o in cv2.BFMatcher().knnMatch(da, df, k=2) if m.distance < 0.8 * o.distance]
            if len(ms) < 8:
                continue
            H, inl = cv2.findHomography(np.float32([ka[m.queryIdx].pt for m in ms]) / 2,
                                        np.float32([kf[m.trainIdx].pt for m in ms]) / 2 + [x0, y0], cv2.RANSAC, 2.0)
            if H is not None and inl.sum() > best[0]:
                best = (int(inl.sum()), i, H)
        if best[1] is None:
            raise ValueError("ArtworkTrack: no frame matched the artwork")
        a = best[1]
        H, cc = ecc(best[2], a)
        Hs = {a: H}
        log.append(f"anchor {a}: {best[0]} SIFT inliers, cc {cc:.3f}")

        def from_key(i):
            K = keys[i]
            Hk = cv2.getPerspectiveTransform(sq, K)
            Hr, c = ecc(Hk, i)
            shift = float(np.abs(cv2.perspectiveTransform(sq[None], Hr)[0] - K).max())
            if c >= min_cc and shift <= max_key_shift_px:
                return Hr, f"key+ecc cc {c:.3f} shift {shift:.0f}px"
            return Hk, f"key as given (ecc cc {c:.3f} shift {shift:.0f}px)"

        for rng in (range(a + 1, n), range(a - 1, -1, -1)):
            prev = Hs[a]
            for i in rng:
                Hn, c = ecc(prev, i)
                if c >= min_cc:
                    Hs[i], why = Hn, f"ecc cc {c:.3f}"
                elif i in keys:
                    Hs[i], why = from_key(i)
                else:
                    Hs[i], why = prev, f"held (ecc cc {c:.3f})"
                log.append(f"{i}: {why}")
                prev = Hs[i]
        quads = [np.round(cv2.perspectiveTransform(sq[None], Hs[i])[0], 2).tolist() for i in range(n)]
        return (json.dumps(quads), "\n".join(sorted(log, key=lambda s: (not s.startswith("anchor"), int(s.split(":")[0]) if s[0].isdigit() else -1))))


NODE_CLASS_MAPPINGS = {"MaskToQuads": MaskToQuads, "PlanarTrack": PlanarTrack, "CompositeIntoQuads": CompositeIntoQuads,
                       "ArtworkTrack": ArtworkTrack}
NODE_DISPLAY_NAME_MAPPINGS = {"MaskToQuads": "Mask to Quads (planar track)",
                              "PlanarTrack": "Planar Track (SIFT homography)",
                              "CompositeIntoQuads": "Composite Into Quads (planar track)",
                              "ArtworkTrack": "Artwork Track (align known print)"}
