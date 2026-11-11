#!/usr/bin/env python3
"""Copy a nested-qemu preset with vm.rpms set to the RPMs built on this guest.

    adapt-preset.py PRESET OUT --rpms DIR

tf-runner.sh runs the copy instead of PRESET. A preset that names its own
vm.rpms or vm.srpms is copied unchanged.
"""
import argparse
import os
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', '..', '..', 'env'))
from freeipa_env import request  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('preset')
    ap.add_argument('out')
    ap.add_argument('--rpms', required=True, metavar='DIR')
    args = ap.parse_args()
    with open(args.preset) as f:
        doc = yaml.safe_load(f)
    with open(args.out, 'w') as f:
        yaml.safe_dump(request.adapt(doc, rpms_dir=args.rpms), f,
                       sort_keys=False)


if __name__ == '__main__':
    main()
