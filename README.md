# tnn1t1s-comfy

Tools and worked examples for running ComfyUI workflows on a ComfyUI server you
already operate. The `tnn1t1s_comfy` package holds HTTP clients for ComfyUI and
Cue and a small command, `tnn1t1s-comfy`, for checking a server and managing Cue
jobs. [`examples/`](examples/) holds case studies, each with its own node pack,
workflow script, inputs and write-up.

## Install

Python 3.10 or later; the package has no dependencies outside the standard
library.

```sh
python3 -m venv .venv
.venv/bin/pip install .
```

## Command

```sh
export COMFY_URL='http://your-comfy-host:8188'
export CUE_URL='http://your-cue-host:12321'
.venv/bin/tnn1t1s-comfy doctor --require-node SaveImage
.venv/bin/tnn1t1s-comfy job YOUR_CUE_JOB_ID
.venv/bin/tnn1t1s-comfy cancel YOUR_CUE_JOB_ID --submitter team:your-team --wait 60
```

`doctor` checks that the ComfyUI server answers and reports how many prompts are
running and waiting. With `--require-node` it also confirms that those node
classes are installed. A server that answers has not yet shown that its models
load or that a GPU render works, and the output says so.

`job` prints a Cue job's state, its attempts and the reason Cue gives for it.
When Cue gives no reason for a waiting job, the output says the reason is
unknown.

`cancel` asks Cue to cancel the job on behalf of the named team and waits up to
`--wait` seconds for it to stop. If it is still running at the timeout the
command exits with code 1, because the job may still be using its resources. If
the job finished before the cancel arrived, the command prints the real result.

For a ComfyUI server that runs as a Fennel service, set `COMFY_URL` to the
service's load-balancer address and service port. That address stays the same
when Fennel moves or restarts the service.

## Examples

[Record swap](examples/record-swap/) replaces an album sleeve in a three-shot
film clip with planar tracking and compositing nodes.

## Test

```sh
python3 -m unittest discover -s tests -v
```
