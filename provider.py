"""Carnet — memory provider for Hermes.

Four layers:
  · USER.md  — managed by the core, never touched here
  · core     — importance 0, injected into the system prompt, 20 seats max
  · facts    — importance 1, injected per turn if new and relevant
  · sheets   — importance 2, never injected whole: we show the beginning + an id

The recall block is idempotent per session: it never returns the same fact twice,
because the block is pasted into the user message and reread on every following turn.
"""
from __future__ import annotations

import json
import os
import time
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from agent.memory_provider import MemoryProvider, RecallStatus, spawn_context_thread

from .embedder import Embedder
from .store import DEFAULTS, Carnet, normalize, normalize_settings

log = logging.getLogger("carnet")

GLYPH = "📓"

# Tool schemas — four, no more: every schema is sent back on every turn.
TOOLS = [
    {
        "name": "remember",
        "description": (
            "Stores a durable fact in long-term memory. "
            "Use it when the user says \"remember\", \"note\", or when you learn "
            "something stable that will be useful again (preference, constraint, decision, path, version). "
            "One fact = one self-contained sentence, a single topic, 500 characters maximum — "
            "beyond that the tool REFUSES and nothing is written: shorten it and call again. "
            "(a `importance=2` sheet has no limit). "
            "With `id`, correct an existing fact: a new version replaces the old one, nothing is lost. "
            "With `importance=0`, request a core seat (20 slots, injected on every session). "
            "With `importance=2`, create a sheet (long document, read only on demand)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The fact, self-contained and dated if useful."},
                "id": {"type": "string", "description": "Identifier of a fact to correct (optional)."},
                "importance": {"type": "integer", "enum": [0, 1, 2], "description": "0=core, 1=fact (default), 2=sheet."},
                "topic": {"type": "string", "description": "Short, reusable topic (e.g. 'project orion')."},
                "confidence": {"type": "string", "enum": ["certain", "probable"], "description": "certain only if verified or stated explicitly."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "recall",
        "description": (
            "Searches memory when automatic recall returned nothing, or when you need "
            "a precise fact before asserting something. Returns identifiers and excerpts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords or question."},
                "limit": {"type": "integer", "description": "Number of results (default 6, max 20)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_fiche",
        "description": "Opens a sheet or fact in full, with its provenance. Use it when an identifier appears in the memory block and the detail matters.",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "Identifier (f-xxxx)."}},
            "required": ["id"],
        },
    },
    {
        "name": "forget",
        "description": "Removes a fact from recall without deleting it (it stays consultable and reversible). Use it when a fact is wrong or no longer valid.",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Identifier of the fact."},
                "reason": {"type": "string", "description": "Why (journaled)."},
            },
            "required": ["id"],
        },
    },
]

NOTE_TOOLS = [
    {
        "name": "note",
        "description": (
            "Leaves a temporary note for a future session, for example \"remind me to "
            "discuss X next time\". A note is not a durable fact: it expires after the "
            "configured delay, never enters the core, and is shown once at the start of "
            "the next session. Use `note_close` when it has been handled."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The note, self-contained and short."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "note_close",
        "description": (
            "Closes a temporary note once it has been used, so it is no longer injected. "
            "The note is kept in the database until cleanup."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Identifier of the note (n-xxxx)."},
            },
            "required": ["id"],
        },
    },
]


# ---------------------------------------------------------------- extraction

_RE_EXPLICIT = re.compile(
    # `(?<!\w)` : without it, "note" fires inside "notebook", "annotate",
    # "denote" — hence ghost facts cut in the middle of a word.
    r"(?<!\w)(?:remember|keep in mind|note|memorize|remind me)"
    r"\s+(?:that\s+)?(.{10,2000})",
    re.IGNORECASE,
)
_RE_PREFERENCE = re.compile(
    r"(?<!\w)\b(i prefer|i don't like|i dislike|i hate|i use|i avoid|i always want|"
    r"from now on|from here on)\b(.{5,2000})",
    re.IGNORECASE,
)

