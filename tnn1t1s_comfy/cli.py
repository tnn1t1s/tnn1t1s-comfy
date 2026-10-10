"""Explicit service URLs; discovery belongs to the deployment environment."""
import argparse
import json
import os
import sys
from .client import Cue, doctor, explain


def main(argv=None):
    parser = argparse.ArgumentParser(description='Inspect ComfyUI and manage finite Cue jobs')
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
    args = parser.parse_args(argv)
    try:
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
