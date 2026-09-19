"""Exercise real source mutations between observations, using synthetic bytes."""
import os
from pathlib import Path
import subprocess
import pytest
from profile_bridge import sources


def request_for(root, stage):
    def records():
        yield {"source_id": "synthetic-source", "relative_path": ".", "path": root}
        for path in sorted(root.rglob("*")):
            yield {"source_id": "synthetic-source", "relative_path": path.relative_to(root).as_posix(), "path": path}
    return {"sources": records, "stage": stage, "policy": "synthetic-v1"}


@pytest.mark.parametrize("mutation", ["content", "replacement", "membership", "type"])
def test_churn_exhaustion_has_no_accepted_stage(tmp_path, monkeypatch, mutation):
    root = tmp_path / "source"; root.mkdir()
    file = root / "member"; file.write_bytes(b"same bytes")
    stage = tmp_path / "stage"
    def change(point, attempt):
        if point != "between_rounds":
            return
        if mutation == "content":
            file.write_bytes(str(attempt).encode())
        elif mutation == "replacement":
            replacement = root / "replacement"
            replacement.write_bytes(file.read_bytes())
            info = file.stat()
            os.utime(replacement, ns=(info.st_atime_ns, info.st_mtime_ns))
            os.replace(replacement, file)
        elif mutation == "membership":
            (root / str(attempt)).write_bytes(b"member")
        else:
            if file.is_dir():
                file.rmdir(); file.write_bytes(b"same bytes")
            else:
                file.unlink(); file.mkdir()
    monkeypatch.setattr(sources, "checkpoint", change)
    result = sources.freeze(request_for(root, stage))
    assert result["status"] == "busy_sources" and result["attempts"] == 3
    assert not stage.exists()


def test_one_replacement_retries_and_stage_is_independent(tmp_path, monkeypatch):
    root = tmp_path / "source"; root.mkdir()
    file = root / "member"; file.write_bytes(b"accepted")
    def replace(point, attempt):
        if point == "between_rounds" and attempt == 1:
            replacement = root / "replacement"; replacement.write_bytes(b"accepted")
            os.replace(replacement, file)
    monkeypatch.setattr(sources, "checkpoint", replace)
    request = request_for(root, tmp_path / "stage")
    request["quiesced"] = True  # A caller assertion is not cooperative evidence.
    result = sources.freeze(request)
    assert result["status"] == "frozen" and result["attempts"] == 2
    assert result["consistency"] == "observed_stable"
    staged = Path(next(r["staged_path"] for r in result["members"] if r["kind"] == "file"))
    assert staged.stat().st_ino != file.stat().st_ino
    file.write_bytes(b"later change")
    assert staged.read_bytes() == b"accepted"


def test_time_budget_and_explicit_policy(tmp_path, monkeypatch):
    root = tmp_path / "source"; root.mkdir(); (root / "member").write_bytes(b"synthetic")
    request = request_for(root, tmp_path / "stage")
    with pytest.raises(ValueError):
        sources.freeze({**request, "attempts": 4})
    with pytest.raises(ValueError):
        sources.freeze({**request, "seconds": 301})
    with pytest.raises(ValueError):
        sources.freeze({**request, "policy": ""})
    ticks = iter([0, 31, 31])
    monkeypatch.setattr(sources.time, "monotonic", lambda: next(ticks))
    assert sources.freeze(request)["status"] == "busy_sources"


def test_actual_link_target_churn_is_not_a_stable_observation(tmp_path, monkeypatch):
    root = tmp_path / "source"; root.mkdir()
    targets = [tmp_path / "one", tmp_path / "two"]
    for target in targets:
        target.mkdir(); (target / "file").write_bytes(b"same content")
    link = root / "link"
    def link_to(target):
        if os.path.lexists(link):
            link.rmdir() if os.name == "nt" else link.unlink()
        if os.name == "nt":
            process = subprocess.run(["powershell", "-NoProfile", "-Command",
                "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path $env:TEST_LINK -Target $env:TEST_TARGET | Out-Null"],
                env=dict(os.environ, TEST_LINK=str(link), TEST_TARGET=str(target)), capture_output=True, timeout=15)
            assert process.returncode == 0, process.stderr
        else:
            link.symlink_to(target, target_is_directory=True)
    link_to(targets[0])
    def churn(point, attempt):
        if point == "between_rounds":
            link_to(targets[attempt % 2])
    monkeypatch.setattr(sources, "checkpoint", churn)
    request = {"sources": [{"source_id": "synthetic-link", "relative_path": "link", "path": link}],
               "stage": tmp_path / "stage", "policy": "synthetic-v1"}
    assert sources.freeze(request)["status"] == "busy_sources"
