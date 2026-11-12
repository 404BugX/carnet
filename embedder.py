"""Text encoder + vector search, client side.

Four modes, same interface:

- ``disabled`` — no vectors at all; recall stays lexical.
- ``lmstudio`` (default) — POST /v1/embeddings on the local LM Studio server.
  One local process for everything (embeddings + small models), no more sidecar,
  no more onnxruntime, no more venv to purge. Vectors live in ``vec``.
- ``custom`` — any OpenAI-compatible /v1/embeddings endpoint (URL, model, key).
- ``sidecar`` — ``sidecar/embed_server.py`` in the Hermes venv, running the local
  ONNX model downloaded once in the shared profile cache.

Similarity is computed here, in pure Python: the plugin runtime has no numpy.
``sum(map(mul, a, b))`` over ``array('f')`` stays C-level, so ~2 ms for 150 facts
and ~300 ms for 10 000 — within a turn's budget.

Golden rule: no waiting on the hot path. If the backend is not ready,
``search()`` returns an empty list and the provider continues lexically.
"""
from __future__ import annotations

import atexit
import contextlib
import json
import queue
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from array import array
from operator import mul
from pathlib import Path
from typing import Optional

log = logging.getLogger("carnet.embedder")

PYTHON_CANDIDATES = [
    Path.home() / ".hermes/hermes-agent/venv/bin/python",
    Path.home() / ".hermes/venv/bin/python",
]

DEFAULT_URL = "http://127.0.0.1:1234"
DEFAULT_MODEL = "text-embedding-embeddinggemma-300m-qat"


def find_python(forced: str = "") -> Optional[str]:
    if forced:
        return forced if Path(forced).exists() else None
    env = os.environ.get("CARNET_PYTHON")
    if env and Path(env).exists():
        return env
    for p in PYTHON_CANDIDATES:
        if p.exists():
            return str(p)
    return shutil.which("python3")


def _cosine(a: array, b: array) -> float:
    """Cosine similarity. Both vectors are already normalized at encoding time."""
    return sum(map(mul, a, b))

# -- keep-alive -------------------------------------------------------------
# Loading the ONNX model takes several seconds. Tying that cost to the provider
# object meant paying it again at the start of every conversation, so the
# sidecar now belongs to the whole gateway: one process, shared by every session,
# released after KEEPALIVE_SECONDS of silence or as soon as the machine runs
# short on memory.
KEEPALIVE_SECONDS = 30 * 60
MEMORY_CEILING_PERCENT = 92.0
_WATCH_INTERVAL_SECONDS = 60

_RUNTIME: dict = {}
_RUNTIME_LOCK = threading.Lock()
_WATCH_STARTED = False


def _memory_percent() -> Optional[float]:
    """Share of physical memory in use, or None when it cannot be read."""
    try:
        import psutil  # type: ignore

        return float(psutil.virtual_memory().percent)
    except Exception:
        pass
    try:
        total = os.sysconf("SC_PHYS_PAGES")
        available = os.sysconf("SC_AVPHYS_PAGES")
        if total <= 0 or available < 0:
            return None
        return max(0.0, min(100.0, (1.0 - available / total) * 100.0))
    except (ValueError, OSError, AttributeError):
        return None


def _kill_sidecar(proc) -> None:
    try:
        if proc.stdin:
            proc.stdin.write('{"op":"quit"}\n')
            proc.stdin.flush()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _watch_once() -> None:
    with _RUNTIME_LOCK:
        entries = list(_RUNTIME.items())
    for key, entry in entries:
        proc = entry.get("proc")
        if proc is None or proc.poll() is not None:
            with _RUNTIME_LOCK:
                _RUNTIME.pop(key, None)
            continue
        idle = time.monotonic() - entry.get("touched", 0.0)
        pressure = _memory_percent()
        reason = ""
        if idle >= KEEPALIVE_SECONDS:
            reason = "idle for {:.0f} min".format(idle / 60)
        elif pressure is not None and pressure >= MEMORY_CEILING_PERCENT and idle > 60:
            reason = "memory at {:.0f}%".format(pressure)
        if not reason:
            continue
        log.info("carnet: unloading embedding model (%s)", reason)
        _kill_sidecar(proc)
        with _RUNTIME_LOCK:
            _RUNTIME.pop(key, None)


