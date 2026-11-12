"""The local embedding model, downloaded once and shared by every profile.

Hermes profiles live under ``$HERMES_HOME/profiles/<name>``. The cache is keyed
off the root home, so two profiles reuse the same ~320 MB model instead of
downloading it twice.

Downloads are resumable: bytes go to ``<file>.part`` and a Range request
continues from there. A file is accepted only at its exact expected size **and**
its exact sha256; a short response, an oversized one or a changed payload is an
error, never a silently corrupted model. No download starts without an explicit
consent flag.

The repository is pinned to a commit, not to ``main``: a branch can move under
us, and a memory provider must not adopt a different model without saying so.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("carnet.localmodel")

REPO = "onnx-community/embeddinggemma-300m-ONNX"
# Pinned commit, not a branch: `main` can be rewritten between two installs.
COMMIT = "5090578d9565bb06545b4552f76e6bc2c93e4a66"
URL_TEMPLATE = "https://huggingface.co/{repo}/resolve/{commit}/{path}"

# Path, exact byte size, sha256 of the file as published at COMMIT.
# model_quantized.onnx carries its weights in the .onnx_data file beside it.
FILES: list[tuple[str, int, str]] = [
    ("tokenizer.json", 20_323_312,
     "4dda02faaf32bc91031dc8c88457ac272b00c1016cc679757d1c441b248b9c47"),
    ("tokenizer.model", 4_689_074,
     "1299c11d7cf632ef3b4e11937501358ada021bbdf7c47638d13c0ee982f2e79c"),
    ("tokenizer_config.json", 1_156_830,
     "3ca953eea6c3c9fcda9cf3df22949ff18b216f7c74bd6459230f3f1013953f3a"),
    ("config.json", 1_765,
     "6e1f06404b7163e0325ed2ea3e6781cde50f4a50b31780a95ad0d30e8404d77b"),
    ("special_tokens_map.json", 662,
     "2f7b0adf4fb469770bb1490e3e35df87b1dc578246c5e7e6fc76ecf33213a397"),
    ("added_tokens.json", 35,
     "50b2f405ba56a26d4913fd772089992252d7f942123cc0a034d96424221ba946"),
    ("generation_config.json", 133,
     "1fb1efd221c1ca88a736d1b36cb47d754c177677e222acb3b1e5424c5d664870"),
    ("onnx/model_quantized.onnx", 567_874,
     "172efde319fe1542dc41f31be6154910b05b78f7a861c265c4600eec906bd6d8"),
    ("onnx/model_quantized.onnx_data", 308_890_624,
     "705626e28e4c23c82ade34566b4197d97f534c12275fa406dfb71e9937d388c0"),
]
TOTAL = sum(size for _, size, _sha in FILES)

_lock = threading.Lock()
_state = {"running": False, "error": ""}


def _hermes_home() -> Path:
    """Resolve the Hermes home at call time, never at import time.

    The gateway can switch profile while it runs; a module constant would pin the
    first home it saw and write the model cache under the wrong one.
    """
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def shared_home(home: str | Path | None = None) -> Path:
    """The home that all profiles share, not the profile's own folder."""
    base = Path(home) if home else _hermes_home()
    return base.parent.parent if base.parent.name == "profiles" else base


def cache_dir(home: str | Path | None = None) -> Path:
    return shared_home(home) / "cache" / "carnet" / "embeddinggemma-300m-ONNX"


