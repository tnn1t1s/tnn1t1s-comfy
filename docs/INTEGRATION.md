# Integration boundary and delivery gates

The application uses Cue's job API. It has no knowledge of the resource broker.
Fennel can host the persistent UI/API, with deployment kept separate from the
application. The product name is tnn1t1s-comfy.

```
ComfyUI browser/client -> UI/API gateway -> Cue -> finite ComfyUI task
                             ^                         |
                             +--- progress/artifacts --+
```

Upstream integration reference: ComfyUI commit
`0df64eb242b7c5759c3e86afd5d1846d923b1033`. Preserve its UI protocol through a
queue adapter installed before the local worker starts. Intercepting a prompt
hook alone is insufficient: hook exceptions are caught upstream, and model
validation runs before queue insertion.

## Existing evidence

Original pairing record: https://github.com/tnn1t1s/meme_swap/issues/1.

- Nine upstream queue/validation/cancellation boundary checks passed.
- An isolated CPU gateway submitted a separate Cue task and returned its PNG
  through normal ComfyUI history/file APIs. This was an experimental plugin.
- Success, intentional failure and cancellation released process trees and
  compute; each waiting follower performed real CUDA work. Complete run:
  `ops-release-84ee7e030a`. GPU-seat accounting alone was not used as proof.
- Existing full Wan rendering, model persistence and restart acceptance passed
  on the current long-running service, not yet on finite rendering workers.

Evidence from those experiments does not certify a production gateway.

## Implementation sequence

1. Durable logical-prompt -> Cue-job mapping. Save intent before POST; reconcile
   ambiguous responses using the same ID. Detect conflicting duplicate IDs.
   Save every attempt; retries=0 does not exclude Cue infrastructure retries.
2. Per-attempt input/output directories and manifest publication. A late attempt
   cannot overwrite the selected result. Large artifacts belong in storage;
   stdout contains only a manifest pointer. The UI reports success only after
   validating the published manifest and files.
3. Replace queue/history projections from durable records; prevent local render
   execution in the gateway. Provide truthful model catalogs for validation.
4. Route both job cancellation and legacy /interrupt to the correct Cue job.
   Preserve cancel intent across restart and wait for terminal confirmation.
5. Relay websocket progress and binary previews, including client reconnects.
   Keep credentials out of history, public manifests and diagnostics.
6. Deploy an isolated gateway, test restart during work, worker loss, duplicate
   events, publication failure, uploads and browser behavior. Then run the real
   Wan workload, measure cold startup separately and check output/audio.
7. Move clients after acceptance; retire the old always-on rendering service.

No permanent worker lease/heartbeat pool. If a demonstrated Cue contract gap
appears, discuss it with Cue engineering; infrastructure cleanup failures are
platform bugs, not a reason to invent a second scheduler.

## Current operational tools

The core package accepts explicit COMFY_URL/CUE_URL. Optional Fennel discovery
resolves the assigned port and refuses Scheduled/unhealthy instances. Caller
identity is mandatory for cancellation. Missing waiting diagnostics stay
unknown. These are reusable now without changing the running application.

## Durable gateway slice — 2026-10-10/11

Implemented SQLite intent/cancel journaling, same-ID reconciliation, immutable
per-invocation output directories, SHA256 manifest/artifact validation, queue and
history projection, and cancellation through both `/interrupt` and the job API.
The worker runs one private ComfyUI process, publishes its manifest, stops the
process and exits. The gateway never takes work from the local execution queue.
A loader guard preserves that property if custom-node loading fails.

An isolated zero-GPU Fennel app `/svc/tnn1t1s-comfy-gateway-test` is running.
Live results:
- HTTP prompt -> separate Cue job -> verified PNG -> history/view passed.
- Gateway stopped/restarted while Cue reported its job running; it recovered
  prompt `dacad19f-8bc1-4ce0-8fa9-bc2ef300bd14` with exactly one Cue attempt.
- Targeted legacy interrupt and `/api/jobs/{id}/cancel` both reached cancelled
  backend states and terminal error histories.

The recovery/cancellation fixture deliberately delayed worker startup by 20 s.
This proves recovery/cancellation while the backend task is running, not an
interrupt during a full GPU sampling pass. Evidence: `durable-roundtrip.json`
and `restart-cancel.json` under evidence/.

Live testing found and fixed a race: Cue may report success before its stdout
result capture arrives. The gateway now waits up to 60 seconds for the pointer,
persisting that wait across restart, before validating output. Earlier failed
probe histories remain in the test journal as evidence; they were not relabeled.

Fennel's deployment record also remained Running after the sole instance was
healthy on the desired version. Captured in `fennel-deployment-observation.json`
and reported to Fennel engineering. The test-only restart fixture can cancel
that completed migration's bookkeeping after checking arrival, then scale the
isolated app down/up. Ordinary apply waits and reports a stuck rollout.

Remaining limits: shared local storage; a single gateway journal owner; known
sensitive extra-data fields rejected until transport is configured; no per-node
websocket progress or binary preview relay; no full browser or Wan acceptance.
Input uploads use the shared input directory and are not yet snapshotted per
attempt. No migration of production clients has happened. An ambiguous job that
Cue no longer knows is retained for diagnosis, never silently relaunched.
