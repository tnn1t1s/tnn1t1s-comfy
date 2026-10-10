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
