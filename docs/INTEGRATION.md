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
interrupt during a full GPU sampling pass. `tests/live/roundtrip.py` and
`tests/live/recovery.py` repeat these runs and write their records under `local/`.

Live testing found and fixed a race: Cue may report success before its stdout
result capture arrives. The gateway now waits up to 60 seconds for the pointer,
persisting that wait across restart, before validating output. Earlier failed
probe histories remain in the test journal as evidence; they were not relabeled.

Fennel's deployment record also remained Running after the sole instance was
healthy on the desired version. This was reported to Fennel engineering. The test-only restart fixture can cancel
that completed migration's bookkeeping after checking arrival, then scale the
isolated app down/up. Ordinary apply waits and reports a stuck rollout.

Remaining limits: shared local storage; a single gateway journal owner; known
sensitive extra-data fields rejected until transport is configured; no full Wan
acceptance. Browser evidence below uses a small CPU fixture.
Input uploads use the shared input directory and are not yet snapshotted per
attempt. No migration of production clients has happened. An ambiguous job that
Cue no longer knows is retained for diagnosis, never silently relaunched.


## Live progress and browser slice — 2026-10-11

Workers connect to their private ComfyUI websocket before submitting a graph,
negotiating `supports_preview_metadata` with the pinned upstream. A bounded
latest-state snapshot in shared storage carries node/progress events and the
already encoded preview frame. The gateway relays it to the submitting client's
socket. A replacement socket receives the latest state; no idle render worker
is retained. Intermediate telemetry is replaceable, not an event audit log.
A per-job filesystem lock prevents two invocations writing live telemetry at
once. This requires working filesystem locks, as does the gateway journal.

Completion is separate: the gateway emits final node outputs only after Cue is
terminal and the manifest/files pass verification. An upstream progress or
completion message cannot mark the job successful.

Run records are written under `local/` (see `deploy/README.md`):
- `tests/live/progress.py`: real finite CPU worker, progress and binary preview,
  socket disconnect/reconnect, then successful verified publication.
- `tests/live/browser.cjs`: Chrome
  using the pinned frontend, visible node progress and decoded preview, followed
  by a decoded 32x32 final image on SaveImage. The fixture produces previews
  without requiring a model or GPU; this does not certify Wan previews.

Browser testing caught a protocol mismatch that a binary-frame check missed:
legacy previews arrived but did not display. Negotiating metadata fixed it.
Deployment testing also caught an exec environment-size failure; source bundles
are now gzip-compressed deterministically, and the generator rejects command or
environment strings over 64 KiB before deployment.

The known Fennel deployment bookkeeping issue (#154) recurred after healthy
rollouts. Isolated test bookkeeping was cleared only after checking the sole
replacement was healthy on the requested version. The production Comfy service
was not modified. Full GPU acceptance needs a coordinated memory handoff from
that service before a realistically sized finite worker can fit.


## Full GPU acceptance — 2026-10-11

Passed on the isolated gateway using the application-owned Wan profile and the
same pinned ARM64 image/model cache as the accepted service:

- Prompt `8a7258d7-0d88-49ad-b497-718828224caf`: 848x480, 80 frames,
  16 fps, two sampling passes, seed 1. Gateway wall time 385.35 seconds;
  private ComfyUI execution log reports 374.02 seconds. Twelve live previews.
- H.264 video and AAC audio are both 5.00 seconds. Decoded video **and audio**
  hashes match the earlier accepted reference exactly. Three frames were also
  inspected. No weight download messages appeared in the worker log.
- Prompt `20f4ba90-1bbe-466e-8fe0-5385064b7ec6` was cancelled after its first
  sampling step. Confirmed terminal cancellation followed in 2.57 seconds.
  The worker/private ComfyUI processes disappeared, and CPU/memory/GPU/store
  accounting returned to its pre-run baseline after success and cancellation.
- A new task requesting the same 4 CPUs, 65536 MiB and one GPU seat then executed
  a CUDA tensor operation and returned `CUDA_REUSE_PASS`.

`deploy/acceptance/gpu.py` repeats this run and writes its report to the
directory given by `--evidence-dir`; it and the Ansible capacity-window recipe
are under `deploy/acceptance/`. Application
workflow/model selection remains outside the core package.

The real graph exposed input references in ComfyUI loader history. The old
publisher rejected them after a completed render. The fix retains an
attempt-owned input copy and validates it with the other published artifacts;
regression tests cover retention and path confinement. This is post-render
retention, not a claim of immutable pre-launch input staging. The video verifier
selects the explicit save node so a loader's source clip cannot masquerade as
the rendered result.

The legacy service was restored healthy at its original endpoint with identical
runtime configuration. Its uploads and prior output were restored and hash
checked. Its normal in-memory history resets on restart; user-directory backups
were retained without overwriting a live database. The new gateway's history
remains journaled. No production-client migration is claimed by this acceptance.
