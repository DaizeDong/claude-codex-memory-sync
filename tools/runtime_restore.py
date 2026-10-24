"""Install the runtime generation on a machine that has none, from source, without a person.

`~/.codex/claude-sync/runtime-artifacts` and `toolchains` are rebuilt here from the commits of the
repos the build spec names, the PyPI wheels it pins and the python-build-standalone archive it pins.
Two steps, each skipped when already done:

  toolchain  installs the pinned python-build-standalone archive named in the spec (url + sha256) at
             the toolchain path when it is missing; an existing interpreter must report the pinned
             version and is never replaced.
  install    = toolchain, then tools/build-runtime-generation.py builds a release from the repos' main
             (cold: wheels from PyPI by sha256, lock computed fresh) into
             <artifacts>/releases/<lock sha256>, then that release's own runtime-bootstrap.pyz installs it
             into <artifacts>/<root>, then <root>/launch.py (tools/runtime_launch.py, the stable entry
             consumers name) is published. An active generation that already verifies is kept.

The spec lives in a private config repository (--config-repo); --spec is its path on disk inside it.

Output is one JSON object on stdout; the exit code is 0 for PASS/ALREADY/PLAN, 4 for WAIT (needs the
network under --offline), 1 for FAIL. Stdlib only, plus tools/runtime_launch.py from this directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_launch  # noqa: E402

BUILDER = Path(__file__).resolve().parent / "build-runtime-generation.py"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class RuntimeRestoreError(Exception):
    def __init__(self, reason: str, state: str = "FAIL"):
        super().__init__(reason)
        self.reason = reason
        self.state = state


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path):
    return json.loads(path.read_bytes().decode("utf-8"))


def expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


# ---- toolchain --------------------------------------------------------------------------------

def probe_version(python: Path) -> str:
    try:
        result = subprocess.run([str(python), "-I", "-c", "import platform; print(platform.python_version())"],
                                capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def download(url: str, digest: str, target: Path) -> None:
    """Stream url to target; the bytes must hash to digest or nothing is left behind."""
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".part")
    h = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=120) as response, temporary.open("xb") as stream:
            for chunk in iter(lambda: response.read(1 << 20), b""):
                h.update(chunk)
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if h.hexdigest() != digest:
            raise RuntimeRestoreError("download_digest_mismatch")
        os.replace(temporary, target)
    except OSError:
        raise RuntimeRestoreError("download_failed") from None
    finally:
        if temporary.exists():
            temporary.unlink()


def extract_toolchain(archive: Path, prefix: str, destination: Path, markers: dict) -> None:
    """Every regular file under `prefix` lands below `destination`; anything else refuses the archive."""
    if destination.exists():
        raise RuntimeRestoreError("toolchain_staging_exists")
    destination.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            if member.isdir():
                continue
            if not member.isfile() or not member.name.startswith(prefix):
                raise RuntimeRestoreError("toolchain_archive_member_rejected")
            relative = member.name[len(prefix):]
            try:
                runtime_launch.safe_relative(relative)
            except runtime_launch.LaunchError:
                raise RuntimeRestoreError("toolchain_archive_member_rejected") from None
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            with target.open("xb") as stream:
                shutil.copyfileobj(source, stream)
    for relative, text in markers.items():
        runtime_launch.safe_relative(relative)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(text.encode("utf-8"))


def toolchain(spec: dict, archive: Path | None, offline: bool, plan: bool) -> dict:
    pin = spec["toolchain_archive"]
    python = expand(spec["toolchain"])
    if python.exists():
        version = probe_version(python)
        if version != pin["version"]:
            raise RuntimeRestoreError("toolchain_present_but_not_the_pinned_version")
        return {"step": "toolchain", "state": "ALREADY", "path": str(python)}
    if plan:
        return {"step": "toolchain", "state": "PLAN", "path": str(python)}
    home = python.parent
    home.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    staging = home.parent / (".toolchain-" + token)
    fetched = None
    try:
        if archive is None:
            if offline:
                raise RuntimeRestoreError("toolchain_needs_download", state="WAIT")
            fetched = home.parent / (".toolchain-" + token + ".tar.gz")
            download(pin["url"], pin["sha256"], fetched)
            archive = fetched
        elif sha256_file(archive) != pin["sha256"]:
            raise RuntimeRestoreError("toolchain_archive_digest_mismatch")
        extract_toolchain(archive, pin["archive_root"], staging, pin.get("markers", {}))
        if not (staging / python.name).is_file():
            raise RuntimeRestoreError("toolchain_archive_without_interpreter")
        os.rename(staging, home)
    finally:
        if fetched is not None and fetched.exists():
            fetched.unlink()
        if staging.exists():
            shutil.rmtree(staging)
    if probe_version(python) != pin["version"]:
        raise RuntimeRestoreError("toolchain_installed_but_does_not_run")
    return {"step": "toolchain", "state": "PASS", "path": str(python)}


# ---- release built from source --------------------------------------------------------------

def run_bootstrap(python: Path, release: Path, root: Path, lock_sha: str) -> None:
    command = [str(python), "-I", "-B", str(release / "runtime-bootstrap.pyz"), "--lock", str(release / "runtime-lock.json"),
               "--bundle", str(release), "--root", str(root), "--expected-lock-sha256", lock_sha]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=1800, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeRestoreError("bootstrap_did_not_run") from None
    try:
        report = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        report = {}
    if result.returncode != 0 or report.get("artifacts_installed") is not True:
        raise RuntimeRestoreError("bootstrap_install_failed:" + str(report.get("error", "no_report")))


def build_release(python: Path, config_repo: Path, spec_path: str, artifacts: Path) -> dict:
    """Cold build into a staging directory, then publish it as releases/<lock sha256>."""
    releases = artifacts / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:8]
    staging = releases / (".build-" + token)
    work = releases / (".w" + token)
    work.mkdir()
    try:
        command = [str(python), "-I", "-B", str(BUILDER), "--out", str(staging), "--config-repo", str(config_repo),
                   "--spec", spec_path, "--artifacts", str(artifacts), "--work", str(work)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=3600, creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeRestoreError("build_did_not_run") from None
        if result.returncode != 0:
            tail = (result.stderr.strip().splitlines() or ["no output"])[-1]
            raise RuntimeRestoreError("build_failed:" + tail[:400])
        lock_sha = read_json(staging / "build-report.json")["lock_sha256"]
        if sha256_file(staging / "runtime-lock.json") != lock_sha:
            raise RuntimeRestoreError("build_report_mismatch")
        release = releases / lock_sha
        if release.exists():
            if not (release / "runtime-lock.json").is_file() or sha256_file(release / "runtime-lock.json") != lock_sha:
                raise RuntimeRestoreError("release_directory_differs")
        else:
            os.rename(staging, release)
        return {"step": "release", "state": "PASS", "path": str(release), "lock_sha256": lock_sha}
    finally:
        for leftover in (staging, work):
            if leftover.exists():
                shutil.rmtree(leftover, ignore_errors=True)


def install(spec: dict, config_repo: Path, spec_path: str, artifacts: Path, root_name: str,
            archive: Path | None, offline: bool, plan: bool, from_root: str | None = None) -> dict:
    steps = []
    root = artifacts / root_name
    # Every generation's venv runs on the toolchain (pyvenv.cfg home), and so do the builder and launch.py.
    steps.append(toolchain(spec, archive, offline, plan))
    lock_sha = None
    if (root / "current.json").exists():
        try:
            lock_sha = runtime_launch.resolve(root)["receipt"].get("lock_sha256")
        except runtime_launch.LaunchError as error:
            raise RuntimeRestoreError("active_generation_does_not_verify:" + error.reason) from None
        steps.append({"step": "generation", "state": "ALREADY", "lock_sha256": lock_sha})
    elif from_root and (artifacts / from_root / "current.json").exists():
        # A second live root (the task authority's) installs the release the first root already runs.
        lock_sha = runtime_launch.resolve(artifacts / from_root)["receipt"].get("lock_sha256")
        if plan:
            steps.append({"step": "generation", "state": "PLAN", "path": str(root), "lock_sha256": lock_sha})
        else:
            run_bootstrap(expand(spec["toolchain"]), artifacts / "releases" / lock_sha, root, lock_sha)
            steps.append({"step": "generation", "state": "PASS", "path": str(root), "lock_sha256": lock_sha})
    elif plan:
        steps.append({"step": "release", "state": "PLAN", "path": str(artifacts / "releases")})
        steps.append({"step": "generation", "state": "PLAN", "path": str(root)})
    elif offline:
        raise RuntimeRestoreError("build_needs_downloads", state="WAIT")
    else:
        steps.append(build_release(expand(spec["toolchain"]), config_repo, spec_path, artifacts))
        lock_sha = steps[-1]["lock_sha256"]
        run_bootstrap(expand(spec["toolchain"]), Path(steps[-1]["path"]), root, lock_sha)
        steps.append({"step": "generation", "state": "PASS", "path": str(root)})
    if plan:
        steps.append({"step": "launcher", "state": "PLAN", "path": str(root / runtime_launch.LAUNCHER_NAME)})
    else:
        published = runtime_launch.publish(root)
        runtime_launch.resolve(root)
        steps.append({"step": "launcher", "state": "PASS" if published == "published" else "ALREADY",
                      "path": str(root / runtime_launch.LAUNCHER_NAME)})
    states = {s["state"] for s in steps}
    state = "PLAN" if "PLAN" in states else ("PASS" if "PASS" in states else "ALREADY")
    return {"state": state, "lock_sha256": lock_sha, "root": str(root), "steps": steps}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("toolchain", "install"):
        p = sub.add_parser(name)
        p.add_argument("--spec", type=Path, required=True)
        p.add_argument("--archive", type=Path, default=None, help="a local copy of the pinned toolchain archive")
        p.add_argument("--offline", action="store_true")
        p.add_argument("--plan", action="store_true")
        if name == "install":
            p.add_argument("--artifacts", type=Path, default=None, help="default: the spec's artifacts path")
            p.add_argument("--root-name", default="installed")
            p.add_argument("--from-root", default=None, help="reuse the release this installed root already runs")
            p.add_argument("--config-repo", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        spec = read_json(args.spec)
        if args.command == "toolchain":
            report = toolchain(spec, args.archive, args.offline, args.plan)
        else:
            artifacts = args.artifacts or expand(spec["artifacts"])
            spec_path = args.spec.resolve().relative_to(args.config_repo.resolve()).as_posix()
            report = install(spec, args.config_repo, spec_path, artifacts, args.root_name, args.archive,
                             args.offline, args.plan, args.from_root)
    except RuntimeRestoreError as error:
        report = {"state": error.state, "reason": error.reason}
    except runtime_launch.LaunchError as error:
        report = {"state": "FAIL", "reason": "launcher:" + error.reason}
    except OSError as error:
        report = {"state": "FAIL", "reason": "io_error:" + type(error).__name__}
    print(json.dumps(report, sort_keys=True))
    return {"WAIT": 4, "FAIL": 1}.get(report.get("state"), 0)


if __name__ == "__main__":
    sys.exit(main())
