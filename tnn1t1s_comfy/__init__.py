"""HTTP clients and a command-line tool for working with a ComfyUI server and Cue jobs.

client  Thin clients for the ComfyUI and Cue HTTP APIs, a readiness check for a
        ComfyUI server, and a plain summary of a Cue job's state.
cli     The ``tnn1t1s-comfy`` command: ``doctor``, ``job`` and ``cancel``.

The package uses only the Python standard library. Every service address is
passed in by the caller; nothing here looks up hosts or ports.
"""
