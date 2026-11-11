#!/usr/bin/env python3
"""Helpers of tf-runner.sh for nested-qemu presets.

    adapt-preset.py PRESET OUT --rpms DIR
        copy PRESET to OUT with vm.rpms set to the RPMs built on this guest
        (a preset that names its own vm.rpms or vm.srpms is copied unchanged)
    adapt-preset.py --dist PRESET [PRESET ...]
        print the Fedora release of their VM images, nothing if the name has
        none; the RPMs are built for it. Exits 1 if the presets differ.
"""
import argparse
import os
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', '..', '..', 'env'))
from freeipa_env import request  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument('--dist', action='store_true')
    ap.add_argument('--rpms', metavar='DIR')
    ap.add_argument('preset', nargs='+')
    args = ap.parse_args()
    if args.dist:
        try:
            print(request.vm_dist(args.preset) or '')
        except request.RequestError as e:
            print(f'error: {e}', file=sys.stderr)
            return 1
        return 0
    if len(args.preset) != 2 or not args.rpms:
        ap.error('PRESET OUT --rpms DIR')
    src, out = args.preset
    with open(src) as f:
        doc = yaml.safe_load(f)
    with open(out, 'w') as f:
        yaml.safe_dump(request.adapt(doc, rpms_dir=args.rpms), f,
                       sort_keys=False)
    return 0


if __name__ == '__main__':
    sys.exit(main())
