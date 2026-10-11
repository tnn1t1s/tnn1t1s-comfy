# tnn1t1s-comfy

ComfyUI with finite rendering through Cue. Keep the familiar UI; acquire compute
for a render and release it when the task finishes. Pinned upstream ComfyUI is
the runtime; this repository owns the integration and its operational tools.

**Current state:** an isolated gateway now persists prompt/job mappings, runs
finite Cue workers, verifies published outputs, and serves normal queue/history/
file APIs. Live CPU tests passed round trip, gateway restart with one Cue
attempt, and both cancellation routes. The existing production service is
unchanged. Live CPU browser tests now show node progress, decoded previews and
the verified final image; websocket reconnect replays current progress/preview.
Full GPU video and general multi-user behavior are not yet accepted.

## Use now

Python 3.10+ for the CLI, with no added dependencies. The gateway/worker use
the pinned ComfyUI runtime and its aiohttp installation:

```sh
python3 -m venv .venv
.venv/bin/pip install .
export COMFY_URL='http://your-comfy-host:assigned-port'
export CUE_URL='http://your-cue-host:port'
.venv/bin/tnn1t1s-comfy doctor --require-node SaveImage
.venv/bin/tnn1t1s-comfy job YOUR_CUE_JOB_ID
.venv/bin/tnn1t1s-comfy cancel YOUR_CUE_JOB_ID --submitter team:your-team --wait 60
```

`doctor` reads readiness, queue counts and optional node catalogs. It does not
submit work or claim that a responsive API proves CUDA or model readiness.
`job` preserves Cue's waiting/failure explanation and attempt identity. When
Cue has no waiting reason, it says so; it does not guess from cluster state.
`cancel` sends the caller identity and waits for a terminal state. A timeout
exits nonzero: the task may still be running. A completion race reports the
actual result, not a fictitious cancellation.

For a Fennel-hosted service, resolve its current healthy instance instead of
copying a port from yesterday's logs:

```sh
export COMFY_URL="$(python3 deploy/fennel_url.py \
  --fennel-url "$FENNEL_URL" --app /svc/your-comfy-app)"
```

Discovery requires one Running/Healthy instance. Fennel is a deployment option,
not an application dependency. The core calls only ComfyUI and Cue APIs.
There is no Medios client, broker subscription, SSH, or built-in fleet address.

## Ownership and next work

- Here: Cue adapter, prompt/job mapping, worker lifecycle, UI progress/cancel,
  durable artifact contract, diagnostics and optional deployment recipes.
- Consumer repositories: workflows, model manifests, custom node selections,
  inputs and output evaluation. `meme_swap` is the first consumer.
- Fleet configuration: service URLs, image registries, stores and placement.

See [integration roadmap](docs/INTEGRATION.md) for evidence and acceptance gates,
and [optional deployment](deploy/README.md) for the Fennel recipe.
The wrapper must not own a pool of idle GPU workers. Image/model disk caches
may survive task exit without retaining a rendering process.

## Test

```sh
python3 -m unittest discover -s tests -v
```
