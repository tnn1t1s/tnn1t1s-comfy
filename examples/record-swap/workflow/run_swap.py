"""Record swap on a ComfyUI server with the ComfyUI-PlanarTrack nodes and the SAM2 nodes installed.
SAM2 masks the sleeve and the hands in shots 1 and 3, ArtworkTrack aligns the original sleeve's artwork to every
frame for its four corners, and CompositeIntoQuads puts the new art in front of the sleeve and behind the hands.
Shot 1 shows the new back cover until the sleeve swings upright. Shot 2 composites the new back into the corners
measured by measure_s2.py, blurred to the shot's defocus.
Uploads the inputs, queues the workflow, waits, downloads frames, masks and corner JSON into OUT_DIR.
Usage: COMFY_URL=http://host:port run_swap.py SRC.mp4 FRONT.jpg BACK.jpg OUT_DIR"""
import json, os, sys, time, urllib.request, uuid

COMFY = os.environ.get("COMFY_URL") or sys.exit("run_swap: set COMFY_URL to the ComfyUI server, e.g. http://host:8188")
src, cover, back, out = sys.argv[1:5]
BLUR = 0.8    # px, Gaussian sigma on the new art in shots 1 and 3: the in-focus sleeve's softness
GRADE = 0.5   # 0..1, share of the way the art's colour moves to the footage's; 0.3 left the green front too bright
S2_BLUR = 9.0  # px: shot 2's top edge rises 10-90% over about 24 px, which is 2.56 sigma of a Gaussian
here = os.path.dirname(os.path.abspath(__file__))
ORIGINAL_ART = os.path.join(here, "..", "art", "ringo-front.jpg")   # the sleeve in the footage: Ringo Starr, Stop and Smell the Roses
# Shot 1, frames 0-9: starting corners read by hand (the sleeve is edge-on, then motion-blurred), refined by ECC
S1_KEYS = open(os.path.join(here, "keys_s1.json")).read()
# Shot 1 frames before this one show the back cover: the sleeve is edge-on and rotated until it swings upright
# in the motion blur of frames 7-10, and frame 9 is the most blurred.
BACK_UNTIL = 9
os.makedirs(out, exist_ok=True)

# Shots in the trimmed clip (frame index ranges, from the cut analysis of frames 77-155 of the download).
# Shot 2 (Zooey's close-up, frames 34-53) shows the sleeve's back out of focus; it is handled after the loop.
SHOTS = {
    "s1": {"start": 0, "n": 34, "reverse": True,     # SAM2 runs from the last frame, where the cover faces camera
           "pos": [[760, 250], [815, 250], [745, 330], [785, 300], [740, 345], [797, 260], [800, 300]], "neg": [[810, 350], [835, 340], [900, 300]],
           "hand_pos": [[810, 350], [835, 345], [795, 345], [830, 375]], "hand_neg": [[770, 280], [800, 300], [880, 300]]},
    "s3": {"start": 54, "n": 25, "reverse": False,
           "pos": [[700, 330], [930, 420], [800, 470], [760, 400], [780, 515], [690, 330]],
           "neg": [[660, 480], [690, 520], [960, 450], [985, 500]],
           "hand_pos": [[665, 470], [700, 500], [650, 520], [965, 440], [975, 480]], "hand_neg": [[800, 400], [700, 400], [850, 520]]},
}


