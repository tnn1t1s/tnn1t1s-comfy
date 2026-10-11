# Optional Fennel deployment

The application talks only to Cue and ComfyUI. These deployment tools are
optional; the same gateway can be hosted elsewhere. All fleet-specific values
come from an operator-owned JSON profile, not application code.

The profile has two top-level objects:

- `gateway_app`: a Fennel app spec with an explicit id, submitter, instances=1,
  zero GPUs, CPU/memory, digest-pinned container image, store mounts, placement,
  allocated HTTP port, and an HTTP health check on `/cue/status`.
- `gateway_config`: `cue_url`, `submitter`, `journal`, `comfy_dir`, optional
  upstream CLI `args`, `worker_spec` (Cue resources, image, stores and placement),
  and `worker_config` (`comfy_dir`, `input_root`, `output_root`, `timeout`, `args`).

Use the same persistent input/output mount paths in both environments. Put the
journal outside the output root so ComfyUI's `/view` cannot serve it. SQLite
requires a filesystem with working file locks/fsync; only one gateway process
may own a journal. The launcher holds a process lock and disables local queue
consumption even if the remote custom node fails to import.

The base image must already contain the pinned ComfyUI runtime and required
custom nodes. `build_spec.py` adds the versioned Python adapter as a source
bootstrap; its SHA256 is recorded in the app labels. This does not install
packages on the host or require a private source repository at task launch.
Profile `python` defaults to `/opt/venv/bin/python` and is configurable.

```sh
python3 deploy/build_spec.py local/profile.json --output local/gateway.json
ansible-playbook -i localhost, deploy/apply.yml \
  -e fennel_url="$FENNEL_URL" \
  -e expected_app=/svc/YOUR-ISOLATED-GATEWAY \
  -e spec_file="$PWD/local/gateway.json"
```

The recipe compares desired fields, waits for earlier deployment completion,
changes only the explicit application, then checks readiness and rollout
completion. Check mode previews differences. It does not restart unchanged apps.

Never overwrite the accepted rendering service while testing this adapter.
`tests/live/restart.yml` is restricted to the dedicated acceptance app. It tests
journal recovery by scaling that gateway down/up while the Cue job continues.
It can clear stale deployment bookkeeping only after verifying the sole
instance is already healthy on the desired version. This exception is confined
to the live acceptance fixture; normal apply does not silently cancel rollouts.

Current acceptance used a CPU-only base runtime, shared local storage and
`--disable-all-custom-nodes` with the gateway node whitelisted. Consumer model
catalogs/custom packs and the real GPU workflow still require acceptance.

Run the live HTTP probes from the repository root (each submits bounded work):

```sh
PYTHONPATH=. python3 tests/live/roundtrip.py --url "$COMFY_URL" \
  --evidence local/roundtrip.json
PYTHONPATH=. python3 tests/live/recovery.py --fennel-url "$FENNEL_URL" \
  --cue-url "$CUE_URL" --vars local/ansible-vars.json \
  --evidence local/restart-cancel.json
```

The restart probe only targets `/svc/tnn1t1s-comfy-gateway-test`. Its accepted
fixture used a 20-second startup delay before the worker bootstrap to provide a
repeatable interruption window; that delay is not part of build_spec.py's
normal worker command. If a probe fails, inspect the printed prompt ID and
cancel its `comfy-<prompt-id>` Cue job before repeating.


For progress/browser acceptance, build a separate **CPU-only** fixture from an
explicit isolated profile. This installs `CueRelayProbe` only inside the test
gateway and worker; it is not shipped in the application package:

```sh
python3 tests/live/build_progress_spec.py local/profile.json --output local/gateway.json
# Apply this spec with the Ansible invocation above, using the isolated app ID.
python3 tests/live/progress.py --url "$COMFY_URL" --evidence local/progress.json
npm install --prefix local/browser playwright
NODE_PATH="$PWD/local/browser/node_modules" COMFY_URL="$COMFY_URL" \
  node tests/live/browser.cjs
```

The websocket probe needs aiohttp; the browser probe uses installed Google
Chrome. These are test dependencies. Browser evidence defaults to
`local/browser-evidence/` (override `EVIDENCE_DIR`). The fixture runs for about
10 seconds and saves a small image. Both probes request cancellation on failure
and wait for terminal history, reporting an unconfirmed cancellation explicitly.
The worker still has the profile's finite runtime limit.
