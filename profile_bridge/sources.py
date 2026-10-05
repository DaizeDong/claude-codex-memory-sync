"""Bounded observation of caller-selected sources, with independent staged bytes.

Double reads detect observed churn, including same-content file replacement.
They cannot prove a global point in time or detect an unobserved A-B-A change.
No source discovery or registry interpretation lives in this module.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time
import uuid
from contextvars import ContextVar

from .filesystem import atomic_replace, read_bounded, validate_path, UnsafePathError, link_state

_DEADLINE = ContextVar("profile_source_deadline", default=None)
MAX_SOURCE_SECONDS = 300


def remaining_seconds(maximum):
    """Budget for a supplier's bounded metadata I/O during the same freeze."""
    deadline = _DEADLINE.get()
    if deadline is None:
        return maximum
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BusySources("source_deadline_exhausted")
    return min(maximum, remaining)


def checkpoint(point, attempt):
    """Fault-injection boundary; production has no callback or external action."""


class BusySources(RuntimeError):
    pass


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _deadline(deadline):
    if time.monotonic() >= deadline:
        raise BusySources("source_deadline_exhausted")


def _round(request, deadline, progress=None):
    progress = {} if progress is None else progress
    progress.update(phase="source_discovery", completed_members=0)
    records = request["sources"]() if callable(request["sources"]) else request["sources"]
    observations, payload = {}, {}
    total = 0
    for record in records:
        _deadline(deadline)
        progress["phase"] = "source_read"
        source_id, rel = record["source_id"], record["relative_path"]
        if not isinstance(source_id, str) or not source_id or not isinstance(rel, str):
            raise ValueError("source_identity_required")
        key = (source_id, rel)
        if key in observations or len(observations) >= request.get("max_members", 100000):
            raise ValueError("duplicate_or_excess_source_members")
        path = Path(record["path"]).absolute()
        validate_path(path.parent)
        info = path.lstat()
        linked = stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)
        kind = "link" if linked else "directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "unsupported"
        if kind == "unsupported":
            raise UnsafePathError("unsupported_source_type")
        row = {"source_id": source_id, "relative_path": rel, "path": str(path), "kind": kind,
               "identity": [info.st_dev, info.st_ino], "size": info.st_size,
               "mtime_ns": info.st_mtime_ns, "policy": record.get("policy", request["policy"]),
               "metadata": record.get("metadata", {})}
        if kind == "link":
            row["link_state"] = link_state(path)
        elif kind == "file":
            data = read_bounded(path, request.get("max_file_bytes", 64 * 1024 * 1024))
            total += len(data)
            if total > request.get("max_total_bytes", 512 * 1024 * 1024):
                raise ValueError("source_total_size_exceeded")
            row["raw_sha256"] = _hash(data)
            payload[key] = data
        else:
            validate_path(path)
        after = path.lstat()
        if (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns):
            raise BusySources("source_changed_during_read")
        observations[key] = row
        progress["completed_members"] = len(observations)
        _deadline(deadline)
        progress.update(phase="source_discovery", completed_members=len(observations))
    _deadline(deadline)
    return observations, payload


def freeze(request: dict) -> dict:
    """Freeze explicit records (or their membership supplier) to an empty stage.

    Required: sources (a reusable collection or fresh membership factory),
    stage, policy. The caller owns stage lifetime. No input
    bytes are scanned or normalized. A caller-provided boolean never establishes
    quiescence: this API currently reports only observed_stable.
    The default budget is 30 seconds; explicit larger selections may request up
    to MAX_SOURCE_SECONDS, shared by all observations, retries and staging.
    """
    if not isinstance(request.get("policy"), str) or not request["policy"]:
        raise ValueError("source_policy_required")
    records = request["sources"]
    if not callable(records) and iter(records) is records:
        raise ValueError("source_iterator_requires_reusable_collection_or_factory")
    stage = validate_path(request["stage"])
    if stage.exists():
        raise ValueError("source_stage_must_be_absent")
    attempts = request.get("attempts", 3)
    seconds = request.get("seconds", 30)
    if (type(attempts) is not int or not 1 <= attempts <= 3 or type(seconds) not in (int, float)
            or not math.isfinite(seconds) or not 0 < seconds <= MAX_SOURCE_SECONDS):
        raise ValueError("invalid_source_budget")
    deadline = time.monotonic() + seconds
    token = _DEADLINE.set(deadline)
    try:
        return _freeze(request, stage, attempts, deadline)
    finally:
        _DEADLINE.reset(token)


def _freeze(request, stage, attempts, deadline):
    progress = {}
    for attempt in range(1, attempts + 1):
        try:
            first, _ = _round(request, deadline, progress)
            progress["phase"] = "between_observations"
            checkpoint("between_rounds", attempt)
            second, payload = _round(request, deadline, progress)
            progress["phase"] = "observation_comparison"
            if first != second:
                raise BusySources("source_observations_changed")
            checkpoint("accepted", attempt)
            progress["phase"] = "staging"
            # Fresh files, never hard links to the accepted source objects.
            stage.mkdir(parents=True, exist_ok=False)
            rows = []
            for key, observation in sorted(second.items()):
                _deadline(deadline)
                row = dict(observation)
                if key in payload:
                    staged = stage / (str(len(rows)) + ".bin")
                    atomic_replace(staged, payload[key])
                    row["staged_path"] = str(staged)
                rows.append(row)
            _deadline(deadline)
            # Runtime inode/mtime evidence is for comparing observations, not
            # portable identity or an input to restored ownership receipts.
            progress["phase"] = "generation"
            generation = _hash(_encoded([{k: v for k, v in row.items() if k not in {"identity", "mtime_ns", "size", "staged_path", "path"}} for row in rows]))
            _deadline(deadline)
            return {"status": "frozen", "consistency": "observed_stable", "policy": request["policy"],
                    "source_generation": generation, "attempts": attempt, "members": rows, "stage": str(stage)}
        except (BusySources, FileNotFoundError, NotADirectoryError, UnsafePathError, PermissionError) as exc:
            if isinstance(exc, BusySources):
                reason = str(exc) if str(exc) in {"source_deadline_exhausted", "source_changed_during_read",
                    "source_observations_changed"} else "source_observation_failed"
            elif isinstance(exc, PermissionError):
                reason = "source_access_denied"
            elif isinstance(exc, UnsafePathError):
                reason = "source_changed_during_read" if str(exc) == "file_changed" else "source_unsafe_path"
            else:
                reason = "source_missing_or_changed"
            if isinstance(exc, PermissionError):
                break
            if stage.exists() or time.monotonic() >= deadline:
                break
    return {"status": "busy_sources", "reason": reason, "attempts": attempt, **progress}
