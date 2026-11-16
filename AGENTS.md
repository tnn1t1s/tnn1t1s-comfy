# Working on tnn1t1s-comfy

The `tnn1t1s_comfy` package talks to ComfyUI and Cue over their HTTP APIs and
nothing else. It takes every service address as an explicit input (`COMFY_URL`,
`CUE_URL` or a command-line flag) and never looks up hosts, calls a Medios
controller or node API, or uses SSH. Resolving addresses belongs to whoever runs
the tool.

Keep the package on the standard library. Workflows, custom node packs, model
lists and inputs belong to the example or the project that uses them, under
`examples/` or in that project's own repository.

A ComfyUI server or GPU node used for testing is usually shared. Do not restart
a service or stop another team's job to test code from this repository. Tests
that call live services must be bounded in time and use unique Cue job IDs.

A cancel request accepted by Cue does not mean the job has stopped; wait for a
terminal state before reporting it stopped.

Run `python3 -m unittest discover -s tests -v` before committing.