# A fragment that starts with a connector or a lowercase letter is not a
# fact: it is the tail of a previous sentence ("but worn on a watch").
_CONNECTORS = ("but", "and", "because", "so", "yet", "then", "also", "since", "afterwards",
               "anyway", "knowing", "thus", "however", "still")


def _self_contained(fragment: str, minimum: int) -> bool:
    """True if the fragment can be read on its own, outside its original message.

    We do not require a capital letter: "remember that my server runs on Debian"
    leaves a legitimate lowercase fragment. What we reject is the tail of a
    previous sentence, recognizable by its opening connector.
    """
    if len(fragment) < minimum or len(fragment.split()) < 3:
        return False
    first = fragment.split()[0].lower().strip(",;:.")
    if first in _CONNECTORS:
        return False
    return True


def extract(user_text: str) -> list[dict]:
    """Careful extraction: only what is explicitly memorable.

    Deliberately conservative — better to miss a fact than fill the database with
    conversation sentences. The model writes the rest through the `remember` tool.
    An overlong fragment is DISCARDED, not trimmed: rewriting it is the model's job.
    """
    output: list[dict] = []

    def _add(fragment: str, minimum: int, confidence: str) -> None:
        if _self_contained(fragment, minimum) and len(fragment) <= 500:
            output.append({"text": fragment, "confidence": confidence, "source": "user"})

    for m in _RE_EXPLICIT.finditer(user_text or ""):
        _add(_first_sentence(m.group(1)), 10, "certain")
    if not output:
        for m in _RE_PREFERENCE.finditer(user_text or ""):
            _add(_first_sentence(m.group(1) + m.group(2)), 12, "probable")
    # internal deduplication
    seen, unique = set(), []
    for f in output:
        k = normalize(f["text"])
        if k not in seen:
            seen.add(k)
            unique.append(f)
    return unique[:3]


_META_INSTRUCTION = re.compile(
    r"\b(use|call)\s+(the\s+)?tool\b.*$", re.IGNORECASE)


def _wrote_explicitly(messages) -> bool:
    """True if the turn contains a successful `remember` call — the model already wrote."""
    if not messages:
        return False
    for m in messages[-6:]:
        if not isinstance(m, dict):
            continue
        for call in (m.get("tool_calls") or []):
            fn = (call.get("function") or {}).get("name") if isinstance(call, dict) else None
            if fn == "remember":
                return True
    return False


def _first_sentence(fragment: str) -> str:
    """Cut at the end of the first sentence and remove tool instructions."""
    fragment = (fragment or "").strip()
    fragment = _META_INSTRUCTION.sub("", fragment).strip()
    m = re.search(r"[.!?]\s", fragment)
    if m:
        fragment = fragment[: m.start()]
    return fragment.strip(" .,;:!?\n\t\"'")


# ---------------------------------------------------------------- provider

def _modern_args(args: dict) -> dict:
    """Accept the pre-1.0 parameter names as aliases.

    A session binds its tool schemas once, at startup, so an agent running since
    before an upgrade keeps sending ``texte``/``requete`` while the current schema
    says ``text``/``query``. Without this the tool silently answers "nothing found"
    and the memory looks empty.
    """
    if not isinstance(args, dict):
        return args
    alias = {"texte": "text", "sujet": "topic", "confiance": "confidence",
             "requete": "query", "limite": "limit", "fiche_id": "id"}
    out = dict(args)
    for old, new in alias.items():
        if old in out and new not in out:
            out[new] = out.pop(old)
    return out