def _url(path: str) -> str:
    return URL_TEMPLATE.format(repo=REPO, commit=COMMIT,
                               path=urllib.parse.quote(path, safe="/"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inspect(folder: Path) -> tuple[int, int, list[str]]:
    """(complete bytes, partial bytes, missing paths)."""
    done = partial = 0
    missing: list[str] = []
    for rel, size, _sha in FILES:
        dest = folder / rel
        part = dest.with_name(dest.name + ".part")
        if dest.exists() and dest.stat().st_size == size:
            done += size
            continue
        missing.append(rel)
        if part.exists():
            partial += min(part.stat().st_size, size)
    return done, partial, missing


def verify(home: str | Path | None = None) -> dict:
    """Hash every file against its pinned sha256. Slow, explicit, on demand.

    `status()` only checks sizes: a truncated or bit-rotted file of the right
    length passes. This is the check that actually answers "is the model intact".
    """
    folder = cache_dir(home)
    bad: list[str] = []
    checked = 0
    for rel, size, sha in FILES:
        dest = folder / rel
        if not dest.exists() or dest.stat().st_size != size:
            bad.append(rel)
            continue
        digest = hashlib.sha256()
        with open(dest, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
        if digest.hexdigest() != sha:
            bad.append(rel)
        else:
            checked += 1
    # Tied to the files it checked: a later write of the same size must not
    # inherit the verdict of the previous content.
    _state["verified"] = _signature(folder) if not bad else None
    return {"checked": checked, "files": len(FILES), "bad": bad,
            "intact": not bad}


def _signature(folder: Path) -> tuple:
    """(size, mtime) of every model file: what a verdict is valid for."""
    parts = []
    for rel, size, _sha in FILES:
        try:
            st = (folder / rel).stat()
            # Nanoseconds: a same-second rewrite is exactly the case this guards.
            parts.append((rel, st.st_size, st.st_mtime_ns))
        except OSError:
            parts.append((rel, -1, -1))
    return tuple(parts)


def _is_verified(folder: Path) -> bool:
    return bool(_state.get("verified")) and _state["verified"] == _signature(folder)


def status(home: str | Path | None = None) -> dict:
    folder = cache_dir(home)
    done, partial, missing = _inspect(folder)
    running = bool(_state["running"])
    error = str(_state["error"] or "")
    state = "downloading" if running else ("error" if error else ("ready" if not missing else "absent"))
    return {
        "repo": REPO,
        "path": str(folder),
        "expected": TOTAL,
        "bytes": done,
        "part_bytes": partial,
        "downloaded": not missing,
        # A size match is not an integrity check: say which one this is, so the
        # page never claims more than it knows.
        "integrity": "verified" if _is_verified(folder) else "size-only",
        "files": len(FILES),
        "missing": missing,
        "state": state,
        "error": error,
        "percent": round(100.0 * (done + partial) / TOTAL, 1) if TOTAL else 0.0,
    }


def _fetch(rel: str, dest: Path, expected: int, sha: str) -> None:
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    have = part.stat().st_size if part.exists() else 0
    if have > expected:
        part.unlink()
        have = 0
    if have == expected:
        # A complete part is only accepted if it is the file we asked for.
        if _sha256(part) == sha:
            part.replace(dest)
            return
        log.warning("carnet: %s.part does not match its checksum, starting over", dest.name)
        part.unlink()
        have = 0
    headers = {"User-Agent": "carnet"}
    if have:
        headers["Range"] = f"bytes={have}-"
    request = urllib.request.Request(_url(rel), headers=headers)
    with urllib.request.urlopen(request, timeout=120) as response:
        if have and getattr(response, "status", 200) == 206:
            mode = "ab"
        else:
            if have:
                log.warning("carnet: server ignored the Range request for %s, restarting", rel)
            have, mode = 0, "wb"
        declared = response.headers.get("Content-Length")
        if declared is not None and int(declared) != expected - have:
            raise OSError(f"{rel}: expected {expected - have} bytes, server announces {declared}")
        with open(part, mode) as out:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
    size = part.stat().st_size
    if size != expected:
        raise OSError(f"{rel}: expected {expected} bytes, got {size}")
    digest = _sha256(part)
    if digest != sha:
        part.unlink()          # a resumed download must not loop on a bad file
        raise OSError(f"{rel}: sha256 mismatch, got {digest[:12]}… expected {sha[:12]}…")
    part.replace(dest)


def download(home: str | Path | None = None, *, consent: bool = False,
             progress=None) -> dict:
    """Fetch every missing file. Refuses without explicit consent."""
    if not consent:
        raise PermissionError("downloading the local embedding model requires consent")
    folder = cache_dir(home)
    folder.mkdir(parents=True, exist_ok=True)
    for rel, size, sha in FILES:
        dest = folder / rel
        if dest.exists() and dest.stat().st_size == size:
            if _sha256(dest) != sha:
                log.warning("carnet: %s is complete but corrupt, fetching it again", rel)
                dest.unlink()
            else:
                continue
        _fetch(rel, dest, size, sha)
        if progress:
            progress(rel, size)
    _, _, missing = _inspect(folder)
    if missing:
        raise OSError(f"incomplete model after download: {', '.join(missing)}")
    (folder / "manifest.json").write_text(json.dumps({
        "repo": REPO, "commit": COMMIT,
        "files": {rel: {"size": size, "sha256": sha} for rel, size, sha in FILES},
        "total": TOTAL,
    }, indent=2) + "\n", encoding="utf-8")
    with _lock:
        _state["error"] = ""
    return status(home)


def start_download(home: str | Path | None = None) -> dict:
    """Start the download in the background; idempotent while it runs."""
    with _lock:
        if _state["running"]:
            return status(home)
        _state.update(running=True, error="")
    def _run():
        try:
            download(home, consent=True)
        except Exception as exc:
            _state["error"] = f"{type(exc).__name__}: {exc}"
            log.warning("carnet: model download failed: %s", _state["error"])
        finally:
            with _lock:
                _state["running"] = False
    threading.Thread(target=_run, name="carnet-model-download", daemon=True).start()
    return status(home)
