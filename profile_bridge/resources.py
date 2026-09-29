"""Resolve local resource references from the canonical source, without writes."""
import argparse
import json
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, urlsplit


class ResourceError(ValueError):
    """A stable reason for withholding an unresolved or out-of-root path."""


def _canonical(path, label):
    try:
        return Path(path).resolve(strict=True)
    except FileNotFoundError as exc:
        raise ResourceError(label + '_missing') from exc
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ResourceError(label + '_unresolvable') from exc


def describe_entrypoint(entrypoint, source_root):
    """Describe the canonical base within a caller-approved source boundary."""
    root = _canonical(source_root, 'source_root')
    if not root.is_dir():
        raise ResourceError('source_root_not_directory')
    source = _canonical(entrypoint, 'entrypoint')
    if not source.is_relative_to(root):
        raise ResourceError('entrypoint_outside_source_root')
    if not source.is_file():
        raise ResourceError('entrypoint_not_file')
    return {'canonical_entrypoint': str(source), 'base_directory': str(source.parent),
            'source_root': str(root)}


def resolve_resource(entrypoint, reference, *, source_root):
    """Resolve a file reference, allowing siblings only inside the approved root.

    The entrypoint is canonicalized before joining any parent components.
    Canonicalizing the result also rejects a resource symlink escaping the root.
    This returns a path for immediate reading, not an authorization to execute it.
    """
    context = describe_entrypoint(entrypoint, source_root)
    try:
        parsed = urlsplit(reference)
        decoded = unquote(parsed.path)
        relative = Path(decoded)
        windows = PureWindowsPath(decoded)
        if (parsed.scheme or parsed.netloc or parsed.query or not decoded
                or relative.anchor or windows.anchor or '\x00' in decoded):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ResourceError('resource_reference_not_relative') from exc
    target = _canonical(Path(context['base_directory']) / relative, 'resource')
    if not target.is_relative_to(Path(context['source_root'])):
        raise ResourceError('resource_outside_source_root')
    if not target.is_file():
        raise ResourceError('resource_not_file')
    return {'status': 'resolved', **context, 'path': str(target), 'fragment': parsed.fragment}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--entrypoint', required=True, type=Path)
    parser.add_argument('--source-root', required=True, type=Path,
                        help='Approved package or checkout root; never broaden it after failure')
    parser.add_argument('--reference', required=True)
    args = parser.parse_args(argv)
    try:
        result = resolve_resource(args.entrypoint, args.reference, source_root=args.source_root)
    except ResourceError as exc:
        print(json.dumps({'status': 'blocked', 'reason': str(exc)}))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
