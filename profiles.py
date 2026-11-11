"""Shared plugin code across Hermes profiles.

Hermes discovers plugins inside each profile's ``plugins/`` folder. Installing
the code once, in a shared folder, and linking every profile to it keeps a
single copy of the source, while each profile keeps its own data in
``<profile>/carnet/``.

A link is a symbolic link named ``carnet`` inside ``<profile>/plugins/``. A real
directory is never replaced: it belongs to the user.
"""
from __future__ import annotations

import os
from pathlib import Path

# The folder that holds this module is the shared code folder.
CODE_DIR = Path(__file__).resolve().parent


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def profile_homes(root: Path | None = None) -> list[Path]:
    """Every profile that can host plugins: the main home and ``profiles/*``."""
    base = Path(root or hermes_home())
    homes = [base]
    profiles = base / "profiles"
    if profiles.is_dir():
        homes += sorted(p for p in profiles.iterdir() if p.is_dir())
    return homes


def link_profile(profile: Path, code_dir: Path | None = None) -> dict:
    """Create ``<profile>/plugins/carnet -> code_dir``. Idempotent."""
    code = Path(code_dir or CODE_DIR)
    target = Path(profile) / "plugins" / "carnet"
    if target.is_symlink():
        if target.resolve() == code.resolve():
            return {"profile": str(profile), "path": str(target), "status": "already"}
        target.unlink()
        target.symlink_to(code, target_is_directory=True)
        return {"profile": str(profile), "path": str(target), "status": "relinked"}
    if target.exists():
        return {"profile": str(profile), "path": str(target), "status": "conflict"}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(code, target_is_directory=True)
    return {"profile": str(profile), "path": str(target), "status": "linked"}


def unlink_profile(profile: Path) -> dict:
    """Remove the symlink only. A real folder is left untouched."""
    target = Path(profile) / "plugins" / "carnet"
    if target.is_symlink():
        target.unlink()
        return {"profile": str(profile), "path": str(target), "status": "unlinked"}
    if target.exists():
        return {"profile": str(profile), "path": str(target), "status": "conflict"}
    return {"profile": str(profile), "path": str(target), "status": "absent"}


def link_all(root: Path | None = None, code_dir: Path | None = None) -> list[dict]:
    return [link_profile(p, code_dir) for p in profile_homes(root)]


def unlink_all(root: Path | None = None) -> list[dict]:
    return [unlink_profile(p) for p in profile_homes(root)]
