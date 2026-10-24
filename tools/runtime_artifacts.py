"""Reviewed wheel bundles and isolated generations; no profile or state restoration.

The lock is an integrity manifest, not a signature. Its digest must be reviewed
out of band. Bootstrap uses the selected Python's bundled ensurepip, never PyPI.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import csv
import configparser
import email
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import subprocess
import sys
import uuid
import zipfile

try:
    from fleet_guards import filesystem as _filesystem
except ModuleNotFoundError as error:
    if error.name != 'fleet_guards':
        raise
    # This installer must also bootstrap fleet-guards itself. No sibling imports.
    _filesystem = None

try:
    from profile_lock import acquire as _resource_lock, LockBusyError as _ResourceBusy
except ModuleNotFoundError as error:
    if error.name != 'profile_lock':
        raise
    _resource_lock = None

# The stable entry this installer publishes beside current.json (stdlib only, shipped in the same pyz).
import runtime_launch

try:
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet
    from packaging.tags import parse_tag, sys_tags
    from packaging.utils import canonicalize_name, parse_wheel_filename
except ImportError:
    # pip ships with supported bootstrap Python distributions; no runtime packages
    # from sibling source trees are imported to install those very packages.
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.specifiers import SpecifierSet
    from pip._vendor.packaging.tags import parse_tag, sys_tags
    from pip._vendor.packaging.utils import canonicalize_name, parse_wheel_filename

SCHEMA = 1
MAX_JSON_BYTES = 16 * 1024 * 1024


class ArtifactError(ValueError):
    def __init__(self, message, *, reason='artifact_validation_failed', exit_code=None):
        super().__init__(message)
        self.reason = reason
        self.exit_code = exit_code


class ArtifactArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's normal diagnostic echoes arbitrary argument values.
        raise ArtifactError('invalid command arguments', reason='invalid_arguments')


def failure_report(error):
    """Only allowlisted diagnostics; exception strings can contain private input."""
    reasons = {'artifact_validation_failed', 'child_failed', 'child_timeout',
               'child_unavailable', 'invalid_wheel_metadata', 'invalid_json',
               'lock_digest_mismatch', 'json_too_large', 'invalid_arguments',
               'recovery_unavailable', 'recovery_journal_invalid', 'recovery_conflict',
               'installation_lock_unavailable'}
    reason, kind, exit_code = 'operation_failed', 'Exception', None
    if isinstance(error, ArtifactError):
        reason = error.reason if error.reason in reasons else 'artifact_validation_failed'
        kind = 'ArtifactError'
        exit_code = error.exit_code if type(error.exit_code) is int else None
    elif isinstance(error, OSError):
        kind = 'OSError'
    elif isinstance(error, (ValueError, KeyError, TypeError)):
        kind = 'InvalidInput'
    return {'error': reason, 'error_type': kind, 'exit_code': exit_code}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactError("duplicate JSON key")
        result[key] = value
    return result


def json_buffer(data):
    try:
        return json.loads(data, object_pairs_hook=unique_object)
    except (UnicodeError, json.JSONDecodeError):
        raise ArtifactError('invalid JSON', reason='invalid_json') from None


def read_json_bytes(path: Path):
    """Narrow bootstrap read; the approved digest and parser share these bytes."""
    no_links(path)
    if _filesystem is not None:
        return _filesystem.read_bounded(path, MAX_JSON_BYTES)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ArtifactError('JSON must be a regular file')
    with path.open('rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ArtifactError('JSON must be a regular file')
        data = stream.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise ArtifactError('JSON exceeds byte limit', reason='json_too_large')
    return data


def read_json(path: Path):
    return json_buffer(read_json_bytes(path))


def reviewed_lock(path: Path, expected_sha256: str):
    data = read_json_bytes(path)
    if digest(data) != expected_sha256:
        raise ArtifactError('reviewed lock digest mismatch', reason='lock_digest_mismatch')
    return json_buffer(data)


def safe_relative(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name:
        raise ArtifactError("invalid relative path")
    parts = PurePosixPath(name).parts
    if not parts or name.startswith("/") or any(
        p in (".", "..") or p.endswith((" ", ".")) or
        re.search(r'[<>:"|?*\x00-\x1f]', p) or
        p.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}
        for p in parts
    ) or "/".join(parts) != name:
        raise ArtifactError("unsafe relative path")
    return name


def no_links(path: Path):
    if _filesystem is not None:
        try:
            _filesystem.validate_path(path)
        except ValueError:
            raise ArtifactError('symlink/junction or unsafe path rejected') from None
        return
    # Minimal stdlib-only bootstrap guard, not a second filesystem library.
    # lstat exposes all Windows reparse points even without Path.is_junction (3.11).
    path = path.absolute()
    for item in reversed([path, *path.parents]):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        attributes = getattr(info, 'st_file_attributes', None)
        if os.name == 'nt' and attributes is None:
            raise ArtifactError('reparse attributes unavailable')
        if stat.S_ISLNK(info.st_mode) or (attributes or 0) & 0x400:
            raise ArtifactError("symlink/junction path rejected")
        if item != path and not stat.S_ISDIR(info.st_mode):
            raise ArtifactError('non-directory ancestor')


def bootstrap():
    import ensurepip
    bundled = Path(ensurepip.__file__).parent / "_bundled"
    return {"implementation": platform.python_implementation(), "version": platform.python_version(),
            "platform": sys.platform, "machine": platform.machine(), "pip": ensurepip.version(),
            "bundled_wheels": {p.name: digest(p.read_bytes()) for p in sorted(bundled.glob("*.whl"))}}


def wheel_metadata(path: Path):
    try:
        return _wheel_metadata(path)
    except (ArtifactError, OSError):
        raise
    except Exception:
        raise ArtifactError('wheel metadata invalid', reason='invalid_wheel_metadata') from None


def _wheel_metadata(path: Path):
    no_links(path)
    wheel_bytes = path.read_bytes()
    name, version, _, tags = parse_wheel_filename(path.name)
    if not tags.intersection(sys_tags()):
        raise ArtifactError("wheel platform/interpreter mismatch")
    with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as archive:
        if sum(i.file_size for i in archive.infolist()) > 512 * 1024 * 1024:
            raise ArtifactError("oversized wheel")
        entries = {}
        folded = set()
        for info in archive.infolist():
            entry = safe_relative(info.filename.rstrip("/"))
            if entry.casefold() in folded:
                raise ArtifactError("duplicate wheel member")
            folded.add(entry.casefold())
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ArtifactError("special wheel member")
            if info.file_size > 256 * 1024 * 1024:
                raise ArtifactError("oversized wheel member")
            if not info.is_dir():
                entries[entry] = archive.read(info)
        metas = [p for p in entries if p.endswith(".dist-info/METADATA")]
        if len(metas) != 1:
            raise ArtifactError("wheel must contain one METADATA")
        prefix = metas[0].rsplit("/", 1)[0]
        expected = f"{str(name).replace('-', '_')}-{version}.dist-info"
        if prefix.casefold() != expected.casefold():
            raise ArtifactError("dist-info identity mismatch")
        if any(".dist-info/" in p and not p.startswith(prefix + "/") for p in entries):
            raise ArtifactError("multiple dist-info identities")
        # Some candidate builders emit CRCRLF; normalize for metadata parsing only.
        meta = email.message_from_bytes(entries[metas[0]].replace(b"\r\r\n", b"\n"))
        if meta.get('Metadata-Version') not in ('1.0', '1.1', '1.2', '2.1', '2.2', '2.3', '2.4', '2.5'):
            raise ArtifactError("unsupported METADATA schema")
        if canonicalize_name(meta["Name"] or "") != name or meta["Version"] != str(version):
            raise ArtifactError("filename/METADATA mismatch")
        for field in ("Name", "Version", "Requires-Python"):
            if len(meta.get_all(field, [])) > 1:
                raise ArtifactError("duplicate metadata field")
        requires_python = meta.get("Requires-Python", "")
        if platform.python_version() not in SpecifierSet(requires_python):
            raise ArtifactError("Requires-Python mismatch")
        wheel_file = prefix + "/WHEEL"
        if wheel_file not in entries:
            raise ArtifactError("WHEEL metadata missing")
        wheel = email.message_from_bytes(entries[wheel_file].replace(b'\r\r\n', b'\n'))
        if wheel.get('Wheel-Version') != '1.0':
            raise ArtifactError("unsupported wheel schema")
        wheel_tags = set()
        for value in wheel.get_all('Tag', []):
            wheel_tags.update(parse_tag(value))
        if wheel_tags != tags:
            raise ArtifactError('filename/WHEEL tag mismatch')
        record = prefix + "/RECORD"
        if record not in entries:
            raise ArtifactError("wheel RECORD missing")
        records = {}
        for row in csv.reader(io.StringIO(entries[record].decode())):
            if len(row) != 3 or row[0] in records:
                raise ArtifactError("invalid/duplicate RECORD")
            records[row[0]] = row[1:]
        if set(records) != set(entries):
            raise ArtifactError("RECORD inventory mismatch")
        for member, content in entries.items():
            hash_value, size = records[member]
            if member == record:
                if hash_value or size:
                    raise ArtifactError("RECORD self hash must be empty")
                continue
            expected_hash = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
            if hash_value != expected_hash or size != str(len(content)):
                raise ArtifactError("RECORD hash/size mismatch")
        requirements = sorted(meta.get_all("Requires-Dist", []))
        for requirement in requirements:
            if Requirement(requirement).url:
                raise ArtifactError("URL/source dependencies are forbidden")
    return {"name": str(name), "version": str(version), "file": path.name,
            "sha256": digest(wheel_bytes), "size": len(wheel_bytes),
            "metadata_sha256": digest(entries[metas[0]]), "requires_python": requires_python,
            "requires_dist": requirements}, entries


def git(repo: Path, *args) -> bytes:
    try:
        return subprocess.check_output(["git", "--no-optional-locks", "-C", str(repo), *args], stderr=subprocess.PIPE, timeout=60)
    except subprocess.CalledProcessError as error:
        raise ArtifactError('source inspection failed', reason='child_failed', exit_code=error.returncode) from None
    except subprocess.TimeoutExpired:
        raise ArtifactError('source inspection timed out', reason='child_timeout') from None


def source_evidence(repo: Path):
    no_links(repo)
    head = git(repo, "rev-parse", "HEAD").decode().strip()
    status = git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    inventory = {}
    submodules = []
    tracked = git(repo, "ls-files", "--stage", "-z").split(b"\0")
    gitlinks = {}
    for row in filter(None, tracked):
        header, raw_path = row.split(b"\t", 1)
        mode, sha, stage = header.decode().split()
        if stage != "0":
            raise ArtifactError("unmerged source index")
        rel = safe_relative(raw_path.decode("utf-8"))
        if mode == "160000":
            gitlinks[rel] = sha
    paths = git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    for raw in filter(None, paths.split(b"\0")):
        rel = safe_relative(raw.decode("utf-8"))
        if rel in gitlinks:
            child = repo / rel
            initialized = (child / ".git").exists()
            evidence = source_evidence(child) if initialized else None
            submodules.append({"path": rel, "pin": gitlinks[rel], "initialized": initialized, "source": evidence})
            continue
        path = repo / rel
        no_links(path)
        inventory[rel] = digest(path.read_bytes()) if path.is_file() else None
    return {"head": head, "dirty": bool(status), "status_sha256": digest(status),
            "files": inventory, "submodules": submodules,
            "working_tree_sha256": digest(encoded({"files": inventory, "submodules": submodules}))}


def closure(packages):
    by_name = {p["name"]: p for p in packages}
    if len(by_name) != len(packages):
        raise ArtifactError("duplicate package identity")
    extras = {name: {""} for name in by_name}
    changed = True
    while changed:
        changed = False
        for package in packages:
            for value in package["requires_dist"]:
                req = Requirement(value)
                if req.url:
                    raise ArtifactError("URL dependency forbidden")
                if req.marker and not any(req.marker.evaluate({"extra": e}) for e in extras[package["name"]]):
                    continue
                dep = canonicalize_name(req.name)
                if dep not in by_name or by_name[dep]["version"] not in req.specifier:
                    raise ArtifactError('incomplete dependency closure')
                before = len(extras[dep])
                extras[dep].update(req.extras)
                changed |= before != len(extras[dep])


MCP_RUNTIME_PINS = {'mcp': '1.9.4', 'mcp-proxy': '0.9.0'}


def artifact_capabilities(packages):
    versions = {package['name']: package['version'] for package in packages}
    return {'mcp-pins'} if all(versions.get(name) == version for name, version in MCP_RUNTIME_PINS.items()) else set()


def validate_origin(origin, sha256):
    """Public registry wheels have artifact provenance, not invented Git revisions."""
    if (not isinstance(origin, dict) or origin.get('sha256') != sha256
            or not re.fullmatch(r'https://files\.pythonhosted\.org/[^\s?#]+\.whl', origin.get('url', ''))):
        raise ArtifactError('public wheel provenance mismatch')


def resource_archive(path):
    no_links(path)
    data = path.read_bytes()
    entries, folded = {}, set()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if sum(i.file_size for i in archive.infolist()) > 512 * 1024 * 1024:
            raise ArtifactError('oversized source archive')
        for info in archive.infolist():
            name = safe_relative(info.filename.rstrip('/'))
            mode = info.external_attr >> 16
            if (name.casefold() in folded or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or info.file_size > 32 * 1024 * 1024):
                raise ArtifactError('unsafe source archive member')
            folded.add(name.casefold())
            if not info.is_dir():
                entries[name] = archive.read(info)
    if not entries:
        raise ArtifactError('empty source archive')
    for name in entries:
        if any(parent.as_posix().casefold() in {n.casefold() for n in entries} for parent in PurePosixPath(name).parents if str(parent) != '.'):
            raise ArtifactError('source file/directory collision')
    return data, entries


def resource_metadata(bundle, item):
    """Validate immutable code/resources. No import-path installation or execution."""
    identity, filename = item['id'], safe_relative(item['file'])
    if not re.fullmatch(r'[a-z][a-z0-9-]*', identity) or '/' in filename:
        raise ArtifactError('invalid source identity')
    data, entries = resource_archive(bundle / filename)
    files = {name: digest(content) for name, content in entries.items()}
    if item['sha256'] != digest(data) or files != item['files'] or not item.get('provenance'):
        raise ArtifactError('source resource evidence mismatch')
    for name, entry in item.get('entrypoints', {}).items():
        if not re.fullmatch(r'[a-z][a-z0-9-]*', name) or entry.get('kind') not in ('python-script', 'python-module', 'powershell'):
            raise ArtifactError('invalid resource entrypoint')
        if safe_relative(entry['path']) not in files:
            raise ArtifactError('source entrypoint missing')
        if 'import_root' in entry:
            prefix = safe_relative(entry['import_root']) + '/'
            if not any(p.startswith(prefix) for p in files):
                raise ArtifactError('source import root missing')
        if entry['kind'] == 'python-module' and not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*', entry.get('module', '')):
            raise ArtifactError('invalid module entrypoint')
    return dict(item, size=len(data))


def resource_dependencies(resources):
    return [{'name': r['id'], 'version': '0', 'requires_dist': r.get('requires_dist', [])} for r in resources]


def validate_lock(lock, bundle: Path):
    if lock["schema_version"] != SCHEMA or lock["bootstrap"] != bootstrap():
        raise ArtifactError("lock schema/bootstrap version incompatible")
    if not lock["packages"]:
        raise ArtifactError("empty artifact lock")
    paths = set()
    payload_paths = set()
    for package in lock["packages"]:
        filename = safe_relative(package["file"])
        if "/" in filename or filename.casefold() in paths:
            raise ArtifactError("duplicate/nested artifact path")
        paths.add(filename.casefold())
        actual, entries = wheel_metadata(bundle / filename)
        if any(package.get(key) != value for key, value in actual.items()):
            raise ArtifactError("artifact hash/metadata mismatch")
        for member in entries:
            folded = member.casefold()
            if folded in payload_paths:
                raise ArtifactError("overlapping package payloads")
            payload_paths.add(folded)
        if 'artifact_origin' in package:
            validate_origin(package['artifact_origin'], actual['sha256'])
            continue
        source = package["source"]
        if not re.fullmatch(r"[0-9a-f]{40,64}", source["head"]):
            raise ArtifactError("source revision missing")
        if source["working_tree_sha256"] != digest(encoded({"files": source["files"], "submodules": source["submodules"]})):
            raise ArtifactError("source working bytes evidence mismatch")
        for member, content in entries.items():
            if member.endswith('.py'):
                matches = package["payload_source_matches"].get(member, [])
                if not matches or any(source["files"].get(p) != digest(content) for p in matches):
                    raise ArtifactError("source/payload byte evidence mismatch")
        for schema, bounds in package["schemas"].items():
            version = lock["schema_versions"].get(schema)
            if not isinstance(version, int) or not bounds["min"] <= version <= bounds["max"]:
                raise ArtifactError("consumer schema incompatible")
    resources = lock.get('resources', [])
    for resource in resources:
        if resource['file'].casefold() in paths:
            raise ArtifactError('duplicate artifact path')
        paths.add(resource['file'].casefold())
        if resource_metadata(bundle, resource) != resource:
            raise ArtifactError('resource metadata mismatch')
    closure(lock["packages"] + resource_dependencies(resources))
    missing = set(lock["required_offline"]) - {p["name"] for p in lock["packages"]} - {r['id'] for r in resources} - artifact_capabilities(lock['packages'])
    if not missing.issubset({p["id"] for p in lock["online_required"] if p["status"] == "online_required"}):
        raise ArtifactError("required_offline omissions must be explicit")


def inventory(root: Path):
    no_links(root)
    result = {}
    def failed(error):
        raise error
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=failed):
        for name in dirs:
            no_links(Path(directory) / name)
        for name in files:
            path = Path(directory) / name
            no_links(path)
            if path != root / 'receipt.json':
                result[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    return result


def run(command, env):
    try:
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        raise ArtifactError('isolated subprocess timed out', reason='child_timeout') from None
    except OSError:
        raise ArtifactError('isolated subprocess unavailable', reason='child_unavailable') from None
    if result.returncode:
        raise ArtifactError('isolated subprocess failed', reason='child_failed', exit_code=result.returncode)
    return result.stdout


@contextmanager
def runtime_resource(root, *, recovery=False):
    """Reuse process-lifetime ownership; never infer death from a PID or age.

    Bare bootstrap can still install with the exclusive file, but cannot safely
    recover that format. POSIX requires the shared lock resource preprovisioned;
    create=False prevents this helper from writing outside the supplied root.
    """
    if _resource_lock is None or _filesystem is None:
        if recovery:
            raise ArtifactError('shared recovery primitives unavailable', reason='recovery_unavailable')
        yield False
    else:
        try:
            with _resource_lock(root / '.runtime-install-resource', create=False):
                yield True
        except _ResourceBusy:
            raise ArtifactError('installation resource unavailable or busy', reason='installation_lock_unavailable') from None


def optional_pointer(root):
    try:
        return read_json_bytes(root / 'current.json')
    except FileNotFoundError:
        return None


@contextmanager
def installation_lock(path, expected_sha256):
    with runtime_resource(path.parent) as recoverable:
        no_links(path)
        if path.exists():
            raise FileExistsError('installation already has a journal')
        previous = optional_pointer(path.parent)
        token = uuid.uuid4().hex
        journal = {'schema_version': 1, 'generation': 'generation-' + token,
                   'owner': {'pid': os.getpid(), 'token': token},
                   'locking': 'profile-resource-v1' if recoverable else 'exclusive-file-v1',
                   'lock_sha256': expected_sha256,
                   'previous_pointer_sha256': digest(previous) if previous is not None else None}
        data = encoded(journal)
        if recoverable:
            identity = _filesystem.create_no_replace_with_identity(path, data)
            if identity is None:
                raise FileExistsError('installation already has a journal')
        else:
            with path.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                info = os.fstat(stream.fileno())
                identity = [info.st_dev, info.st_ino]
        try:
            yield journal
        finally:
            no_links(path)
            info = path.lstat()
            if [info.st_dev, info.st_ino] != identity or read_json_bytes(path) != data:
                raise ArtifactError('installation journal conflict')
            path.unlink()


def verified_current(root, current):
    generation = safe_relative(current['generation'])
    if '/' in generation:
        raise ArtifactError('invalid generation')
    previous = root / 'generations' / generation
    receipt_bytes = read_json_bytes(previous / 'receipt.json')
    receipt = json_buffer(receipt_bytes)
    if (receipt.get('generation') != generation
            or digest(receipt_bytes) != current['receipt_sha256']
            or inventory(previous) != receipt['files']):
        raise ArtifactError('active generation receipt verification failed')
    return receipt


def resolve_installation(root: Path):
    """Read a configured generation without a subprocess, writes or fallbacks."""
    root = root.absolute()
    pointer_bytes = read_json_bytes(root / 'current.json')
    current = json_buffer(pointer_bytes)
    receipt = verified_current(root, current)
    stage = root / 'generations' / current['generation']
    python = stage / 'environment' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if (receipt.get('schema_version') != SCHEMA or receipt.get('python') != str(python)
            or not python.is_file() or python.relative_to(stage).as_posix() not in receipt['files']
            or not isinstance(receipt.get('packages'), dict)):
        raise ArtifactError('invalid installed interpreter')
    for entry in receipt.get('entrypoints', {}).values():
        if entry.get('path') not in receipt['files']:
            raise ArtifactError('installed entrypoint missing')
    # A concurrent publisher cannot mix the selected receipt with another pointer.
    if read_json_bytes(root / 'current.json') != pointer_bytes:
        raise ArtifactError('installed generation changed during resolution')
    entries = {}
    for name, entry in receipt.get('entrypoints', {}).items():
        entries[name] = dict(entry, path=str(stage / safe_relative(entry['path'])))
        if entry.get('import_root'):
            entries[name]['import_root'] = str(stage / safe_relative(entry['import_root']))
    return {'python': str(python), 'generation': str(stage), 'entrypoints': entries,
            'packages': receipt['packages'], 'receipt_sha256': current['receipt_sha256'],
            'runtime_ready': False}


def installed_entrypoints(lock, stage):
    result = {}
    for package in lock['packages']:
        _, entries = wheel_metadata(stage / 'wheelhouse' / package['file'])
        for member, data in entries.items():
            if not member.endswith('.dist-info/entry_points.txt'):
                continue
            parser = configparser.ConfigParser(interpolation=None)
            parser.optionxform = str
            parser.read_string(data.decode('utf-8'))
            if parser.has_section('console_scripts'):
                for name, target in parser.items('console_scripts'):
                    safe_relative(name)
                    if '/' in name or name in result:
                        raise ArtifactError('entrypoint collision')
                    path = 'environment/' + ('Scripts/' + name + '.exe' if os.name == 'nt' else 'bin/' + name)
                    if not (stage / path).is_file():
                        raise ArtifactError('installed console script missing')
                    result[name] = {'kind': 'console-script', 'path': path, 'target': target, 'package': package['name']}
    for resource in lock.get('resources', []):
        for name, entry in resource.get('entrypoints', {}).items():
            available = {p['name']: p['version'] for p in lock['packages']}
            requirements = [Requirement(r) for r in entry.get('requires_dist', [])]
            if any(r.url or canonicalize_name(r.name) not in available or available[canonicalize_name(r.name)] not in r.specifier for r in requirements):
                # Retain code but do not advertise an entrypoint whose optional
                # runtime is absent. The lock records its exact requirements.
                continue
            if name in result:
                raise ArtifactError('entrypoint collision')
            prefix = 'sources/' + resource['id'] + '/'
            result[name] = dict(entry, path=prefix + entry['path'])
            if 'import_root' in entry:
                result[name]['import_root'] = prefix + entry['import_root']
    return result


def recover_installation(root: Path):
    """Explicitly retain a dead owner's journal; never publish or delete payloads.

    The shared process-lifetime resource is the liveness authority. PID/token
    describe the journal's owner, but do not authorize breaking a live lock.
    Conditional detach is supported on Windows by the shared filesystem API.
    Legacy/partial/unknown journals and publication conflicts stay fail-closed.
    No arbitrary hostile filesystem compare-and-swap guarantee is made.
    """
    root = root.absolute()
    path = root / 'install.lock'
    no_links(path)
    if not path.exists():
        return {'recovery_state': 'no_pending', 'runtime_ready': False}
    with runtime_resource(root, recovery=True):
        # A competing recovery may already have retained it while we waited.
        if not path.exists():
            return {'recovery_state': 'no_pending', 'runtime_ready': False}
        identity = _filesystem.identity(path)
        data = read_json_bytes(path)
        journal = json_buffer(data)
        if not isinstance(journal, dict):
            raise ArtifactError('invalid installation journal', reason='recovery_journal_invalid')
        owner = journal.get('owner')
        generation = journal.get('generation')
        previous_sha = journal.get('previous_pointer_sha256')
        if (set(journal) != {'schema_version', 'generation', 'owner', 'locking', 'lock_sha256', 'previous_pointer_sha256'}
                or type(journal.get('schema_version')) is not int or journal['schema_version'] != 1
                or journal.get('locking') != 'profile-resource-v1'
                or not isinstance(owner, dict) or set(owner) != {'pid', 'token'}
                or type(owner.get('pid')) is not int
                or owner['pid'] <= 0 or owner['pid'] == os.getpid()
                or not isinstance(owner.get('token'), str)
                or not re.fullmatch(r'[0-9a-f]{32}', owner['token'])
                or generation != 'generation-' + owner['token']
                or not isinstance(journal.get('lock_sha256'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', journal['lock_sha256'])
                or (previous_sha is not None and (not isinstance(previous_sha, str)
                    or not re.fullmatch(r'[0-9a-f]{64}', previous_sha)))):
            raise ArtifactError('unrecoverable installation journal', reason='recovery_journal_invalid')
        stage = root / 'generations' / generation
        no_links(stage)
        current_data = optional_pointer(root)
        current_sha = digest(current_data) if current_data is not None else None
        if current_sha == previous_sha:
            # No publication: leave even incomplete/unverified generation bytes.
            state = 'abandoned_retained'
        else:
            if current_data is None:
                raise ArtifactError('installation publication conflict', reason='recovery_conflict')
            current = json_buffer(current_data)
            if current.get('generation') != generation:
                raise ArtifactError('installation publication conflict', reason='recovery_conflict')
            receipt = verified_current(root, current)
            if receipt.get('lock_sha256') != journal['lock_sha256']:
                raise ArtifactError('installation receipt lock mismatch', reason='recovery_conflict')
            state = 'published_verified'
        retained = root / 'recovery' / (owner['token'] + '.json')
        no_links(retained)
        retained.parent.mkdir(exist_ok=True)
        if not _filesystem.detach_if_matches(path, data, identity, retained):
            raise ArtifactError('installation recovery conflict', reason='recovery_conflict')
        _filesystem.sync_directory(root)
        _filesystem.sync_directory(retained.parent)
        return {'recovery_state': state, 'runtime_ready': False}


def install(lock_path: Path, bundle: Path, root: Path, expected_sha256: str):
    root = root.absolute()
    bundle = bundle.absolute()
    lock = reviewed_lock(lock_path, expected_sha256)
    validate_lock(lock, bundle)  # All artifacts validated before any target creation.
    no_links(root)
    root.mkdir(parents=True, exist_ok=True)
    no_links(root / "install.lock")
    with installation_lock(root / "install.lock", expected_sha256) as journal:
        pointer = root / "current.json"
        if pointer.exists():
            current = read_json(pointer)
            receipt = verified_current(root, current)
            if receipt["lock_sha256"] == expected_sha256:
                runtime_launch.publish(root)
                return receipt
        generation = journal['generation']
        stage = root / "generations" / generation
        no_links(stage)
        stage.mkdir(parents=True)
        wheelhouse = stage / "wheelhouse"
        wheelhouse.mkdir()
        for package in lock["packages"]:
            data = (bundle / package["file"]).read_bytes()
            if digest(data) != package["sha256"]:
                raise ArtifactError("artifact changed after preflight")
            (wheelhouse / package["file"]).write_bytes(data)
        for resource in lock.get('resources', []):
            data, entries = resource_archive(bundle / resource['file'])
            if digest(data) != resource['sha256']:
                raise ArtifactError('source changed after preflight')
            (wheelhouse / resource['file']).write_bytes(data)
            for name, content in entries.items():
                destination = stage / 'sources' / resource['id'] / name
                no_links(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open('xb') as stream:
                    stream.write(content)
        validate_lock(lock, wheelhouse)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "PIP_")) and k != "VIRTUAL_ENV"}
        env.update(PIP_CONFIG_FILE=os.devnull, PIP_NO_INDEX="1", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
        scratch = stage / "tmp"
        scratch.mkdir()
        env.update(TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch))
        environment = stage / "environment"
        run([sys.executable, "-I", "-m", "venv", "--copies", str(environment)], env)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        requirements = stage / "requirements.txt"
        requirements.write_text("".join(f"{p['name']}=={p['version']} --hash=sha256:{p['sha256']}\n" for p in lock["packages"]), encoding="utf-8")
        run([str(python), "-I", "-m", "pip", "--isolated", "--disable-pip-version-check", "install",
             "--no-index", "--require-hashes", "--only-binary=:all:", "--no-cache-dir",
             "--find-links", str(wheelhouse), "-r", str(requirements)], env)
        run([str(python), "-I", "-m", "pip", "--isolated", "check"], env)
        installed = json.loads(run([str(python), "-I", "-c",
            "import importlib.metadata as m,json; print(json.dumps({d.metadata['Name'].lower().replace('_','-'):d.version for d in m.distributions()}))"], env))
        if any(installed.get(p["name"]) != p["version"] for p in lock["packages"]):
            raise ArtifactError("installed package metadata mismatch")
        receipt = {"schema_version": SCHEMA, "lock_sha256": expected_sha256,
                   "generation": generation, "python": str(python.absolute()),
                   "packages": installed, "artifacts_installed": True,
                   "entrypoints": installed_entrypoints(lock, stage),
                   "runtime_ready": False, "auth_pending": True,
                   "online_required": lock["online_required"], "files": inventory(stage)}
        with (stage / "receipt.json").open('xb') as stream:
            stream.write(encoded(receipt))
            stream.flush()
            os.fsync(stream.fileno())
        if read_json(stage / 'receipt.json') != receipt:
            raise ArtifactError('receipt readback mismatch')
        if inventory(stage) != receipt["files"]:
            raise ArtifactError("generation changed before publication")
        new_pointer = root / ("current-" + uuid.uuid4().hex + ".tmp")
        with new_pointer.open("xb") as stream:
            stream.write(encoded({"generation": generation, "receipt_sha256": digest(encoded(receipt))}))
            stream.flush()
            os.fsync(stream.fileno())
        no_links(pointer)
        os.replace(new_pointer, pointer)
        # Consumers name <root>/launch.py, never a generation directory (whose name is a fresh token).
        runtime_launch.publish(root)
        return receipt
