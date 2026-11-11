"""The local embedding model, downloaded once and shared by every profile.

Hermes profiles live under ``$HERMES_HOME/profiles/<name>``. The cache is keyed
off the root home, so two profiles reuse the same ~320 MB model instead of
downloading it twice.

Downloads are resumable: bytes go to ``<file>.part`` and a Range request
continues from there. A file is accepted only at its exact expected size; a
short or oversized response is an error, never a silently corrupted model. No
download starts without an explicit consent flag.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("carnet.localmodel")

REPO = "onnx-community/embeddinggemma-300m-ONNX"
URL_TEMPLATE = "https://huggingface.co/{repo}/resolve/main/{path}"

# Path, exact byte size, from the repository metadata (checked 2026-11-10).
# model_quantized.onnx carries its weights in the .onnx_data file beside it.
FILES: list[tuple[str, int]] = [
    ("tokenizer.json", 20_323_312),
    ("tokenizer.model", 4_689_074),
    ("tokenizer_config.json", 1_156_830),
    ("config.json", 1_765),
    ("special_tokens_map.json", 662),
    ("added_tokens.json", 35),
    ("generation_config.json", 133),
    ("onnx/model_quantized.onnx", 567_874),
    ("onnx/model_quantized.onnx_data", 308_890_624),
]
TOTAL = sum(size for _, size in FILES)

_lock = threading.Lock()
_state = {"running": False, "error": ""}


def shared_home(home: str | Path | None = None) -> Path:
    """The home that all profiles share, not the profile's own folder."""
    base = Path(home or os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    return base.parent.parent if base.parent.name == "profiles" else base


def cache_dir(home: str | Path | None = None) -> Path:
    return shared_home(home) / "cache" / "carnet" / "embeddinggemma-300m-ONNX"


def _url(path: str) -> str:
    return URL_TEMPLATE.format(repo=REPO, path=urllib.parse.quote(path, safe="/"))


def _inspect(folder: Path) -> tuple[int, int, list[str]]:
    """(complete bytes, partial bytes, missing paths)."""
    done = partial = 0
    missing: list[str] = []
    for rel, size in FILES:
        dest = folder / rel
        part = dest.with_name(dest.name + ".part")
        if dest.exists() and dest.stat().st_size == size:
            done += size
            continue
        missing.append(rel)
        if part.exists():
            partial += min(part.stat().st_size, size)
    return done, partial, missing


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
        "files": len(FILES),
        "missing": missing,
        "state": state,
        "error": error,
        "percent": round(100.0 * (done + partial) / TOTAL, 1) if TOTAL else 0.0,
    }


def _fetch(rel: str, dest: Path, expected: int) -> None:
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    have = part.stat().st_size if part.exists() else 0
    if have > expected:
        part.unlink()
        have = 0
    if have == expected:
        part.replace(dest)
        return
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
    part.replace(dest)


def download(home: str | Path | None = None, *, consent: bool = False,
             progress=None) -> dict:
    """Fetch every missing file. Refuses without explicit consent."""
    if not consent:
        raise PermissionError("downloading the local embedding model requires consent")
    folder = cache_dir(home)
    folder.mkdir(parents=True, exist_ok=True)
    for rel, size in FILES:
        dest = folder / rel
        if dest.exists() and dest.stat().st_size == size:
            continue
        _fetch(rel, dest, size)
        if progress:
            progress(rel, size)
    _, _, missing = _inspect(folder)
    if missing:
        raise OSError(f"incomplete model after download: {', '.join(missing)}")
    (folder / "manifest.json").write_text(json.dumps({
        "repo": REPO, "files": {rel: size for rel, size in FILES},
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
