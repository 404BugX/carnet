"""Carnet embedding server — runs in the Hermes venv (numpy + onnxruntime).

Protocol: one JSON line per request, one JSON line per response, over stdin/stdout.

    {"op":"health"}
    {"op":"index","items":[["f-1","some text"], ...]}
    {"op":"search","query":"...","k":20}
    {"op":"delete","ids":["f-1"]}

Vectors live in carnet.db (table `vec`), and are held in memory in a numpy
matrix so that search is a simple matrix product.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np

MODEL = Path(os.environ.get(
    "CARNET_MODEL",
    Path.home() / ".compartment/models/embeddinggemma-300m-int8",
))


def model_file(model: Path) -> Path:
    """The quantized ONNX file, at the root or in the repository's onnx/ folder."""
    for rel in ("model_quantized.onnx", "onnx/model_quantized.onnx",
                "onnx/model_q4.onnx", "model.onnx"):
        candidate = model / rel
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"no ONNX model found under {model}")


def log(msg: str) -> None:
    print(f"[embed] {msg}", file=sys.stderr, flush=True)


class Embedder:
    def __init__(self, model: Path):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.tok = Tokenizer.from_file(str(model / "tokenizer.json"))
        self.tok.enable_truncation(max_length=256)
        self.tok.enable_padding(pad_id=0, pad_token="<pad>")

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        # Without this, onnxruntime threads spin idle waiting for work: an
        # idle server burned 160% CPU and pushed it to 107 °C.
        opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
        opts.add_session_config_entry("session.inter_op.allow_spinning", "0")
        opts.enable_mem_pattern = False
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        onnx_path = model_file(model)
        self.sess = ort.InferenceSession(str(onnx_path), sess_options=opts,
                                         providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.sess.get_inputs()}
        self.dim = int(self.sess.get_outputs()[0].shape[-1])
        log(f"model loaded: {model.name} · dim={self.dim} · inputs={sorted(self.inputs)}")

    def encode(self, texts: list[str], batch: int = 8) -> np.ndarray:
        """Mean pooling + L2 normalization (embeddinggemma is trained with mean pooling).
        Encoded in small batches: the peak memory of one large batch reached several GB."""
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        if len(texts) > batch:
            return np.vstack([self.encode(texts[i:i + batch], batch) for i in range(0, len(texts), batch)])
        enc = self.tok.encode_batch(texts)
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        output = self.sess.run(None, feed)[0]
        m = mask[..., None].astype(np.float32)
        total = (output * m).sum(axis=1)
        count = np.clip(m.sum(axis=1), 1e-6, None)
        vecs = total / count
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        return (vecs / np.clip(norms, 1e-9, None)).astype(np.float32)


class Reranker:
    """Multilingual cross-encoder: it reads (question, fact) together, which the
    embedding never does. Expensive, so reserved for the top of the basket.

    Loaded through transformers directly: sentence-transformers' CrossEncoder
    wrapper returned degenerate logits (0.006 for a relevant pair) and degraded
    the ranking. With the raw model, the relevant/off-topic gap is clear.
    """
    NAME = "BAAI/bge-reranker-v2-m3"

    def __init__(self):
        self._tok = None
        self._m = None
        self._torch = None

    def _load(self):
        if self._m is None:
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
            log("loading the bge-reranker-v2-m3 reranker…")
            self._torch = torch
            self._tok = AutoTokenizer.from_pretrained(self.NAME)
            self._m = AutoModelForSequenceClassification.from_pretrained(self.NAME).eval()
            log("reranker ready")
        return self._m

    def rank(self, query: str, items: list[list[str]]) -> list[list]:
        if not items:
            return []
        m = self._load()
        torch = self._torch
        enc = self._tok([query] * len(items), [t[:600] for _, t in items],
                        truncation=True, max_length=512, padding=True, return_tensors="pt")
        with torch.no_grad():
            scores = m(**enc).logits.view(-1).float().tolist()
        out = [[str(i), round(float(s), 4)] for (i, _), s in zip(items, scores)]
        out.sort(key=lambda x: -x[1])
        return out


