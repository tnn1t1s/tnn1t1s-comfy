# Full GPU acceptance (Ops only)

These probes use operator APIs and SSH to verify accounting and process cleanup.
They are optional deployment acceptance tooling, not application runtime code.
The gateway and worker package still talk only to Cue and ComfyUI.

Use an application-owned API graph, fixed input files and a pinned runtime/model
profile. `gpu.py` uploads inputs under a fresh prompt prefix, runs the full graph,
cancels a second run only after a sampler reports its first completed step,
checks process disappearance and the CPU/memory/GPU/store ledger, then submits a
same-size CUDA follower. It writes report.json and the video to the evidence
directory. It requires aiohttp and SSH access suitable for read-only `/proc`
inspection. The worker-spec JSON must include the operator's submitter identity.

```sh
python3 deploy/acceptance/gpu.py \
  --comfy-url "$COMFY_URL" --cue-url "$CUE_URL" \
  --controller-url "$CONTROLLER_URL" \
  --graph path/to/consumer-graph.json --output-node save_video --inputs-dir path/to/fixed-inputs \
  --worker-spec path/to/worker-spec.json --evidence-dir local/gpu-evidence \
  --ssh-host "$WORKER_HOST" --ssh-user "$WORKER_USER"
```

The profile's finite task timeout must cover the complete render. Sampler
recognition includes KSampler, KSamplerAdvanced, SamplerCustom and
SamplerCustomAdvanced. This fixture expects a rendered MP4 from the explicit output node, so loader
input previews cannot be mistaken for the rendered result. Inspect its video,
audio and representative frames separately; a terminal state is insufficient.

If the existing rendering service reserves the required capacity, coordinate
an idle-only window. `window.yml` takes `window_action=open|restore`, explicit
`fennel_url`, `legacy_app`, `legacy_url`, `legacy_host`, `legacy_pid`,
`legacy_comfy_dir`, `backup_host_dir`, and SSH `ansible_user`. It refuses an
occupied queue, backs up input/output/user directories, scales the old service
to zero, and waits for release. Restoration scales it back to one, waits for
health and restores input/output files. It retains the user directory archive
for recovery, but does not overwrite a live SQLite database. The process match
currently targets the pinned `/opt/venv/bin/python main.py --listen 0.0.0.0 --port`
launcher; adapt and review that match for another image.

Always put restoration in an outer shell EXIT trap or a try/finally around the
acceptance runner. A failed acceptance does not authorize leaving the original
service down. Preserve the initial service spec and verify its configuration,
restored endpoint and files afterward. Hold client submissions during the window.

Profile preparation commands belong to deployment configuration. `build_spec.py`
accepts optional `gateway_prepare` and `worker_prepare` shell snippets, executed
inside the pinned image before the bootstrap. Use them for the consumer's model
path/cache initialization; never embed consumer assumptions in the core worker.

Validate the rendered output explicitly (not a loader's input preview):

```sh
python3 deploy/acceptance/verify_video.py --comfy-url "$COMFY_URL" \
  --prompt-id "$PROMPT_ID" --output-node save_video \
  --source path/to/source.mp4 --reference path/to/previous-accepted.mp4 \
  --output-dir local/video-verification
```

The verifier downloads the chosen output, checks video/audio shape, records
encoded and decoded hashes plus audio levels, and extracts representative
frames for visual inspection. Its three-frame sheet targets the 80-frame
acceptance clip; adapt those indices for another clip length. Matching decoded
hashes against the accepted reference is stronger evidence than file size or
exit status. A differing hash requires inspection; it is not automatically a
quality failure.
