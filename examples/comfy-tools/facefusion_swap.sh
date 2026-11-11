#!/bin/sh
# Runs inside the meme-swap-comfy image on spark: FaceFusion 3.9.2 headless swap of the two faces in a clip,
# picked by left/right position, one pass per face.
#   facefusion_swap.sh <target.mp4> <left-face> <right-face> <out-dir> <swap-model> <pixel-boost>
# FaceFusion has its own venv on the store because its pins (numpy, opencv, onnxruntime) differ from ComfyUI's.
# Installed once; models download and hash-check into the FaceFusion checkout on first use.
set -eu
O=/models/meme-swap/overlay
export PATH=$O/bin:$PATH  # static ffmpeg; the image has none
FF=$O/facefusion
V=$O/facefusion-venv
target=$1 left=$2 right=$3 out=$4 model=$5 boost=$6
mkdir -p "$out"
[ -d "$FF/.git" ] || { echo "facefusion_swap: $FF missing; clone facefusion 3.9.2 there" >&2; exit 1; }
if [ ! -f "$V/.installed" ]; then
  t0=$(date +%s)
  python3 -m venv "$V"
  (cd "$FF" && PIP_CACHE_DIR=$O/pip-cache PATH="$V/bin:$PATH" "$V/bin/python" install.py cuda@13 --skip-conda)
  touch "$V/.installed"
  echo "install_seconds $(( $(date +%s) - t0 ))"
fi
swap() {  # swap <source face> <selector order> <in video> <out video>
  t0=$(date +%s)
  (cd "$FF" && "$V/bin/python" facefusion.py headless-run --temp-path /tmp/ff --jobs-path /tmp/ff-jobs \
     --execution-providers cuda --processors face_swapper \
     --face-swapper-model "$model" --face-swapper-pixel-boost "$boost" \
     --face-selector-mode one --face-selector-order "$2" \
     --output-video-quality 95 -s "$1" -t "$3" -o "$4")
  echo "pass $2 seconds $(( $(date +%s) - t0 ))"
}
swap "$left" left-right "$target" "$out/pass-left.mp4"
swap "$right" right-left "$out/pass-left.mp4" "$out/swap.mp4"
rm -f "$out/pass-left.mp4"
