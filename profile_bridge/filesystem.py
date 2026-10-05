"""Profile consumers use the shared bounded-read and publication primitives."""
from fleet_guards.filesystem import (
    UnsafePathError, FileTooLargeError, atomic_replace, create_no_replace,
    create_no_replace_with_identity, detach_if_matches, identity,
    read_bounded, sync_directory, validate_path,
)


def link_state(path):
    """Immediate link facts without traversing the target."""
    import os
    from pathlib import Path
    import stat
    path = Path(path)
    validate_path(path.parent)
    info = path.lstat()
    symlink = stat.S_ISLNK(info.st_mode)
    if not symlink and not getattr(info, "st_file_attributes", 0) & 0x400:
        raise UnsafePathError("not_a_link")
    result = {"kind": "link", "target": os.readlink(path),
              "link_type": "symlink" if symlink else "junction"}
    if os.name == "nt" and symlink:
        result["directory"] = bool(info.st_file_attributes & 0x10)
    return result
