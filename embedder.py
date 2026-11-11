"""Text encoder + vector search, client side.

Four modes, same interface:

- ``disabled`` — no vectors at all; recall stays lexical.
- ``lmstudio`` (default) — POST /v1/embeddings on the local LM Studio server.
  One local process for everything (embeddings + small models), no more sidecar,
  no more onnxruntime, no more venv to purge. Vectors live in ``vec``.
- ``custom`` — any OpenAI-compatible /v1/embeddings endpoint (URL, model, key).
- ``sidecar`` — ``dev/embed_server.py`` in the Hermes venv, running the local
  ONNX model downloaded once in the shared profile cache.

Similarity is computed here, in pure Python: the plugin runtime has no numpy.
``sum(map(mul, a, b))`` over ``array('f')`` stays C-level, so ~2 ms for 150 facts
and ~300 ms for 10 000 — within a turn's budget.

Golden rule: no waiting on the hot path. If the backend is not ready,
``search()`` returns an empty list and the provider continues lexically.
"""
from __future__ import annotations

import json
import logging
import os
import select
import shutil
import subprocess
import threading
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


class Embedder:
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
        self._error = ""
        self._state = {"dim": 0, "n": 0}
        self._cache: dict[str, array] = {}
        self._cache_loaded = False

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
        targets = {
            "lmstudio": self._start_lmstudio,
            "custom": self._start_custom,
            "sidecar": self._start_sidecar,
        }
        target = targets.get(self.backend, self._start_lmstudio)
        threading.Thread(target=target, name="carnet-embed-start", daemon=True).start()

    def stop(self) -> None:
        if self.backend != "sidecar":
            return
        p = self.proc
        self.proc = None
        self._ready.clear()
        if not p:
            return
        try:
            if p.stdin:
                p.stdin.write('{"op":"quit"}\n')
                p.stdin.flush()
            p.wait(timeout=2)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

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
            journal = open(self.db.parent / "embed.log", "a", encoding="utf-8")
            env = {k: v for k, v in os.environ.items()
                   if k not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")}
            env["PYTHONNOUSERSITE"] = "1"
            if self.model_dir:
                env["CARNET_MODEL"] = str(self.model_dir)
            self.proc = subprocess.Popen(
                [self.python, str(self.server), str(self.db)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=journal,
                text=True, bufsize=1, cwd=str(self.server.parent), env=env,
            )
            line = self.proc.stdout.readline() if self.proc.stdout else ""
            rep = json.loads(line) if line.strip() else {}
            if rep.get("ok"):
                self._state = {"dim": rep.get("dim", 0), "n": rep.get("n", 0)}
                self._ready.set()
            else:
                self._error = "invalid startup response"
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            log.warning("carnet: embedding server unavailable: %s", self._error)

    def _call(self, request: dict, timeout: Optional[float] = None) -> dict:
        p = self.proc
        if not p or not self._ready.is_set() or p.poll() is not None:
            return {"ok": False, "error": "server not ready"}
        wait = self.timeout if timeout is None else timeout
        with self._lock:
            try:
                p.stdin.write(json.dumps(request) + "\n")
                p.stdin.flush()
                r, _, _ = select.select([p.stdout], [], [], wait)
                if not r:
                    return {"ok": False, "error": "timeout"}
                line = p.stdout.readline()
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
            with self._conn() as c:
                return c.execute("SELECT count(*) n FROM vec").fetchone()["n"]
        except Exception:
            return 0

    def _load_cache(self) -> None:
        if self._cache_loaded:
            return
        self._cache = {}
        try:
            with self._conn() as c:
                for r in c.execute("SELECT fact_id, blob FROM vec"):
                    self._cache[r["fact_id"]] = array("f", r["blob"])
        except Exception as exc:
            log.warning("carnet: unreadable vector cache: %s", exc)
        self._cache_loaded = True

    def _store(self, items: list[tuple[str, array]]) -> int:
        with self._conn() as c:
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
        if not self.ready or not query.strip():
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
            with self._conn() as c:
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
