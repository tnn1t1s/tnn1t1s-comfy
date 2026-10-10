#!/usr/bin/env python3
"""Optional Fennel discovery. No cluster addresses or app IDs built in."""
import argparse
import json
from urllib.parse import quote
from urllib.request import urlopen


def resolve(app):
    urls = []
    for instance in app.get('instances', []):
        if instance.get('goal') != 'Running' or instance.get('condition') != 'Running':
            continue
        if instance.get('health') != 'Healthy':
            continue
        ports = instance.get('ports', [])
        host = instance.get('agent_info', {}).get('host')
        if host and ports:
            if ':' in host:
                host = '[' + host.strip('[]') + ']'
            urls.append(f'http://{host}:{ports[0]}')
    if len(urls) != 1:
        raise ValueError(f'Expected one healthy ComfyUI instance, found {len(urls)}; inspect the Fennel app')
    return urls[0]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--fennel-url', required=True)
    p.add_argument('--app', required=True)
    args = p.parse_args()
    url = args.fennel_url.rstrip('/') + '/v2/apps/' + quote(args.app.lstrip('/'), safe='/')
    with urlopen(url, timeout=10) as response:
        print(resolve(json.load(response)))


if __name__ == '__main__':
    main()
