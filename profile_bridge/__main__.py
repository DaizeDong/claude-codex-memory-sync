"""JSON ownership verification and path remapping; no implicit filesystem reads."""
import argparse
import json
import sys
from . import ownership


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("verify", "remap"))
    parser.add_argument("--kind", required=True)
    args = parser.parse_args(argv)
    try:
        request = json.load(sys.stdin)
        document = bytes.fromhex(request["document_hex"])
        if args.command == "verify":
            result = ownership.verify(document, args.kind)
        else:
            result = {"status": "remapped", "document_hex": ownership.remap(document, args.kind, request["mapping"]).hex()}
    except (ValueError, KeyError, TypeError):
        result = {"status": "conflict", "reason": "invalid_ownership_request"}
    print(json.dumps(result, sort_keys=True))
    return 2 if result["status"] == "conflict" else 0


if __name__ == "__main__":
    sys.exit(main())