class Index:
    def __init__(self, db: Path, embedder: Embedder):
        self.db = db
        self.emb = embedder
        self.ids: list[str] = []
        self.pos: dict[str, int] = {}
        self.matrix = np.zeros((0, embedder.dim), dtype=np.float32)
        self._conn: sqlite3.Connection | None = None
        self.load()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db, timeout=8.0, isolation_level=None,
                                         check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=6000")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS vec (fact_id TEXT PRIMARY KEY, dim INTEGER NOT NULL, blob BLOB NOT NULL)")
        return self._conn

    def load(self) -> None:
        rows = list(self.conn.execute("SELECT fact_id, dim, blob FROM vec"))
        if not rows:
            self.matrix = np.zeros((0, self.emb.dim), dtype=np.float32)
            return
        ids, vecs = [], []
        for r in rows:
            if r["dim"] != self.emb.dim:
                continue
            ids.append(r["fact_id"])
            vecs.append(np.frombuffer(r["blob"], dtype=np.float32))
        self.ids = ids
        self.pos = {i: k for k, i in enumerate(ids)}
        self.matrix = np.vstack(vecs).astype(np.float32) if vecs else np.zeros((0, self.emb.dim), np.float32)
        log(f"index loaded: {len(self.ids)} vectors")

    def index(self, items: list[list[str]]) -> int:
        items = [(str(i), str(t)) for i, t in items if i and t]
        if not items:
            return 0
        vecs = self.emb.encode([t for _, t in items])
        n = 0
        for (fid, _), v in zip(items, vecs):
            self.conn.execute(
                "INSERT INTO vec(fact_id, dim, blob) VALUES (?,?,?) "
                "ON CONFLICT(fact_id) DO UPDATE SET dim=excluded.dim, blob=excluded.blob",
                (fid, self.emb.dim, v.tobytes()),
            )
            k = self.pos.get(fid)
            if k is None:
                self.pos[fid] = len(self.ids)
                self.ids.append(fid)
                self.matrix = np.vstack([self.matrix, v[None, :]])
            else:
                self.matrix[k] = v
            n += 1
        return n

    def delete(self, ids: list[str]) -> int:
        n = 0
        for fid in ids:
            self.conn.execute("DELETE FROM vec WHERE fact_id=?", (fid,))
            k = self.pos.pop(fid, None)
            if k is not None:
                self.matrix = np.delete(self.matrix, k, axis=0)
                self.ids.pop(k)
                self.pos = {i: j for j, i in enumerate(self.ids)}
            n += 1
        return n

    def search(self, query: str, k: int = 20) -> list[list]:
        if not self.ids or not query.strip():
            return []
        q = self.emb.encode([query])[0]
        scores = self.matrix @ q
        k = min(k, len(self.ids))
        idx = np.argpartition(-scores, k - 1)[:k]
        idx = idx[np.argsort(-scores[idx])]
        return [[self.ids[i], round(float(scores[i]), 4)] for i in idx]


def _watchdog(parent: int) -> None:
    """If the Hermes process dies without closing stdin, the server would stay
    orphaned with the model in RAM. We watch the parent and exit."""
    import time
    while True:
        time.sleep(5)
        if os.getppid() != parent:
            log("parent gone, stopping")
            os._exit(0)


def main() -> None:
    parent = os.getppid()
    import threading
    threading.Thread(target=_watchdog, args=(parent,), daemon=True).start()
    db = Path(sys.argv[1])
    emb = Embedder(MODEL)
    idx = Index(db, emb)
    rerank = Reranker()
    emb.encode(["warmup"])  # first inference is the slowest, we pay for it at startup
    print(json.dumps({"ok": True, "ready": True, "dim": emb.dim, "n": len(idx.ids)}), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            op = req.get("op")
            if op == "health":
                rep = {"ok": True, "dim": emb.dim, "n": len(idx.ids), "model": MODEL.name}
            elif op == "index":
                rep = {"ok": True, "indexed": idx.index(req.get("items") or [])}
            elif op == "search":
                rep = {"ok": True, "results": idx.search(req.get("query") or "", int(req.get("k", 20)))}
            elif op == "delete":
                rep = {"ok": True, "deleted": idx.delete(req.get("ids") or [])}
            elif op == "rerank":
                rep = {"ok": True, "results": rerank.rank(req.get("query") or "", req.get("items") or [])}
            elif op == "reload":
                idx.load()
                rep = {"ok": True, "n": len(idx.ids)}
            elif op == "quit":
                break
            else:
                rep = {"ok": False, "error": f"unknown op: {op}"}
        except Exception as exc:
            rep = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(rep), flush=True)


if __name__ == "__main__":
    main()