def upload(path):
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{os.path.basename(path)}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + open(path, "rb").read() + \
           f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n--{boundary}--\r\n".encode()
    r = urllib.request.Request(f"{COMFY}/upload/image", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return json.load(urllib.request.urlopen(r, timeout=120))["name"]


def pts(p):
    return json.dumps([{"x": x, "y": y} for x, y in p])


vid, img, bimg, oimg = upload(src), upload(cover), upload(back), upload(ORIGINAL_ART)
g = {
    "1": {"class_type": "LoadVideo", "inputs": {"file": vid}},
    "2": {"class_type": "GetVideoComponents", "inputs": {"video": ["1", 0]}},
    "3": {"class_type": "DownloadAndLoadSAM2Model",
          "inputs": {"model": "sam2.1_hiera_large.safetensors", "segmentor": "video", "device": "cuda", "precision": "bf16"}},
    "4": {"class_type": "LoadImage", "inputs": {"image": img}},
    "5": {"class_type": "LoadImage", "inputs": {"image": bimg}},
    "6": {"class_type": "LoadImage", "inputs": {"image": oimg}},
}
done = {}
for k, s in SHOTS.items():
    g[f"{k}_range"] = {"class_type": "GetImageRangeFromBatch", "inputs": {"images": ["2", 0], "start_index": s["start"], "num_frames": s["n"]}}
    feed = [f"{k}_range", 0]
    if s["reverse"]:
        g[f"{k}_rev"] = {"class_type": "ReverseImageBatch", "inputs": {"images": feed}}
        feed = [f"{k}_rev", 0]
    g[f"{k}_pos"] = {"class_type": "StringConstant", "inputs": {"string": pts(s["pos"])}}
    g[f"{k}_neg"] = {"class_type": "StringConstant", "inputs": {"string": pts(s["neg"])}}
    g[f"{k}_add"] = {"class_type": "Sam2VideoSegmentationAddPoints",
                     "inputs": {"sam2_model": ["3", 0], "coordinates_positive": [f"{k}_pos", 0], "coordinates_negative": [f"{k}_neg", 0],
                                "frame_index": 0, "object_index": 0, "image": feed}}
    g[f"{k}_seg"] = {"class_type": "Sam2VideoSegmentation",
                     "inputs": {"sam2_model": [f"{k}_add", 0], "inference_state": [f"{k}_add", 1], "keep_model_loaded": True}}
    g[f"{k}_hp"] = {"class_type": "StringConstant", "inputs": {"string": pts(s["hand_pos"])}}
    g[f"{k}_hn"] = {"class_type": "StringConstant", "inputs": {"string": pts(s["hand_neg"])}}
    g[f"{k}_hadd"] = {"class_type": "Sam2VideoSegmentationAddPoints",
                      "inputs": {"sam2_model": ["3", 0], "coordinates_positive": [f"{k}_hp", 0], "coordinates_negative": [f"{k}_hn", 0],
                                 "frame_index": 0, "object_index": 0, "image": feed}}
    g[f"{k}_hseg"] = {"class_type": "Sam2VideoSegmentation",
                      "inputs": {"sam2_model": [f"{k}_hadd", 0], "inference_state": [f"{k}_hadd", 1], "keep_model_loaded": True}}
    g[f"{k}_himg"] = {"class_type": "MaskToImage", "inputs": {"mask": [f"{k}_hseg", 0]}}
    g[f"{k}_hsave"] = {"class_type": "SaveImage", "inputs": {"images": [f"{k}_himg", 0], "filename_prefix": f"record-swap/{k}_hand"}}
    g[f"{k}_quads"] = {"class_type": "ArtworkTrack",
                       "inputs": {"frames": [f"{k}_range", 0], "artwork": ["6", 0], "min_cc": 0.75, "max_key_shift_px": 40.0,
                                  "keyframes_json": S1_KEYS if k == "s1" else "{}",
                                  "masks": [f"{k}_seg", 0], "masks_reversed": s["reverse"],
                                  "occluder_masks": [f"{k}_hseg", 0], "occluder_masks_reversed": s["reverse"]}}
    g[f"{k}_quadtext"] = {"class_type": "PreviewAny", "inputs": {"source": [f"{k}_quads", 0]}}
    g[f"{k}_logtext"] = {"class_type": "PreviewAny", "inputs": {"source": [f"{k}_quads", 1]}}
    g[f"{k}_comp"] = {"class_type": "CompositeIntoQuads",
                      "inputs": {"frames": [f"{k}_range", 0], "image": ["4", 0], "quads_json": [f"{k}_quads", 0],
                                 "blur_sigma_px": BLUR, "grade_match": GRADE, "feather_px": 1.0, "grain_match": 1.0,
                                 "masks": [f"{k}_seg", 0], "masks_reversed": s["reverse"],
                                 "occluder_masks": [f"{k}_hseg", 0], "occluder_masks_reversed": s["reverse"], "grow_px": 3.0,
                                 **({"lead_image": ["5", 0], "lead_frames": BACK_UNTIL} if k == "s1" else {})}}
    g[f"{k}_maskimg"] = {"class_type": "MaskToImage", "inputs": {"mask": [f"{k}_comp", 1]}}
    g[f"{k}_savemask"] = {"class_type": "SaveImage", "inputs": {"images": [f"{k}_maskimg", 0], "filename_prefix": f"record-swap/{k}_mask"}}
g["s2_range"] = {"class_type": "GetImageRangeFromBatch", "inputs": {"images": ["2", 0], "start_index": 34, "num_frames": 20}}
g["s2_quads"] = {"class_type": "StringConstant", "inputs": {"string": open(os.path.join(here, "quads_s2.json")).read()}}
# No masks: nothing passes in front of the sleeve in this shot, so the art fills the measured quad.
g["s2_comp"] = {"class_type": "CompositeIntoQuads",
                "inputs": {"frames": ["s2_range", 0], "image": ["5", 0], "quads_json": ["s2_quads", 0],
                           "blur_sigma_px": S2_BLUR, "grade_match": GRADE, "feather_px": 3.0, "grain_match": 1.0, "grow_px": 0.0}}
g["b1"] = {"class_type": "ImageBatch", "inputs": {"image1": ["s1_comp", 0], "image2": ["s2_comp", 0]}}
g["b2"] = {"class_type": "ImageBatch", "inputs": {"image1": ["b1", 0], "image2": ["s3_comp", 0]}}
g["save"] = {"class_type": "SaveImage", "inputs": {"images": ["b2", 0], "filename_prefix": "record-swap/frame"}}

r = urllib.request.Request(f"{COMFY}/prompt", json.dumps({"prompt": g}).encode(), {"Content-Type": "application/json"})
try:
    pid = json.load(urllib.request.urlopen(r, timeout=30))["prompt_id"]
except urllib.error.HTTPError as e:
    sys.exit(f"queue refused: {e.read().decode()[:2000]}")
print("queued", pid, flush=True)
t0 = time.time()
while True:
    h = json.load(urllib.request.urlopen(f"{COMFY}/history/{pid}", timeout=30)).get(pid)
    if h and h.get("status", {}).get("completed") is not None:
        break
    if h and h.get("status", {}).get("status_str") == "error":
        break
    print(f"t+{time.time() - t0:.0f}s running", flush=True)
    time.sleep(10)
st = h["status"]
if st.get("status_str") != "success":
    for m in st.get("messages", []):
        if m[0] == "execution_error":
            print(json.dumps(m[1])[:3000])
    sys.exit("workflow failed")
print(f"done in {time.time() - t0:.0f}s", flush=True)
for node, o in h["outputs"].items():
    for im in o.get("images", []):
        q = f"filename={im['filename']}&subfolder={im['subfolder']}&type={im['type']}"
        urllib.request.urlretrieve(f"{COMFY}/view?{q}", os.path.join(out, im["filename"]))
    if "text" in o:
        open(os.path.join(out, f"{node}.json"), "w").write(o["text"][0] if isinstance(o["text"], list) else o["text"])
print("downloaded to", out)
