"""The ``tnn1t1s-comfy`` command.

doctor   Check that a ComfyUI server answers, show its queue, and with
         ``--require-node`` confirm that given node classes are installed.
job      Print a Cue job's state and the reason Cue gives for it.
cancel   Cancel a Cue job on behalf of a team and wait for it to stop.
run      Upload input files, submit an API-format workflow graph to ComfyUI
         and download its outputs.

The ComfyUI address comes from ``--comfy-url`` or ``COMFY_URL`` and the Cue
address from ``--cue-url`` or ``CUE_URL``; a command that needs one exits with
an error when it is missing. Output is JSON on stdout. The exit code is 0 on
success and 1 when a check fails, a call errors, or a cancel times out.
"""
import argparse
import json
import os
import sys
from .client import Cue, doctor, explain


def main(argv=None):
    parser = argparse.ArgumentParser(description='Check a ComfyUI server and manage Cue jobs')
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('doctor', help='Read-only readiness and queue checks')
    check.add_argument('--comfy-url', default=os.getenv('COMFY_URL'))
    check.add_argument('--cue-url', default=os.getenv('CUE_URL'))
    check.add_argument('--require-node', action='append', default=[])
    for name in ('job', 'cancel'):
        command = commands.add_parser(name)
        command.add_argument('id')
        command.add_argument('--cue-url', default=os.getenv('CUE_URL'))
        if name == 'cancel':
            command.add_argument('--submitter', required=True)
            command.add_argument('--wait', type=float, default=60,
                                 help='Seconds to wait for confirmed termination; timeout exits nonzero')
    submit = commands.add_parser('run', help='Upload input files, submit an API-format graph, download outputs')
    submit.add_argument('graph', help='ComfyUI API-format graph JSON')
    submit.add_argument('--comfy-url', default=os.getenv('COMFY_URL'))
    submit.add_argument('--input', action='append', default=[], metavar='NODE[.FIELD]=PATH',
                        help='Upload PATH and bind it to a loader node; repeatable')
    submit.add_argument('--set', action='append', default=[], metavar='NODE.FIELD=TEXT',
                        help='Set a string input, e.g. a prompt; repeatable')
    submit.add_argument('--out', required=True, help='Directory for run.json and downloaded outputs')
    submit.add_argument('--wait', type=float, default=1800,
                        help='Seconds to wait for terminal history; timeout exits nonzero')
    args = parser.parse_args(argv)
    try:
        if args.command == 'run':
            if not args.comfy_url:
                parser.error('Set COMFY_URL or --comfy-url')
            from .run import run
            result = run(args.comfy_url, args.graph, args.input, args.set, args.out, args.wait)
            print(json.dumps(result, indent=2))
            return 0 if result['status'] == 'success' else 1
        if args.command == 'doctor':
            if not args.comfy_url:
                parser.error('Set COMFY_URL or --comfy-url')
            result = doctor(args.comfy_url, args.cue_url, args.require_node)
            exit_code = 0 if result['ok'] else 1
        else:
            if not args.cue_url:
                parser.error('Set CUE_URL or --cue-url')
            cue = Cue(args.cue_url)
            if args.command == 'cancel':
                if args.wait <= 0:
                    parser.error('--wait must be positive')
                cue.cancel(args.id, args.submitter)
                job = cue.wait(args.id, args.wait)
            else:
                job = cue.job(args.id)
            result, exit_code = explain(job), 0
        print(json.dumps(result, indent=2))
        return exit_code
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