def _ensure_watch() -> None:
    global _WATCH_STARTED
    with _RUNTIME_LOCK:
        if _WATCH_STARTED:
            return
        _WATCH_STARTED = True

    def loop() -> None:
        while True:
            time.sleep(_WATCH_INTERVAL_SECONDS)
            try:
                _watch_once()
            except Exception as exc:
                log.warning("carnet: embedding watchdog: %s", exc)

    threading.Thread(target=loop, name="carnet-embed-watch", daemon=True).start()

    # A warm sidecar must never outlive the gateway that owns it.
    def _release_all() -> None:
        for entry in list(_RUNTIME.values()):
            proc = entry.get("proc")
            if proc is not None and proc.poll() is None:
                _kill_sidecar(proc)

    atexit.register(_release_all)


class Embedder:
    # Loading a 300 MB ONNX model takes a few seconds on a laptop: the startup
    # line is waited for this long, then the child is killed rather than trusted.
    START_TIMEOUT = 45.0

    def __init__(self, db: Path, server: Path, python: str = "", timeout_ms: int = 300,
                 backend: str = "lmstudio", url: str = DEFAULT_URL, model: str = DEFAULT_MODEL,
                 api_key: str = "", model_dir: str = ""):
        self.db = db
        self.server = server
        self.python = find_python(python)
        self.timeout = timeout_ms / 1000.0
        self.backend = (backend or "lmstudio").strip().lower()
        self.url = (url or DEFAULT_URL).rstrip("/")
        self.model = model or DEFAULT_MODEL
        self.api_key = (api_key or "").strip()
        self.model_dir = Path(model_dir) if model_dir else None
        self.proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._restart_lock = threading.Lock()
        self._error = ""
        self._state = {"dim": 0, "n": 0}
        self._cache: dict[str, array] = {}
        self._cache_loaded = False
        # One sidecar per database *and* model: two profiles sharing a gateway
        # must not adopt each other's vectors.
        self._key = (str(self.model_dir or ""), str(self.db))

    # -- lifecycle ---------------------------------------------------
    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    @property
    def error(self) -> str:
        return self._error

    def start_in_background(self) -> None:
        """Prepare the backend without blocking the provider's initialization."""
        if self.backend == "disabled":
            return
        if self.backend == "sidecar" and self._adopt():
            return
        targets = {
            "lmstudio": self._start_lmstudio,
            "custom": self._start_custom,
            "sidecar": self._start_sidecar,
        }
        target = targets.get(self.backend, self._start_lmstudio)
        threading.Thread(target=target, name="carnet-embed-start", daemon=True).start()

    def stop(self, force: bool = False) -> None:
        """Let go of the model without paying for it twice.

        Session teardown calls this, and the next session would have to reload
        the whole ONNX graph. The process therefore stays warm and is released by
        the watchdog instead, on silence or memory pressure.
        """
        if self.backend != "sidecar":
            return
        self._ready.clear()
        if not force:
            with _RUNTIME_LOCK:
                entry = _RUNTIME.get(self._key)
            if entry is not None and entry.get("proc") is not None and entry["proc"].poll() is None:
                entry["touched"] = time.monotonic()
                self.proc = None
                return
        p = self.proc
        self.proc = None
        with _RUNTIME_LOCK:
            _RUNTIME.pop(self._key, None)
        if p:
            _kill_sidecar(p)

    def _adopt(self) -> bool:
        """Reuse the sidecar already running for this database and model."""
        _ensure_watch()
        with _RUNTIME_LOCK:
            entry = _RUNTIME.get(self._key)
        if not entry:
            return False
        proc = entry.get("proc")
        if proc is None or proc.poll() is not None:
            with _RUNTIME_LOCK:
                _RUNTIME.pop(self._key, None)
            return False
        self.proc = proc
        self._state = dict(entry.get("state") or {"dim": 0, "n": 0})
        entry["touched"] = time.monotonic()
        self._ready.set()
        return True

    def _read_line(self, wait: float) -> Optional[str]:
        """One line from the child, or None after `wait` seconds.

        Reading the text stream and waiting on its file descriptor do not agree:
        the wrapper can hold whole lines while select() sees nothing, which made
        a healthy child look dead whenever two lines arrived in one burst. A
        reader thread owns the stream, and every caller waits on a queue.
        """
        try:
            return self._lines.get(timeout=max(0.0, wait))
        except queue.Empty:
            return None

    def _start_reader(self) -> None:
        stream = self.proc.stdout if self.proc else None
        if stream is None:
            return
        # The queue is captured by the pump, never looked up through `self`:
        # after a restart the old pump must not push its end-of-stream marker
        # into the new generation's queue, where a caller would read it as "the
        # child died" and kill a healthy sidecar.
        lines = queue.Queue()
        self._lines = lines

        def _pump():
            try:
                for line in stream:
                    lines.put(line)
            except Exception:
                pass
            finally:
                lines.put(None)            # this child is gone

        reader = threading.Thread(target=_pump, name="carnet-embed-reader", daemon=True)
        reader.start()

    def _restart(self) -> None:
        """Start the child again after a timeout or a crash. One attempt.

        Readiness is not a gate here: after a crash the flag can still be set,
        and refusing to restart because "we are ready" is how the channel stayed
        dead for the rest of the process. A live child is the only reason not to.
        """
        if self.backend != "sidecar":
            return
        if self.proc is not None and self.proc.poll() is None:
            self._ready.clear()
            _kill_sidecar(self.proc)
            self.proc = None
        with self._restart_lock:
            now = time.monotonic()
            if now - getattr(self, "_last_restart", 0.0) < 5.0:
                return                  # never a spawn storm, never two children
            self._last_restart = now
        try:
            self._start_sidecar()
        except Exception as exc:
            log.warning("carnet: embedding server restart failed: %s", exc)

    def _touch(self) -> None:
        with _RUNTIME_LOCK:
            entry = _RUNTIME.get(self._key)
        if entry is not None:
            entry["touched"] = time.monotonic()

    def _endpoint(self, suffix: str) -> str:
        """Accept a base URL, a /v1 URL or a full endpoint URL."""
        base = self.url.rstrip("/")
        if base.endswith(suffix):
            return base
        if base.endswith("/v1"):
            return base + suffix
        return base + "/v1" + suffix

    # -- HTTP backends (LM Studio and custom) ------------------------
    def _http(self, endpoint: str, body: Optional[dict] = None, timeout: float = 10.0) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            self._endpoint(endpoint), data=data, headers=headers,
            method="POST" if data else "GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as rep:
            return json.loads(rep.read().decode())

    def _start_lmstudio(self) -> None:
        try:
            models = [m.get("id") for m in self._http("/models", timeout=3).get("data", [])]
            if self.model not in models:
                self._error = f"model {self.model} missing from LM Studio"
                return
            # A first encoding forces the model load (14 s cold, ~20 ms afterwards).
            vec = self._encode_http(["warmup"])[0]
            self._state = {"dim": len(vec), "n": self.vector_count()}
            self._ready.set()
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            log.warning("carnet: LM Studio unavailable: %s", self._error)

    def _start_custom(self) -> None:
        try:
            # No /models probe: many OpenAI-compatible endpoints do not expose one.
            vec = self._encode_http(["warmup"])[0]
            self._state = {"dim": len(vec), "n": self.vector_count()}
            self._ready.set()
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            log.warning("carnet: custom embedding endpoint unavailable: %s", self._error)

    def _encode_http(self, texts: list[str], timeout: float = 30.0) -> list[array]:
        rep = self._http("/embeddings", {"model": self.model, "input": texts},
                         timeout=timeout)
        vectors = [array("f", d["embedding"]) for d in rep.get("data", [])]
        for v in vectors:
            n = sum(map(mul, v, v)) ** 0.5
            if n:
                for i in range(len(v)):
                    v[i] /= n
        return vectors

    # -- sidecar backend (fallback) ----------------------------------
    def _start_sidecar(self) -> None:
        if not self.python:
            self._error = "venv python not found"
            return
        if self.model_dir:
            onnx = self.model_dir / "onnx" / "model_quantized.onnx"
            if not onnx.exists():
                self._error = "local model not downloaded"
                return
        try:
            log_path = self.db.parent / "embed.log"
            try:                    # a debugging trace, not a record: cap it
                if log_path.exists() and log_path.stat().st_size > 4 * 1024 * 1024:
                    # By bytes, not by lines: one long traceback is a single line
                    # and would defeat a line-based cap.
                    with open(log_path, "rb") as f:
                        f.seek(log_path.stat().st_size - 2 * 1024 * 1024)
                        keep = f.read()
                    newline = keep.find(b"\n")
                    if newline != -1:
                        keep = keep[newline + 1:]
                    if not keep.strip():
                        # One long line and nothing else: dropping "the rest of
                        # the line" would erase the whole trace. Keep the tail.
                        keep = b"[truncated]\n" + keep
                    log_path.write_bytes(keep)
            except OSError:
                pass
            journal = open(log_path, "a", encoding="utf-8")
            # Minimal environment: the child reads a model and a database, it has
            # no business inheriting the gateway's credentials or paths.
            env = {
                "PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
                "HOME": os.environ.get("HOME") or str(Path.home()),
                "PYTHONNOUSERSITE": "1",
                "PYTHONUNBUFFERED": "1",
            }
            for keep in ("TMPDIR", "LANG", "LC_ALL", "PYENV_ROOT", "CONDA_PREFIX",
                         "VIRTUAL_ENV"):
                if os.environ.get(keep):
                    env[keep] = os.environ[keep]
            if self.model_dir:
                env["CARNET_MODEL"] = str(self.model_dir)
            self.proc = subprocess.Popen(
                [self.python, str(self.server), str(self.db)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=journal,
                text=True, bufsize=1, cwd=str(self.server.parent), env=env,
            )
            self._start_reader()
            line = self._read_line(self.START_TIMEOUT) or ""
            if not line:
                _kill_sidecar(self.proc)
                self.proc = None
                self._error = "startup timeout"
                log.warning("carnet: embedding server did not answer within %s s",
                            self.START_TIMEOUT)
                return
            # The child may print a banner or a warning before its JSON line:
            # skip anything that is not the answer instead of failing on it.
            rep = {}
            deadline = time.monotonic() + self.START_TIMEOUT
            while line.strip() and not rep:
                try:
                    rep = json.loads(line)
                except ValueError:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    line = self._read_line(left) or ""
                    if not line:
                        break
            if rep.get("ok"):
                self._state = {"dim": rep.get("dim", 0), "n": rep.get("n", 0)}
                _ensure_watch()
                with _RUNTIME_LOCK:
                    _RUNTIME[self._key] = {
                        "proc": self.proc,
                        "touched": time.monotonic(),
                        "state": dict(self._state),
                    }
                self._ready.set()
            else:
                self._error = "invalid startup response"
                # The child is alive but useless, and it holds the model: leaving
                # it running is how a failed start leaks a process per attempt.
                if self.proc is not None:
                    _kill_sidecar(self.proc)
                    self.proc = None
                self._ready.clear()
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            log.warning("carnet: embedding server unavailable: %s", self._error)
            # A half-started child holds the model in memory and answers nothing:
            # keeping it means every later call says "not ready" for ever.
            if self.proc is not None:
                _kill_sidecar(self.proc)
                self.proc = None
            self._ready.clear()

    def _call(self, request: dict, timeout: Optional[float] = None) -> dict:
        p = self.proc
        if p is not None and p.poll() is not None:
            # The child is gone: the readiness flag is a lie from this moment on.
            self._ready.clear()
        if not p or not self._ready.is_set() or p.poll() is not None:
            # A killed or crashed child is restarted here, once: without this the
            # embedding channel would stay dead until the process restarts.
            self._restart()
            p = self.proc
            if not p or not self._ready.is_set() or p.poll() is not None:
                return {"ok": False, "error": "server not ready"}
        wait = self.timeout if timeout is None else timeout
        with self._lock:
            try:
                p.stdin.write(json.dumps(request) + "\n")
                p.stdin.flush()
                line = self._read_line(wait)
                if line is None:
                    # The child still holds the request and its answer would be
                    # read as the reply to the next one, silently pairing every
                    # query with the previous result. Kill it instead: the next
                    # call starts a fresh one.
                    _kill_sidecar(p)
                    self._ready.clear()
                    self._error = "timeout"
                    log.warning("carnet: embedding server timed out, restarted on the next call")
                    return {"ok": False, "error": "timeout"}
                self._touch()
                return json.loads(line) if line.strip() else {"ok": False, "error": "empty response"}
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # -- local index (lmstudio backend) ------------------------------
    def _conn(self):
        import sqlite3
        c = sqlite3.connect(self.db, timeout=5)
        c.row_factory = sqlite3.Row
        return c

    def vector_count(self) -> int:
        try:
            with contextlib.closing(self._conn()) as c:
                return c.execute("SELECT count(*) n FROM vec").fetchone()["n"]
        except Exception:
            return 0

    def _load_cache(self) -> None:
        if self._cache_loaded:
            return
        self._cache = {}
        try:
            with contextlib.closing(self._conn()) as c:
                for r in c.execute("SELECT fact_id, blob FROM vec"):
                    self._cache[r["fact_id"]] = array("f", r["blob"])
        except Exception as exc:
            log.warning("carnet: unreadable vector cache: %s", exc)
        self._cache_loaded = True

    def _store(self, items: list[tuple[str, array]]) -> int:
        with contextlib.closing(self._conn()) as c:
            for fid, v in items:
                c.execute(
                    "INSERT INTO vec(fact_id, dim, blob) VALUES (?,?,?) "
                    "ON CONFLICT(fact_id) DO UPDATE SET dim=excluded.dim, blob=excluded.blob",
                    (fid, len(v), v.tobytes()),
                )
            c.commit()
        for fid, v in items:
            self._cache[fid] = v
        return len(items)

    # -- public API (identical for both backends) --------------------
    def search(self, query: str, k: int = 20) -> list[tuple[str, float]]:
        if not query.strip():
            return []
        if not self.ready and self.backend == "sidecar":
            # A dead child must be revived by the call that needs it, but a
            # start that just failed must not cost three seconds on the hot
            # path: inside the throttle window, answer immediately.
            if self._error and time.monotonic() - getattr(self, "_last_restart", 0.0) < 5.0:
                return []
            self._restart()
        if not self.ready:
            return []
        if self.backend == "sidecar":
            rep = self._call({"op": "search", "query": query, "k": k})
            return [(str(i), float(s)) for i, s in rep.get("results") or []] if rep.get("ok") else []
        try:
            self._load_cache()
            if not self._cache:
                return []
            # Short timeout: if LM Studio unloaded the model, do not block the turn.
            # Return [] and the provider continues lexically, as if the vector were missing.
            q = self._encode_http([query], timeout=self.timeout)[0]
            scores = [(_cosine(v, q), fid) for fid, v in self._cache.items()]
            scores.sort(reverse=True)
            return [(fid, s) for s, fid in scores[:k]]
        except Exception as exc:
            log.warning("carnet: vector search: %s", exc)
            return []

    def index(self, items: list[tuple[str, str]], timeout: float = 30.0) -> int:
        items = [(str(i), str(t)) for i, t in items if i and t]
        if not items:
            return 0
        if self.backend == "sidecar":
            rep = self._call({"op": "index", "items": [[i, t] for i, t in items]}, timeout=timeout)
            return int(rep.get("indexed", 0)) if rep.get("ok") else 0
        if not self.ready:
            return 0
        try:
            self._load_cache()
            vectors = self._encode_http([t for _, t in items])
            return self._store([(fid, v) for (fid, _), v in zip(items, vectors)])
        except Exception as exc:
            log.warning("carnet: vector indexing: %s", exc)
            return 0

    def delete(self, ids: list[str]) -> int:
        if self.backend == "sidecar":
            rep = self._call({"op": "delete", "ids": ids}, timeout=10.0)
            return int(rep.get("deleted", 0)) if rep.get("ok") else 0
        try:
            with contextlib.closing(self._conn()) as c:
                for fid in ids:
                    c.execute("DELETE FROM vec WHERE fact_id=?", (fid,))
                c.commit()
        except Exception:
            return 0
        for fid in ids:
            self._cache.pop(fid, None)
        return len(ids)

    def rerank(self, query: str, items: list[tuple[str, str]], timeout: float = 3.0) -> list[tuple[str, float]]:
        """Reorder (id, text) with a cross-encoder. Empty list if unavailable."""
        if self.backend != "sidecar" or not items:
            return []
        rep = self._call({"op": "rerank", "query": query,
                          "items": [[i, t[:600]] for i, t in items]}, timeout=timeout)
        if not rep.get("ok"):
            return []
        return [(str(i), float(s)) for i, s in rep.get("results") or []]

    def state(self) -> dict:
        if self.backend == "lmstudio" and self.ready:
            self._state["n"] = self.vector_count()
        return {"ready": self.ready, "dim": self._state.get("dim", 0),
                "n": self._state.get("n", 0), "error": self._error,
                "backend": self.backend}
