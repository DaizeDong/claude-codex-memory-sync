"""Verify a reviewed lock and install only its offline wheel closure."""
import json
from pathlib import Path

from runtime_artifacts import (
    ArtifactArgumentParser, failure_report, install, recover_installation,
    reviewed_lock, validate_lock, resolve_installation,
)
import runtime_launch


def main():
    parser = ArtifactArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--expected-lock-sha256")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--verify-only", action="store_true")
    modes.add_argument("--recover", action="store_true")
    modes.add_argument("--resolve", action="store_true")
    # Publish <root>/launch.py (the stable entry) into a root installed before it existed.
    modes.add_argument("--publish-launcher", action="store_true")
    try:
        args = parser.parse_args()
        if args.resolve:
            if args.root is None or any((args.lock, args.bundle, args.expected_lock_sha256)):
                raise ValueError('resolution requires only root')
            report = resolve_installation(args.root)
        elif args.publish_launcher:
            if args.root is None or any((args.lock, args.bundle, args.expected_lock_sha256)):
                raise ValueError('launcher publication requires only root')
            runtime_launch.resolve(args.root)  # only a root with a verifiable active generation
            report = {"launcher": runtime_launch.publish(args.root), "runtime_ready": False}
        elif args.recover:
            if args.root is None or any((args.lock, args.bundle, args.expected_lock_sha256)):
                raise ValueError('recovery requires only root')
            report = recover_installation(args.root)
        elif not all((args.lock, args.bundle, args.expected_lock_sha256)):
            raise ValueError('lock, bundle and digest required')
        elif args.verify_only:
            lock = reviewed_lock(args.lock, args.expected_lock_sha256)
            validate_lock(lock, args.bundle)
            report = {"artifacts_verified": True, "runtime_ready": False,
                      "online_required": lock["online_required"]}
        else:
            if args.root is None:
                parser.error("--root is required for installation")
            receipt = install(args.lock, args.bundle, args.root, args.expected_lock_sha256)
            report = {k: v for k, v in receipt.items() if k != "files"}
        print(json.dumps(report, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps({"artifacts_installed": False, "runtime_ready": False, **failure_report(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
