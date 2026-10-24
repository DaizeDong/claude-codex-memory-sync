#!/usr/bin/env python3
"""Build a runtime release (wheels, source resources, lock, bootstrap) from committed shas.

This is the one reproducible way to make the bundle that install-runtime-artifacts.py turns into
a generation.

Invariants (each one is checked, not assumed):
  * Every first-party byte comes from a commit. Each repo's ref (default main) is resolved ONCE in
    its live checkout to a sha; packages are built from a fresh `git clone` of that checkout,
    detached at the sha (core.autocrlf=false, so the bytes are the committed blobs); resources are
    read straight from the object store (`git show sha:path`). No live working tree is read or
    written, and an uncommitted change can never reach a release.
  * The spec and its pins (requirements-runtime.txt beside it) are operator configuration: they are
    read from a private config repository at one commit (--config-repo, --spec). A built or
    downloaded package whose version differs from its pin fails the build.
  * runtime_artifacts.py, runtime_launch.py and the bootstrap sources come from this repository at
    the commit being built, so the lock is written by the code that will later validate and install it.
  * Every Python file in a built wheel must be byte-identical to a file of the clean source tree.
  * Every entrypoint must name a file the resource ships.
  * Nothing comes from an earlier release: a cold build on a new machine needs only the repos and
    the network. Third-party wheels are downloaded from the PyPI URLs the spec lists (public_wheels)
    and must hash to the listed sha256; the bootstrap's packaging/ modules come from the pinned
    packaging wheel the same way; the lock header (required_offline, prerequisites, scope) is the
    spec's "lock" section. The lock itself is computed fresh each build.
  * The finished bundle is verified by the bootstrap it ships (`runtime-bootstrap.pyz --verify-only`),
    in a separate isolated process.

Run it with the runtime toolchain interpreter (the lock records that interpreter's ensurepip
bootstrap and install refuses a mismatch):

  %USERPROFILE%\\.codex\\claude-sync\\toolchains\\cpython-3.11.15-windows-x86_64-none\\python.exe -I -B ^
      tools\\build-runtime-generation.py --config-repo <dir> --spec <path in it> --out <dir> [--ref REPO=<ref>]

Install is a separate, explicit step (see the printed next steps). This script installs only when
--install-root is given, and refuses the live roots unless --allow-live-root is also given.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

ZIP_TIME = (2026, 1, 1, 0, 0, 0)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LIVE_ROOT_NAMES = ("installed", "background-task-core-installed")
TOOLS_REPO = "claude-codex-memory-sync"
DEFAULT_ARTIFACTS = Path.home() / ".codex" / "claude-sync" / "runtime-artifacts"


class BuildError(Exception):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(command, *, cwd=None, env=None, timeout=900, binary=False):
    try:
        result = subprocess.run([str(c) for c in command], cwd=cwd, env=env, capture_output=True,
                                timeout=timeout, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BuildError(f"{Path(str(command[0])).name}: {type(error).__name__}") from None
    if result.returncode != 0:
        tail = result.stderr.decode("utf-8", "replace").strip().splitlines()[-6:]
        raise BuildError(f"{' '.join(str(c) for c in command[:4])} ... exited {result.returncode}: "
                         + " | ".join(tail))
    return result.stdout if binary else result.stdout.decode("utf-8", "replace")


def git(repo, *args, binary=False):
    return run(["git", "--no-optional-locks", "-c", "core.autocrlf=false", "-c", "core.longpaths=true",
                "-C", str(repo), *args],
               binary=binary, timeout=300)


def expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


def remove_tree(path: Path):
    """Git marks pack files read-only; clear the bit instead of leaving a half-deleted clone."""
    def retry(function, target, _info):
        os.chmod(target, stat.S_IWRITE)
        function(target)
    if path.exists():
        shutil.rmtree(path, onerror=retry)


def fetch(url: str, digest: str, target: Path) -> Path:
    """Download url to target; the bytes must hash to digest."""
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()
    if sha256(data) != digest:
        raise BuildError(f"{url}: sha256 mismatch")
    target.write_bytes(data)
    return target


def deterministic_zip(entries: dict) -> bytes:
    """Sorted members, fixed timestamps and modes: the same inputs always give the same bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = 0o100644 << 16
            archive.writestr(info, entries[name])
    return buffer.getvalue()


