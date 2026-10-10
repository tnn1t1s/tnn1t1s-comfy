# Working on tnn1t1s-comfy

This is a ComfyUI integration whose execution backend is Cue. The application
must not call a Medios controller/node API, subscribe as a framework, use SSH,
or embed fleet addresses. Cue handles placement and execution. Keep Cue's
opaque placement/store fields as deployment configuration, not broker logic.

Fennel is an optional host for the UI/API service. Keep its deployment and
discovery helpers under deploy/, outside the application runtime package.
COMFY_URL and CUE_URL are explicit inputs. Resolve them in the deployment
layer; no dependency on a particular cluster's endpoint utility in core code.

Use pinned upstream ComfyUI and extension points. Workflows, custom node packs,
and model manifests belong to application profiles, not a hidden meme-swap
assumption. Existing service deployment must not change during investigation.

No idle warm render workers. A finite Cue task owns each render attempt and
exits after durable output publication. UI/API stays available independently.
Cancellation requested is not termination confirmed. Cue infrastructure retries
may occur with retries=0. Never claim exactly-once rendering.

Run python3 -m unittest discover -s tests -v. Integration probes must be bounded,
have unique job IDs, and record terminal outcomes. Never stop another team's
render to test this package. Keep verified capabilities separate from milestones.