class CarnetProvider(MemoryProvider):
    def __init__(self) -> None:
        self._root: Optional[Path] = None
        self._home: Optional[Path] = None
        self._store: Optional[Carnet] = None
        self._embedder: Optional[Embedder] = None
        self._session_id = ""
        self._config: dict = dict(DEFAULTS)
        self._config_stamp: tuple = ()
        self._last_prefetch: list[str] = []
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._first_turn = True
        self._notes_seen_session = ""

    # -- identity ----------------------------------------------------
    @property
    def name(self) -> str:
        return "carnet"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        home = kwargs.get("hermes_home") or ""
        if not home:
            # NEVER fall back to the current folder: a memory created there would be
            # invisible from Hermes, and the model would answer "I don't know" without
            # anything flagging the error. Resolve the real home, or say so.
            try:
                from hermes_constants import get_hermes_home
                home = str(get_hermes_home())
            except Exception:
                home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
            log.warning("carnet: hermes_home missing at initialization, falling back to %s", home)
        self._home = Path(home)
        self._root = self._home / "carnet"
        self._store = Carnet(self._root)
        self._store.init_schema()
        self._refresh_config()
        self._session_id = session_id or ""
        self._first_turn = True
        self._notes_seen_session = ""
        # Vectors: separate server, started in the background so the turn is never blocked.
        try:
            server = Path(__file__).with_name("dev") / "embed_server.py"
            backend = str(self._config.get("embed_backend") or "lmstudio")
            model_dir = ""
            if backend == "sidecar":
                from .localmodel import cache_dir
                model_dir = str(cache_dir(home))
            self._embedder = Embedder(self._store.path, server,
                                      python=str(self._config.get("python_venv") or ""),
                                      timeout_ms=int(self._config.get("embed_timeout_ms") or 300),
                                      backend=backend,
                                      url=str(self._config.get("embed_url") or ""),
                                      model=str(self._config.get("embed_model") or ""),
                                      api_key=str(self._config.get("embed_api_key") or ""),
                                      model_dir=model_dir)
            self._embedder.start_in_background()
        except Exception as exc:
            log.warning("carnet: embedder not started: %s", exc)
            self._embedder = None
        self._start_worker()
        # Catch-up: the queue from a previous session is processed at startup.
        self._wake.set()
        self._start_indexing()

    def _config_sources(self) -> list[Path]:
        if not self._home:
            return []
        return [self._root / "config.json", self._home / "config" / "carnet.json"]

    def _load_config(self) -> dict:
        cfg = dict(DEFAULTS)
        for p in self._config_sources():
            if not p.exists():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
            except (OSError, ValueError):
                log.warning("carnet: unreadable %s, skipping it", p)
        return normalize_settings(cfg)

    def _refresh_config(self) -> None:
        """Re-read the settings when either file changes: next turn, no restart."""
        if not self._home:
            return
        stamp = []
        for p in self._config_sources():
            try:
                stamp.append(p.stat().st_mtime_ns)
            except OSError:
                stamp.append(None)
        key = tuple(stamp)
        if key == self._config_stamp:
            return
        self._config = self._load_config()
        self._config_stamp = key
        if self._store:
            self._store.meta_set("seat_limit", str(int(self._config["seat_limit"])))

    # -- layer 0: the core -------------------------------------------
    def system_prompt_block(self) -> str:
        if not self._store:
            return ""
        self._refresh_config()
        limit = int(self._config["seat_limit"])
        seats = self._store.core(limit)
        date = (self._store.meta_get("core_updated_at", "") or self._store.meta_get("created_at", ""))[:10]
        lines = [f"[MEMORY — permanent core, {len(seats)}/{limit} seats, as of {date or 'n/a'}]"]
        for s in seats:
            lines.append(f"· {s['text'][: self._config['seat_char_limit']]}")
        if not seats:
            lines.append("· (empty core)")
        lines.append("")
        lines.append(
            "This memory is nearly unlimited and lives beside the context, not inside it. "
            "Facts cost almost nothing: use them without hesitation. `remember` anything durable "
            "as soon as you learn it, `recall` before answering from memory or saying you do not "
            "know, `read_fiche(id)` to open a long note. A fact recorded once is far cheaper than "
            "the same question asked again. Relevant facts you have not seen yet are added "
            "automatically each turn under a `[memory · N new]` header; the same fact is never "
            "repeated in a session."
        )
        lines.append(
            f"Limits: {limit} core seats × {int(self._config['seat_char_limit'])} chars · "
            f"fact max {int(self._config['fact_char_limit'])} chars · "
            f"{int(self._config['max_facts_per_turn'])} facts + "
            f"{int(self._config['max_sheets_per_turn'])} notes per turn · "
            f"{int(self._config['budget_chars_turn'])} chars per turn "
            f"({int(self._config['budget_chars_first_turn'])} on the first turn)."
        )
        return "\n".join(lines)

    # -- reading -----------------------------------------------------
    def prefetch(self, query: str, *, session_id: str = "") -> str:
        sid = session_id or self._session_id
        if not self._store:
            return ""
        self._refresh_config()
        try:
            notes_block = self._notes_block(sid)
        except Exception as exc:
            log.warning("carnet: notes block failed: %s", exc)
            notes_block = ""
        try:
            budget = int(self._config["budget_chars_first_turn"] if self._first_turn
                         else self._config["budget_chars_turn"])
            results = self._candidates(query)
            already = self._store.already_injected(sid)
            threshold = float(self._config["abstention_threshold"])

            # Beyond N facts already injected in the session, only the very relevant
            # are kept: a long session must not end with 70 facts in the context.
            session_limit = int(self._config.get("session_fact_limit", 40))
            tight = len(already) >= session_limit
            threshold = threshold * (1.6 if tight else 1.0)

            kept, total, n_facts, n_sheets = [], 0, 0, 0
            for r in results:
                row = r["row"]
                if r["score"] < threshold:
                    break
                if row["id"] in already:
                    continue
                # The core is already in the system prompt: reinjecting it would be a paid duplicate.
                if int(row["importance"]) == 0:
                    continue
                if row["importance"] == 2:
                    if n_sheets >= int(self._config["max_sheets_per_turn"]):
                        continue
                    line = self._sheet_line(row)
                    n_sheets += 1
                else:
                    if n_facts >= int(self._config["max_facts_per_turn"]):
                        continue
                    line = self._fact_line(row)
                    n_facts += 1
                if total + len(line) > budget and kept:
                    break
                kept.append((row["id"], line))
                total += len(line)

            self._first_turn = False
            if not kept:
                self._last_prefetch = []
                parts = [p for p in (notes_block, self._health_line()) if p]
                return "\n\n".join(parts)

            self._store.mark_injected(sid, [i for i, _ in kept])
            self._last_prefetch = [i for i, _ in kept]
            header = f"[memory · {len(kept)} new · {len(already)} already in context]"
            block = [header] + [l for _, l in kept]
            health = self._health_line()
            if health:
                block.append(health)
            if notes_block:
                block.append("")
                block.append(notes_block)
            final_text = "\n".join(block)
            self._log_injection(sid, query, final_text, [i for i, _ in kept])
            return final_text
        except Exception as exc:  # never blocking, never silent
            log.warning("carnet: prefetch failed: %s", exc)
            return ""

    def _candidates(self, query: str, *, semantic: bool = True) -> list[dict]:
        """Lexical + vector fusion. Each channel is brought onto a 0..1 scale,
        then the best of the two is kept with a bonus when they agree."""
        lex = {r["row"]["id"]: (r["row"], r["score"]) for r in self._store.search(query, limit=60)}

        vec: dict = {}
        if semantic and self._embedder and self._embedder.ready:
            for fid, cos in self._embedder.search(query, k=30):
                # embeddinggemma: unrelated pairs hover around 0.45-0.55
                normalized = max(0.0, min(1.0, (cos - 0.45) / 0.55))
                if normalized <= 0:
                    continue
                if fid in lex:
                    vec[fid] = (lex[fid][0], normalized)
                else:
                    row = self._store.fact(fid)
                    if row is None or row["status"] != "active":
                        continue
                    vec[fid] = (row, normalized)

        fusion: dict = {}
        for fid, (row, sl) in lex.items():
            fusion[fid] = [row, sl, 0.0]
        for fid, (row, sv) in vec.items():
            if fid in fusion:
                fusion[fid][2] = sv
            else:
                fusion[fid] = [row, 0.0, sv]

        output = []
        for row, sl, sv in fusion.values():
            score = max(sl, sv) + 0.15 * min(sl, sv)
            output.append({"row": row, "score": round(score, 4)})
        output.sort(key=lambda d: d["score"], reverse=True)

        # Second stage: a cross-encoder reexamines the top 20. Recall stays
        # fast and broad, precision is played on the top of the basket only.
        if (self._config.get("rerank") and self._embedder and self._embedder.ready and len(output) > 1):
            top = output[:20]
            reordered = self._embedder.rerank(query, [(d["row"]["id"], d["row"]["text"]) for d in top])
            if reordered:
                by_id = {d["row"]["id"]: d for d in top}
                ordered = []
                for fid, sc in reordered:
                    d = by_id.get(fid)
                    if d is not None:
                        d["rerank_score"] = round(sc, 4)
                        ordered.append(d)
                if len(ordered) == len(top):
                    output = ordered + output[20:]
        return output

    def _log_injection(self, sid: str, query: str, block: str, ids: list[str]) -> None:
        """Clean trace of what goes to the model: state.db's `api_content` is not
        always written (the sidecar can be lost depending on the persistence path)."""
        try:
            p = self._root / "injections.jsonl"
            line = json.dumps({
                "ts": _iso_now(), "session": sid, "query": (query or "")[:300],
                "ids": ids, "chars": len(block), "block": block,
            }, ensure_ascii=False)
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def _fact_line(self, row) -> str:
        if row["confidence"] == "certain":
            mark = "✓" if not row["evidence_missing"] else "✓?"
        else:
            mark = "~"
        date = (row["created_at"] or "")[:10]
        text = row["text"].replace("\n", " ")
        if len(text) > 220:
            text = text[:217] + "…"
        return f"[{row['importance']}] {mark} {date} · {text}"

    def _sheet_line(self, row) -> str:
        excerpt = row["text"][: int(self._config["sheet_excerpt_chars"])].replace("\n", " ")
        rest = max(0, len(row["text"]) - len(excerpt))
        end = f" … (+{rest} chars → read_fiche({row['id']}))" if rest else ""
        return f"[2] · {excerpt}{end}"

    def _notes_block(self, sid: str) -> str:
        """First turn only: pending notes, separate from the core and recall blocks."""
        if self._notes_seen_session == sid:
            return ""
        self._notes_seen_session = sid
        if not self._store or not self._config.get("notes_enabled"):
            return ""
        limit = int(self._config.get("max_notes", 0) or 0)
        if limit <= 0:
            return ""
        now = datetime.now(timezone.utc)
        char_limit = int(self._config.get("note_char_limit", 300) or 300)
        lines = []
        for row in self._store.active_notes(limit=limit, now=now.replace(microsecond=0).isoformat()):
            try:
                created = datetime.fromisoformat(row["created_at"])
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age = max(0, int((now - created).total_seconds() // 86400))
            except (TypeError, ValueError):
                age = 0
            text = (row["text"] or "").replace("\n", " ")[:char_limit]
            if text:
                lines.append(f"· [{age}d] {text}")
        if not lines:
            return ""
        return "\n".join([f"[pending notes · {len(lines)}]", *lines])

    def _health_line(self) -> str:
        try:
            c = self._store.count()
        except Exception:
            return ""
        alerts = []
        if c["queue"] > 20:
            alerts.append(f"{c['queue']} pending")
        if c["contested"]:
            alerts.append(f"{c['contested']} contested")
        pressure = self._store.meta_get("seat_pressure", "")
        if pressure:
            alerts.append("core full, 1 candidate waiting")
        return f"[health · {' · '.join(alerts)}]" if alerts else ""

    def _health_line_only(self) -> str:
        return self._health_line()

    def recall_status(self) -> Optional[RecallStatus]:
        n = len(self._last_prefetch)
        if not n:
            return None
        return RecallStatus(provider_label="carnet", count=n, glyph=GLYPH)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        self._wake.set()

    # -- writing -----------------------------------------------------
    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None,
                  turn_author=None) -> None:
        if not self._store:
            return
        try:
            # If the model already wrote explicitly during this turn, its version
            # prevails: heuristic extraction must not produce an approximate duplicate.
            if _wrote_explicitly(messages):
                return
            self._store.enqueue_turn(session_id or self._session_id,
                                     user_content or "", assistant_content or "")
            self._wake.set()
        except Exception as exc:
            log.warning("carnet: sync_turn: %s", exc)

    def on_pre_compress(self, messages) -> str:
        if not self._store:
            return ""
        try:
            text = " ".join(
                m.get("content", "") for m in messages[-8:]
                if isinstance(m, dict) and m.get("role") in ("user", "assistant")
                and isinstance(m.get("content"), str)
            )
            self._store.enqueue_turn(self._session_id, text, "", turn="pre_compress")
            self._wake.set()
        except Exception:
            pass
        return ""

    # -- worker ------------------------------------------------------
    def _start_worker(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()

        def _loop():
            while not self._stop.is_set():
                self._wake.wait(timeout=5.0)
                self._wake.clear()
                if self._stop.is_set():
                    break
                try:
                    self._drain()
                except Exception as exc:
                    log.warning("carnet: worker: %s", exc)

        self._worker = spawn_context_thread(_loop, name="carnet-worker")
        self._worker.start()

    def _drain(self) -> None:
        if not self._store:
            return
        for row in self._store.queue_head(limit=10):
            try:
                facts = extract(row["user_text"] or "")
                for f in facts:
                    self._store.add_fact(
                        f["text"], evidence=(row["user_text"] or "")[:400],
                        confidence=f["confidence"], source=f["source"],
                        session_id=row["session_id"], actor="worker",
                    )
                self._store.mark_done(row["id"])
            except Exception as exc:
                self._store.mark_error(row["id"], str(exc))
        self._index()
        self._store.meta_set("last_watch", _iso_now())

    def _index(self) -> None:
        """Send facts that have no vector yet to the embedding server."""
        if not self._store or not self._embedder or not self._embedder.ready:
            return
        try:
            todo = self._store.facts_to_index(limit=64)
            if todo:
                n = self._embedder.index(todo)
                if n:
                    log.info("carnet: %s fact(s) vectorized", n)
        except Exception as exc:
            log.warning("carnet: indexing: %s", exc)

    def _start_indexing(self) -> None:
        """Loading the model takes a few seconds. If there is a backlog to catch up,
        wait for its startup (bounded): the next session then has vectors from the
        first turn. Otherwise let it happen in the background."""
        try:
            backlog = bool(self._store and self._store.facts_to_index(1))
        except Exception:
            backlog = False

        if backlog:
            deadline = time.time() + float(self._config.get("index_wait_s", 8))
            while time.time() < deadline and not self._stop.is_set():
                if self._embedder and self._embedder.ready:
                    self._index()
                    return
                time.sleep(0.3)
            log.warning("carnet: embedding server not ready, indexing postponed")
            return

        def _wait():
            deadline = time.time() + 30
            while time.time() < deadline and not self._stop.is_set():
                if self._embedder and self._embedder.ready:
                    self._index()
                    return
                time.sleep(0.4)

        t = spawn_context_thread(_wait, name="carnet-index-init")
        t.start()

    # -- tools -------------------------------------------------------
    def get_tool_schemas(self) -> list[dict[str, Any]]:
        self._refresh_config()
        schemas = list(TOOLS)
        if self._config.get("notes_enabled"):
            schemas.extend(NOTE_TOOLS)
        return schemas

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        if not self._store:
            return json.dumps({"success": False, "error": "carnet not initialized"}, ensure_ascii=False)
        self._refresh_config()
        args = _modern_args(args)
        try:
            if tool_name == "remember":
                return self._tool_remember(args)
            if tool_name == "recall":
                return self._tool_recall(args)
            if tool_name == "read_fiche":
                return self._tool_read(args)
            if tool_name == "forget":
                return self._tool_forget(args)
            if tool_name == "note":
                return self._tool_note(args)
            if tool_name == "note_close":
                return self._tool_note_close(args)
        except Exception as exc:
            log.warning("carnet: tool %s: %s", tool_name, exc)
            return json.dumps({"success": False, "error": str(exc)[:300]}, ensure_ascii=False)
        return json.dumps({"success": False, "error": f"unknown tool: {tool_name}"}, ensure_ascii=False)

    def _tool_remember(self, args: dict) -> str:
        text = (args.get("text") or "").strip()
        if not text:
            return json.dumps({"success": False, "error": "empty text"}, ensure_ascii=False)
        try:
            importance = int(args.get("importance", 1))
        except (TypeError, ValueError):
            importance = 1
        if importance not in (0, 1, 2):
            importance = 1
        # Layer 2 (sheets, importance=2): no limit — it is a document, read on demand.
        # Layer 0 (core): 250; layer 1 (fact): 500.
        # An overflow is REFUSED, never trimmed: a fact cut at the character level
        # is corrupted and silent data. The model must know and rewrite.
        if importance == 2:
            limit = None
        else:
            key = "seat_char_limit" if importance == 0 else "fact_char_limit"
            limit = int(self._config.get(key, 250 if importance == 0 else 500))
        if limit is not None and len(text) > limit:
            layer = "core seat" if importance == 0 else "fact"
            return json.dumps({
                "success": False,
                "error": "text too long — nothing was recorded",
                "length": len(text),
                "limit": limit,
                "chars_over": len(text) - limit,
                "message": (
                    f"Refused: a {layer} is limited to {limit} characters, your text has "
                    f"{len(text)} ({len(text) - limit} too many). Nothing was written. "
                    f"Rewrite it as a self-contained sentence of {limit} characters maximum and "
                    f"call the tool again; if the long detail is essential, save it as a "
                    f"sheet (importance=2), which has no limit."
                ),
            }, ensure_ascii=False)
        confidence = args.get("confidence") or "probable"
        topic = args.get("topic")
        old = args.get("id")

        if old:
            new = self._store.replace_fact(old, text, importance=importance,
                                           confidence=confidence, topic=topic,
                                           source="agent", session_id=self._session_id)
            if new.get("error"):
                return json.dumps({"success": False, "error": new["error"],
                                   "message": "Correction refused: the original fact is intact. "
                                              + new["error"]}, ensure_ascii=False)
            return json.dumps({"success": True, "message": "fact corrected", "id": new.get("id"),
                               "replaces": old, "_change": True}, ensure_ascii=False)

        res = self._store.add_fact(text, importance=importance, confidence=confidence,
                                   topic=topic, source="agent", session_id=self._session_id)
        # A store refusal must surface as a refusal: announcing "already known"
        # on empty or too short text would make the model believe it was written.
        if res.get("error"):
            return json.dumps({"success": False, "error": res["error"],
                               "message": "Nothing was recorded: " + res["error"]},
                              ensure_ascii=False)
        fid = res.get("id")
        seat = None
        if importance == 0 and fid:
            seat = self._store.promote(fid)
            self._store.meta_set("core_updated_at", _iso_now())
        if res.get("is_new"):
            # Vector right away: in one-shot the worker has no time to run.
            if self._embedder and self._embedder.ready:
                try:
                    self._embedder.index([(fid, text)])
                except Exception:
                    pass
            msg = "fact added" if not (seat and seat.get("seat")) else "fact added to the core"
            if seat and seat.get("pressure"):
                msg += " — core full: the fact stays in layer 1, to be arbitrated later"
            return json.dumps({"success": True, "message": msg, "id": fid, "_change": True},
                              ensure_ascii=False)
        return json.dumps({"success": True, "message": "already known", "id": fid}, ensure_ascii=False)

    def _tool_recall(self, args: dict) -> str:
        q = args.get("query") or ""
        limit = max(1, min(20, int(args.get("limit", 6) or 6)))
        res = self._store.search(q, limit=60)[:limit]
        if not res:
            return json.dumps({"success": True, "results": [], "message": "nothing found"},
                              ensure_ascii=False)
        out = []
        for r in res:
            row = r["row"]
            out.append({"id": row["id"], "score": r["score"], "importance": row["importance"],
                        "confidence": row["confidence"], "date": (row["created_at"] or "")[:10],
                        "text": row["text"][:400]})
        return json.dumps({"success": True, "results": out}, ensure_ascii=False)

    def _tool_read(self, args: dict) -> str:
        fid = (args.get("id") or "").strip()
        row = self._store.fact(fid)
        if not row:
            return json.dumps({"success": False, "error": "unknown id"}, ensure_ascii=False)
        return json.dumps({
            "success": True, "id": row["id"], "text": row["text"],
            "importance": row["importance"], "confidence": row["confidence"],
            "status": row["status"], "topic": row["topic"],
            "evidence": row["evidence"], "evidence_missing": bool(row["evidence_missing"]),
            "created_at": row["created_at"], "session_id": row["session_id"],
        }, ensure_ascii=False)

    def _tool_forget(self, args: dict) -> str:
        fid = (args.get("id") or "").strip()
        ok = self._store.remove_fact(fid, args.get("reason", ""))
        return json.dumps({"success": ok, "message": "removed" if ok else "no change",
                           "_change": ok}, ensure_ascii=False)

    def _tool_note(self, args: dict) -> str:
        if not self._config.get("notes_enabled"):
            return json.dumps({"success": False, "error": "notes layer is disabled"},
                              ensure_ascii=False)
        text = (args.get("text") or "").strip()
        if not text:
            return json.dumps({"success": False, "error": "empty text"}, ensure_ascii=False)
        limit = int(self._config.get("note_char_limit", 300) or 300)
        if len(text) > limit:
            return json.dumps({
                "success": False,
                "error": "text too long — nothing was recorded",
                "length": len(text), "limit": limit, "chars_over": len(text) - limit,
                "message": f"A note is limited to {limit} characters; rewrite it shorter.",
            }, ensure_ascii=False)
        days = int(self._config.get("expiry_days", 7) or 7)
        expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).replace(
            microsecond=0).isoformat()
        res = self._store.add_note(text, expires_at)
        if res.get("error"):
            return json.dumps({"success": False, "error": res["error"]}, ensure_ascii=False)
        return json.dumps({"success": True, "message": "note added", "id": res["id"],
                           "expires_at": expires_at, "_change": True}, ensure_ascii=False)

    def _tool_note_close(self, args: dict) -> str:
        nid = (args.get("id") or "").strip()
        ok = self._store.close_note(nid)
        return json.dumps({"success": ok, "message": "closed" if ok else "no change",
                           "_change": ok}, ensure_ascii=False)

    # -- misc --------------------------------------------------------
    def on_profile_created(self, profile_home: str = "", provider: str = "", **kwargs) -> None:
        """Link the shared code into a new profile that uses carnet.

        Hermes calls this hook when a profile is created. The profile keeps its
        own data in ``<profile>/carnet/``; only the code is shared.
        """
        name = str(kwargs.get("provider") or kwargs.get("memory_provider") or provider or "")
        if name and name != "carnet":
            return
        home = (profile_home or kwargs.get("hermes_home") or kwargs.get("profile")
                or kwargs.get("path") or "")
        if not home or not isinstance(home, (str, os.PathLike)):
            log.warning("carnet: on_profile_created without a profile path")
            return
        try:
            from .profiles import link_profile
            result = link_profile(Path(home))
            log.info("carnet: profile %s: %s", home, result["status"])
        except Exception as exc:
            log.warning("carnet: profile link failed for %s: %s", home, exc)

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "",
                          reset: bool = False, rewound: bool = False, **kwargs) -> None:
        self._session_id = new_session_id or self._session_id
        self._first_turn = True
        self._notes_seen_session = ""

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._embedder:
            self._embedder.stop()
        if self._store:
            self._store.close()

    def backup_paths(self) -> list[str]:
        return []


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