class Source:
    """One repository pinned to one commit. Reads go to the object store; builds use a clone."""

    def __init__(self, name, live: Path, ref: str, work: Path):
        self.name, self.live, self.ref, self.work = name, live, ref, work
        if not (live / ".git").exists():
            raise BuildError(f"{name}: {live} is not a git checkout")
        self.sha = git(live, "rev-parse", "--verify", "--quiet", ref + "^{commit}").strip()
        if len(self.sha) != 40:
            raise BuildError(f"{name}: {ref} does not resolve to a commit")
        self.epoch = int(git(live, "show", "-s", "--format=%ct", self.sha).strip())
        self.subject = git(live, "show", "-s", "--format=%s", self.sha).strip()
        contained = git(live, "branch", "-r", "--contains", self.sha).split()
        self.on_remote = sorted(b for b in contained if b.startswith("origin/"))
        self._clone = None

    def read(self, path: str) -> bytes:
        return git(self.live, "show", f"{self.sha}:{path}", binary=True)

    def exists(self, path: str) -> bool:
        listing = git(self.live, "ls-tree", "-z", "--name-only", self.sha, "--", path).split("\0")
        return path in listing

    def tracked(self, directory: str):
        out = git(self.live, "ls-tree", "-r", "-z", "--name-only", self.sha, "--", directory)
        return [line for line in out.split("\0") if line]

    def clone(self) -> Path:
        """A fresh clone detached at the sha. Never the live working tree."""
        if self._clone is None:
            target = self.work / "src" / self.name
            run(["git", "-c", "core.autocrlf=false", "-c", "core.longpaths=true", "clone", "--quiet", "--no-checkout", "--no-tags",
                 str(self.live), str(target)], timeout=900)
            git(target, "config", "core.autocrlf", "false")
            git(target, "config", "core.longpaths", "true")
            git(target, "checkout", "--quiet", "--detach", self.sha)
            if git(target, "status", "--porcelain=v1", "--untracked-files=all").strip():
                raise BuildError(f"{self.name}: fresh clone at {self.sha[:12]} is not clean")
            self._clone = target
        return self._clone

    def record(self):
        return {"repo": self.name, "ref": self.ref, "sha": self.sha, "subject": self.subject,
                "on_remote": self.on_remote}


def _evidence(head, files):
    # A resource never carries submodule files: the evidence records an empty list in the shape
    # runtime_artifacts.source_evidence uses.
    submodules = []
    evidence = {"head": head, "dirty": False, "status_sha256": sha256(b""),
                "files": files, "submodules": submodules}
    evidence["working_tree_sha256"] = sha256(
        (json.dumps({"files": files, "submodules": submodules}, sort_keys=True, indent=2,
                    ensure_ascii=True) + "\n").encode())
    return evidence


def observe(source: Source, paths):
    """Provenance of a resource read from the object store at one commit (never dirty)."""
    files = {p: sha256(source.read(p)) for p in sorted(paths)}
    return _evidence(source.sha, files)


def parse_pins(text: str):
    pins = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if "==" not in line:
            raise BuildError(f"requirements-runtime.txt: not an exact pin: {line!r}")
        name, version = (part.strip() for part in line.split("==", 1))
        if name in pins:
            raise BuildError(f"requirements-runtime.txt: {name} pinned twice")
        pins[name] = version
    return pins


class Builder:
    def __init__(self, args):
        self.args = args
        self.work = Path(tempfile.mkdtemp(prefix="runtime-build-", dir=args.work))
        self.out = args.out.absolute()

    # ---- inputs -------------------------------------------------------------------------------
    def load_config(self):
        refs = dict(item.split("=", 1) for item in self.args.ref)
        self.config = Source("config", self.args.config_repo, refs.pop("config", "main"), self.work)
        self.spec = json.loads(self.config.read(self.args.spec).decode("utf-8"))
        pins = str(PurePosixPath(self.args.spec).with_name("requirements-runtime.txt"))
        self.pins = parse_pins(self.config.read(pins).decode("utf-8"))
        unknown = set(refs) - set(self.spec["repos"])
        if unknown:
            raise BuildError("--ref names repos the spec does not know: " + ", ".join(sorted(unknown)))
        self.sources = {name: Source(name, expand(path), refs.get(name, "main"), self.work)
                        for name, path in self.spec["repos"].items()}
        if TOOLS_REPO not in self.sources:
            raise BuildError(f"the spec must name {TOOLS_REPO}, which carries these tools")
        # runtime_artifacts.py from the commit being built: the lock is written and validated by
        # the code that the bundle's own bootstrap will run.
        tools = self.work / "build-tools"
        tools.mkdir()
        for name in ("runtime_artifacts.py", "runtime_launch.py"):
            (tools / name).write_bytes(self.sources[TOOLS_REPO].read("tools/" + name))
        # fleet_guards (filesystem guards) from the fleet-guards commit being built.
        guards = self.sources["fleet-guards"].clone()
        sys.path[:0] = [str(tools), str(guards)]
        import runtime_artifacts  # noqa: E402  (deliberately late: from the pinned commit)
        self.ra = runtime_artifacts
        if Path(runtime_artifacts.__file__).resolve().parent != tools.resolve():
            raise BuildError("runtime_artifacts was not imported from the commit being built")

    # ---- packages -----------------------------------------------------------------------------
    def build_wheel(self, name, source: Source) -> Path:
        clone = source.clone()
        tree = self.work / "build" / name
        shutil.copytree(clone, tree, ignore=shutil.ignore_patterns(".git"))
        wheels = self.work / "wheels" / name
        wheels.mkdir(parents=True)
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("PYTHON", "PIP_"))}
        env.update(PIP_CONFIG_FILE=os.devnull, PYTHONDONTWRITEBYTECODE="1",
                   SOURCE_DATE_EPOCH=str(source.epoch))
        run([sys.executable, "-I", "-m", "pip", "wheel", "--quiet", "--no-deps", "--no-build-isolation",
             "--no-index", "--no-cache-dir", "--disable-pip-version-check", "-w", wheels, tree],
            cwd=self.work, env=env)
        built = sorted(wheels.glob("*.whl"))
        if len(built) != 1:
            raise BuildError(f"{name}: expected one wheel, got {[w.name for w in built]}")
        return built[0]

    def packages(self):
        ra = self.ra
        records, changes = [], []
        for name, repo in self.spec["packages"].items():
            source = self.sources[repo]
            wheel = self.build_wheel(name, source)
            shutil.copyfile(wheel, self.out / wheel.name)
            meta, entries = ra.wheel_metadata(self.out / wheel.name)
            if meta["name"] != name:
                raise BuildError(f"{repo} builds {meta['name']}, the spec expects {name}")
            evidence = ra.source_evidence(source.clone())
            if evidence["dirty"] or evidence["head"] != source.sha:
                raise BuildError(f"{name}: build source is not the clean commit {source.sha[:12]}")
            matches = {}
            for member, data in entries.items():
                if member.endswith(".py"):
                    found = sorted(p for p, h in evidence["files"].items()
                                   if p.endswith(".py") and h == ra.digest(data))
                    if not found:
                        raise BuildError(f"{name}: wheel member {member} matches no committed file")
                    matches[member] = found
            if not matches:
                raise BuildError(f"{name}: wheel carries no Python payload to attest")
            meta.update(source=evidence, payload_source_matches=matches, schemas={}, input_id=name,
                        provenance=(f"built by tools/build-runtime-generation.py from a clean clone "
                                    f"detached at {source.sha}; measured Python payload"))
            records.append(meta)
            changes.append({"id": name, "version": meta["version"], "head": source.sha})
        for wheel in self.spec["public_wheels"]:
            filename = wheel["url"].rsplit("/", 1)[-1]
            fetch(wheel["url"], wheel["sha256"], self.out / filename)
            meta, _ = ra.wheel_metadata(self.out / filename)
            origin = {"sha256": wheel["sha256"], "url": wheel["url"]}
            ra.validate_origin(origin, meta["sha256"])
            meta.update(artifact_origin=origin, schemas={}, input_id="public-wheel")
            records.append(meta)
        names = [r["name"] for r in records]
        if len(names) != len(set(names)):
            raise BuildError("a package appears twice in the release")
        versions = {r["name"]: r["version"] for r in records}
        for name, version in self.pins.items():
            if name not in versions:
                raise BuildError(f"pin {name}=={version} has no package in the release")
            if versions[name] != version:
                raise BuildError(f"pin mismatch: {name} is {versions[name]} in the release, "
                                 f"requirements-runtime.txt says {version}")
        self.versions = versions
        return sorted(records, key=lambda p: p["name"]), changes

    # ---- resources ----------------------------------------------------------------------------
    def pinned(self, names):
        missing = [n for n in names if n not in self.versions]
        if missing:
            raise BuildError("resource requires packages the release lacks: " + ", ".join(missing))
        return [f"{n}=={self.versions[n]}" for n in names]

    def resources(self):
        ra = self.ra
        records, changes = [], []
        for identity, spec in self.spec["resources"].items():
            source = self.sources[spec["repo"]]
            paths = list(spec.get("files", []))
            for rule in spec.get("include", []):
                paths += [p for p in source.tracked(rule["dir"])
                          if PurePosixPath(p).suffix in rule["suffixes"]]
            paths = sorted(set(paths))
            missing = [p for p in paths if not source.exists(p)]
            if missing:
                raise BuildError(f"{identity}: not in {spec['repo']}@{source.sha[:12]}: " + ", ".join(missing))
            entries = {p: source.read(p) for p in paths}
            entrypoints = {}
            for name, entry in spec.get("entrypoints", {}).items():
                entry = dict(entry)
                if entry["path"] not in entries:
                    raise BuildError(f"{identity}: entrypoint {name} names files outside the resource")
                if "requires_dist" in entry:
                    entry["requires_dist"] = self.pinned(entry["requires_dist"])
                entrypoints[name] = entry
            archive = deterministic_zip(entries)
            filename = identity + ".zip"
            (self.out / filename).write_bytes(archive)
            item = {"id": identity, "file": filename, "sha256": sha256(archive),
                    "files": {p: sha256(d) for p, d in entries.items()},
                    "provenance": dict(observe(source, paths), repo=spec["repo"]),
                    "entrypoints": entrypoints,
                    "requires_dist": self.pinned(spec.get("requires", []))}
            records.append(ra.resource_metadata(self.out, item))
            changes.append({"id": identity, "sha256": item["sha256"], "head": source.sha})
        return records, changes

    # ---- bootstrap ----------------------------------------------------------------------------
    def bootstrap_pyz(self):
        spec = self.spec["bootstrap"]
        entries = {}
        for name, (repo, path) in spec["files"].items():
            entries[name] = self.sources[repo].read(path)
        pin = spec["packaging_wheel"]
        wheel = fetch(pin["url"], pin["sha256"], self.work / pin["url"].rsplit("/", 1)[-1])
        with zipfile.ZipFile(wheel) as archive:
            for info in archive.infolist():
                if info.filename.startswith("packaging/") and info.filename.endswith(".py"):
                    entries[info.filename] = archive.read(info)
        data = deterministic_zip(entries)
        (self.out / "runtime-bootstrap.pyz").write_bytes(data)
        return sha256(data)

    # ---- whole build --------------------------------------------------------------------------
    def build(self):
        if self.out.exists() and any(self.out.iterdir()):
            raise BuildError(f"{self.out} is not empty")
        self.out.mkdir(parents=True, exist_ok=True)
        self.load_config()
        packages, package_changes = self.packages()
        resources, resource_changes = self.resources()
        lock = dict(self.spec["lock"], schema_version=self.ra.SCHEMA, bootstrap=self.ra.bootstrap(),
                    packages=packages, resources=resources,
                    build={"builder": "tools/build-runtime-generation.py", "config": self.config.sha,
                           "sources": {n: s.sha for n, s in sorted(self.sources.items())}})
        self.ra.validate_lock(lock, self.out)
        data = self.ra.encoded(lock)
        (self.out / "runtime-lock.json").write_bytes(data)
        lock_sha = sha256(data)
        pyz_sha = self.bootstrap_pyz()
        # Verify with the bootstrap this bundle ships, in a separate isolated process.
        verified = json.loads(run([sys.executable, "-I", "-B", self.out / "runtime-bootstrap.pyz",
                                   "--verify-only", "--lock", self.out / "runtime-lock.json",
                                   "--bundle", self.out, "--expected-lock-sha256", lock_sha]))
        if verified.get("artifacts_verified") is not True:
            raise BuildError("the bundle's own bootstrap did not verify it")
        report = {"lock_sha256": lock_sha, "bootstrap_sha256": pyz_sha, "interpreter": sys.executable,
                  "sources": [s.record() for _, s in sorted(self.sources.items())],
                  "pins": self.pins, "package_changes": package_changes,
                  "resource_changes": resource_changes,
                  "not_on_a_remote_branch": sorted(n for n, s in self.sources.items() if not s.on_remote)}
        (self.out / "build-report.json").write_bytes(self.ra.encoded(report))
        return report

    def install(self, root: Path, lock_sha: str):
        root = root.absolute()
        live = {(self.args.artifacts / name).absolute() for name in LIVE_ROOT_NAMES}
        if root in live and not self.args.allow_live_root:
            raise BuildError(f"{root} is a live installed root; pass --allow-live-root to install there")
        return json.loads(run([sys.executable, "-I", "-B", self.out / "runtime-bootstrap.pyz",
                               "--lock", self.out / "runtime-lock.json", "--bundle", self.out,
                               "--root", root, "--expected-lock-sha256", lock_sha], timeout=1800))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True, help="new or empty directory for the release")
    parser.add_argument("--config-repo", type=Path, required=True,
                        help="private config checkout whose ref supplies the spec and its pins")
    parser.add_argument("--spec", required=True,
                        help="spec path inside the config repo; requirements-runtime.txt sits beside it")
    parser.add_argument("--ref", action="append", default=[], metavar="REPO=REF",
                        help="commit or branch per repo (default main); repeatable; "
                             "config=REF selects the config commit")
    parser.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    parser.add_argument("--work", type=Path, default=None, help="scratch parent (default: system temp)")
    parser.add_argument("--keep-work", action="store_true")
    parser.add_argument("--install-root", type=Path, default=None,
                        help="also install the built release into this root (scratch roots only, "
                             "unless --allow-live-root)")
    parser.add_argument("--allow-live-root", action="store_true")
    args = parser.parse_args(argv)
    if args.work is not None:
        args.work.mkdir(parents=True, exist_ok=True)
    builder = Builder(args)
    try:
        report = builder.build()
        print(json.dumps({k: report[k] for k in ("lock_sha256", "bootstrap_sha256", "package_changes",
                                                 "not_on_a_remote_branch")}, indent=2))
        if args.install_root is not None:
            receipt = builder.install(args.install_root, report["lock_sha256"])
            print(json.dumps({"installed": receipt.get("generation"), "packages": receipt.get("packages")},
                             indent=2))
        print(f"\nnext: publish {args.out} as {args.artifacts / 'releases' / report['lock_sha256']}, then\n"
              f"  {sys.executable} -I -B <release>\\runtime-bootstrap.pyz --lock <release>\\runtime-lock.json "
              f"--bundle <release> --root <root> --expected-lock-sha256 {report['lock_sha256']}")
        return 0
    except BuildError as error:
        print(f"BUILD FAILED: {error}", file=sys.stderr)
        return 1
    finally:
        if not args.keep_work:
            remove_tree(builder.work)


if __name__ == "__main__":
    sys.exit(main())
