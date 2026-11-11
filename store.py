"""Carnet — SQLite storage for facts, the work queue and the journal.

No external dependency: stdlib + FTS5 (shipped with SQLite).
Every write goes through this module; it is the only mutation point.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA_VERSION = 1

# ---------------------------------------------------------------- constants

STOPWORDS = set("""
le la les un une des du de d l au aux a et ou mais donc or ni car que qui quoi dont
où quand comme si alors pour par sur sous dans avec sans vers chez entre
je tu il elle on nous vous ils elles me te se ce cet cette ces mon ton son ma ta sa
mes tes ses notre votre leur nos vos leurs y en est sont était étaient être avoir ai as
avons avez ont avait eu fait faire fais
the a an and or but of to in on at for with without from by as is are was were be been
it its this that these those i you he she we they me my your our their
plus moins très trop aussi encore déjà toujours jamais pas ne non oui ok
""".split())

DEFAULTS = {
    "budget_chars_turn": 1400,       # budget of the rendered block, per turn
    "budget_chars_first_turn": 4000,
    "max_facts_per_turn": 8,
    "max_sheets_per_turn": 2,
    "session_fact_limit": 40,
    "sheet_excerpt_chars": 400,
    "seat_limit": 20,
    "fact_char_limit": 500,
    "seat_char_limit": 250,
    "abstention_threshold": 0.55,   # minimum score to inject
    "recall_floor": 0.34,           # below this coverage, recall says "nothing relevant"
    "embed_timeout_ms": 300,
    "embed_backend": "lmstudio",      # disabled | lmstudio | custom | sidecar
    "embed_url": "http://127.0.0.1:1234",
    "embed_model": "text-embedding-embeddinggemma-300m-qat",
    "embed_api_key": "",             # custom endpoints only
    "notes_enabled": False,           # temporary notes are opt-in
    "max_notes": 8,
    "expiry_days": 7,
    "note_char_limit": 300,
    "model_mode": "off",              # off | custom (rephrase and watch)
    "model_url": "",
    "model_name": "",
    "model_api_key": "",
    "rerank": False,
    "python_venv": "",
}

MEMORY_KEYS = (
    "seat_limit", "seat_char_limit", "fact_char_limit", "max_facts_per_turn",
    "max_sheets_per_turn", "session_fact_limit", "budget_chars_turn",
    "budget_chars_first_turn", "sheet_excerpt_chars", "abstention_threshold",
    "recall_floor",
)

NOTE_KEYS = ("max_notes", "expiry_days", "note_char_limit")

NOTE_BOUNDS = {
    "max_notes": (0, 50),
    "expiry_days": (1, 365),
    "note_char_limit": (40, 2000),
}

# Keys that are shares, not counts: they must stay fractional.
FLOAT_KEYS = {"abstention_threshold", "recall_floor"}

SETTING_BOUNDS = {
    "seat_limit": (1, 200),
    "seat_char_limit": (40, 2000),
    "fact_char_limit": (80, 5000),
    "max_facts_per_turn": (0, 50),
    "max_sheets_per_turn": (0, 20),
    "session_fact_limit": (1, 500),
    "budget_chars_turn": (200, 20000),
    "budget_chars_first_turn": (200, 40000),
    "sheet_excerpt_chars": (40, 4000),
    "abstention_threshold": (0.0, 1.0),
    "recall_floor": (0.0, 1.0),
}


def normalize_settings(cfg: dict) -> dict:
    """Clamp the memory settings to their bounds; malformed values use defaults."""
    out = dict(cfg)
    for key, (low, high) in SETTING_BOUNDS.items():
        raw = out.get(key, DEFAULTS[key])
        try:
            value = round(float(raw), 3) if key in FLOAT_KEYS else int(float(raw))
        except (TypeError, ValueError):
            value = DEFAULTS[key]
        out[key] = max(low, min(high, value))
    for key, (low, high) in NOTE_BOUNDS.items():
        raw = out.get(key, DEFAULTS[key])
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            value = DEFAULTS[key]
        out[key] = max(low, min(high, value))
    out["notes_enabled"] = bool(out.get("notes_enabled", DEFAULTS["notes_enabled"]))
    return out


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _uid(prefix: str = "f") -> str:
    import secrets
    return f"{prefix}-{secrets.token_hex(4)}"


_CONNECTORS = ("et ", "mais ", "donc ", "alors ", "car ", "or ", "puis ",
               "parce ", "ainsi ", "sinon ", "ensuite ")


def looks_like_fragment(text: str, *, strict: bool = False) -> str:
    """Return why a text is not a self-contained statement, or "" if it is.

    Models occasionally hand over a piece of their own sentence: a clause cut
    mid-thought, a dictated fragment, a line lifted from a message. Those are
    worthless as memories and, in the core layer, they steal the most expensive
    seats in the prompt. The check is deliberately crude and explainable: a fact
    is a sentence that starts with a capital and ends on a real stop.
    """
    body = (text or "").strip()
    if not body:
        return "empty"
    minimum = 5 if strict else 3
    words = body.split()
    if len(words) < minimum:
        return f"too short for a fact ({len(words)} words)"
    first = body[0]
    if first.islower() or first in ".,;:!?…":
        return "starts mid-sentence"
    if body.lower().startswith(_CONNECTORS):
        return "starts on a connector"
    if body.rstrip().endswith((",", ";", ":", "-", "…")):
        return "ends mid-sentence"
    return ""


def coverage(query: str, text: str) -> float:
    """Share of the query's own words the text actually contains, from 0 to 1.

    This is the number `recall` reports. It is not a similarity and not a
    confidence: 1.0 means every word the caller asked for is present, which is
    allowed to be a poor answer to a vague question — hence the weak flag the
    provider adds around it.
    """
    words = [w for w in normalize(query).split() if len(w) > 2 and w not in STOPWORDS]
    if not words:
        return 0.0
    body = set(normalize(text).split())
    return round(sum(1 for w in words if w in body) / len(words), 3)


def normalize(text: str) -> str:
    """Lowercase, no accents, reduced punctuation — used for hashing and search."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def fingerprint(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()[:32]


def query_terms(text: str, max_terms: int = 18) -> list[str]:
    """Useful tokens from the query, for FTS5. Deduplicated, without stopwords.

    Adjacent pairs are added too: "New York" must find "NewYork", which
    tokenization otherwise splits differently. Without that, a proper name
    written as two words never comes back.
    """
    all_terms = [t for t in normalize(text).split() if len(t) >= 2 and t not in STOPWORDS]
    seen: list[str] = []
    for tok in all_terms:
        if tok.isdigit():
            continue
        if tok not in seen:
            seen.append(tok)
    for a, b in zip(all_terms, all_terms[1:]):
        glued = f"{a}{b}"
        if len(glued) >= 6 and glued not in seen:
            seen.append(glued)
    return seen[:max_terms]


def fts_expression(terms: Iterable[str]) -> str:
    """Safe FTS5 expression: every term quoted, joined with OR."""
    quoted = [f'"{t}"' for t in terms if t]
    return " OR ".join(quoted)


# ---------------------------------------------------------------- the store

class Carnet:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "carnet.db"
        self._local = threading.local()

    # -- connection --------------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        """One connection per thread: SQLite refuses a connection shared across
        threads, and the provider is called from the turn, the worker and the tools."""
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=8.0, isolation_level=None,
                                check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=6000")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            try:
                c.close()
            finally:
                self._local.conn = None

    # -- schema ------------------------------------------------------
    def init_schema(self) -> None:
        c = self.conn
        c.executescript("""
        CREATE TABLE IF NOT EXISTS facts (
            id TEXT PRIMARY KEY,
            text TEXT NOT NULL,
            evidence TEXT,
            evidence_missing INTEGER NOT NULL DEFAULT 0,
            importance INTEGER NOT NULL DEFAULT 1,
            confidence TEXT NOT NULL DEFAULT 'probable',
            topic TEXT,
            keywords TEXT,
            predicate TEXT,
            value TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            replaced_by TEXT,
            valid_from TEXT,
            valid_until TEXT,
            source TEXT NOT NULL DEFAULT 'agent',
            session_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            text_hash TEXT NOT NULL,
            size INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS facts_hash ON facts(text_hash, ifnull(valid_from, ''));
        CREATE INDEX IF NOT EXISTS facts_status ON facts(status);
        CREATE INDEX IF NOT EXISTS facts_importance ON facts(importance, status);
        CREATE INDEX IF NOT EXISTS facts_topic ON facts(topic);

        CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
            fact_id UNINDEXED,
            text,
            topic,
            keywords,
            tokenize="unicode61 remove_diacritics 2"
        );

        CREATE TABLE IF NOT EXISTS propositions (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            fact_a TEXT NOT NULL,
            fact_b TEXT,
            text TEXT,
            reason TEXT,
            score REAL,
            fingerprint TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS propositions_status ON propositions(status, type);

        CREATE TABLE IF NOT EXISTS entities (
            id TEXT PRIMARY KEY,
            canonical TEXT NOT NULL,
            alias TEXT
        );

        CREATE TABLE IF NOT EXISTS links (
            src TEXT NOT NULL,
            dst TEXT NOT NULL,
            rel TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (src, dst, rel)
        );

        CREATE TABLE IF NOT EXISTS injected (
            session_id TEXT NOT NULL,
            fact_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            PRIMARY KEY (session_id, fact_id)
        );

        CREATE TABLE IF NOT EXISTS pending (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            turn TEXT,
            user_text TEXT,
            assistant_text TEXT,
            ts TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS pending_status ON pending(status, id);

        CREATE TABLE IF NOT EXISTS journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            actor TEXT NOT NULL,
            op TEXT NOT NULL,
            fact_id TEXT,
            payload TEXT
        );

        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

        CREATE TABLE IF NOT EXISTS vec (
            fact_id TEXT PRIMARY KEY,
            dim INTEGER NOT NULL,
            blob BLOB NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notes (
            id TEXT PRIMARY KEY,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            closed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS notes_status_expiry ON notes(status, expires_at);
        """)
        self._migrate()
        self._set_meta_if_missing("schema_version", str(SCHEMA_VERSION))
        self._set_meta_if_missing("created_at", _now())

    # -- meta --------------------------------------------------------
    def _migrate(self) -> None:
        """Add recent columns and rebuild the FTS index if its schema changed."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(facts)")}
        prop_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(propositions)")}
        if prop_cols and "fingerprint" not in prop_cols:
            self.conn.execute("ALTER TABLE propositions ADD COLUMN fingerprint TEXT")
            self.conn.commit()
        # Catch-up: earlier propositions have no fingerprint, so an already recorded
        # rejection would be proposed again. Recompute it from the current texts.
        for row in self.conn.execute(
            "SELECT p.id, a.text AS ta, b.text AS tb FROM propositions p "
            "LEFT JOIN facts a ON a.id = p.fact_a LEFT JOIN facts b ON b.id = p.fact_b "
            "WHERE p.fingerprint IS NULL"
        ).fetchall():
            if row["ta"] is not None and row["tb"] is not None:
                self.conn.execute(
                    "UPDATE propositions SET fingerprint=? WHERE id=?",
                    (hashlib.sha256(f"{row['ta']}\x00{row['tb']}".encode()).hexdigest()[:16], row["id"]),
                )
        self.conn.commit()

        if "keywords" not in cols:
            self.conn.execute("ALTER TABLE facts ADD COLUMN keywords TEXT")
        # Usage counters: how many times a fact was kept by recall.
        if "uses" not in cols:
            self.conn.execute("ALTER TABLE facts ADD COLUMN uses INTEGER NOT NULL DEFAULT 0")
        if "last_used_at" not in cols:
            self.conn.execute("ALTER TABLE facts ADD COLUMN last_used_at TEXT")

        # A note is not always a reminder to execute: it can be a question left
        # open or a fact to keep in mind until it is confirmed. And without its
        # originating session, an obscure note cannot be traced back.
        note_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(notes)")}
        if note_cols and "kind" not in note_cols:
            self.conn.execute("ALTER TABLE notes ADD COLUMN kind TEXT NOT NULL DEFAULT 'fact'")
        if note_cols and "session_id" not in note_cols:
            self.conn.execute("ALTER TABLE notes ADD COLUMN session_id TEXT")
        fts_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(facts_fts)")}
        if "keywords" not in fts_cols:
            self.conn.execute("DROP TABLE IF EXISTS facts_fts")
            self.conn.execute("""CREATE VIRTUAL TABLE facts_fts USING fts5(
                fact_id UNINDEXED, text, topic, keywords,
                tokenize="unicode61 remove_diacritics 2")""")
            self.conn.execute(
                """INSERT INTO facts_fts(fact_id, text, topic, keywords)
                   SELECT id, text, ifnull(topic,''), ifnull(keywords,'') FROM facts"""
            )
            self.log("system", "index_rebuild", None, None)

    def _set_meta_if_missing(self, k: str, v: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO meta(k, v) VALUES (?, ?)", (k, v))

    def meta_get(self, k: str, default: str = "") -> str:
        row = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default

    # Tables that hold memory, as opposed to the schema itself. A cloned
    # profile must start empty, not inherit another agent's life.
    MEMORY_TABLES = (
        "facts_fts", "vec", "injected", "pending", "propositions",
        "links", "entities", "journal", "notes", "facts",
    )

    def reset_memory(self, reason: str = "") -> int:
        """Deletes every fact, vector, note and trace, keeping the schema.

        Returns the number of facts removed, so the caller can log something
        precise rather than a vague "cleaned up".
        """
        removed = 0
        try:
            removed = int(self.conn.execute("SELECT count(*) n FROM facts").fetchone()["n"])
        except Exception:
            removed = 0
        for table in self.MEMORY_TABLES:
            try:
                self.conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        for key in ("seat_pressure", "core_updated_at", "last_watch"):
            try:
                self.conn.execute("DELETE FROM meta WHERE k=?", (key,))
            except Exception:
                pass
        try:
            self.meta_set("created_at", _now())
        except Exception:
            pass
        self.conn.commit()
        if reason:
            try:
                self.log("system", "memory_reset", None, {"reason": reason, "facts_removed": removed})
            except Exception:
                pass
        return removed

    def meta_set(self, k: str, v: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, v),
        )

    # -- journal -----------------------------------------------------
    def log(self, actor: str, op: str, fact_id: Optional[str], payload: Any = None) -> None:
        self.conn.execute(
            "INSERT INTO journal(ts, actor, op, fact_id, payload) VALUES (?,?,?,?,?)",
            (_now(), actor, op, fact_id, json.dumps(payload, ensure_ascii=False) if payload is not None else None),
        )

    # -- writing -----------------------------------------------------
    def add_fact(
        self,
        text: str,
        *,
        evidence: Optional[str] = None,
        importance: int = 1,
        confidence: str = "probable",
        topic: Optional[str] = None,
        keywords: Optional[str] = None,
        predicate: Optional[str] = None,
        value: Optional[str] = None,
        source: str = "agent",
        session_id: Optional[str] = None,
        actor: str = "agent",
        force: bool = False,
    ) -> dict:
        """Insert a fact. Returns {'id', 'is_new': bool}. Never a hard failure."""
        text = (text or "").strip()
        if not text:
            return {"id": None, "is_new": False, "error": "empty text"}
        # A fragment ("I mostly use") comes back on almost every query because it
        # is short: that is noise, not a fact.
        if len(text) < 15 or len(text.split()) < 3:
            return {"id": None, "is_new": False, "error": "fact too short: write a complete sentence"}

        # A core seat is only obtained through promote(), which enforces the limit.
        importance = 1 if int(importance) == 0 else int(importance)

        h = fingerprint(text)
        now = _now()
        existing = self.conn.execute(
            "SELECT id, importance FROM facts WHERE text_hash=? AND ifnull(valid_from,'')=? AND status='active'",
            (h, ""),
        ).fetchone()
        if existing:
            # A fact rewritten higher in the hierarchy (core) must win its seat.
            if int(importance) == 0 and int(existing["importance"]) != 0:
                self.promote(existing["id"])
            return {"id": existing["id"], "is_new": False}

        # Inclusion: the same fact already present in another form.
        # Never for an explicit correction, and never between two texts of similar
        # length (otherwise "… number 1" swallows "… number 11").
        compact = normalize(text).replace(" ", "")
        if not force and len(compact) >= 12:
            for r in self.conn.execute(
                "SELECT id, text, importance FROM facts WHERE status='active' AND size <= ?",
                (len(text) * 2,),
            ):
                other = normalize(r["text"]).replace(" ", "")
                short, long = sorted((compact, other), key=len)
                if len(short) <= 0.8 * len(long) and (short in long):
                    if int(importance) == 0 and int(r["importance"]) != 0:
                        self.promote(r["id"])
                    return {"id": r["id"], "is_new": False, "already_covered": True}

        fid = _uid("f")
        self.conn.execute(
            """INSERT INTO facts(id, text, evidence, evidence_missing, importance, confidence,
                                 topic, predicate, value, keywords, status, source, session_id,
                                 created_at, updated_at, text_hash, size)
               VALUES (?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?,?,?)""",
            (
                fid, text, evidence, 0 if evidence else 1, int(importance), confidence,
                topic, predicate, value, keywords, source, session_id, now, now, h, len(text),
            ),
        )
        self.conn.execute(
            "INSERT INTO facts_fts(fact_id, text, topic, keywords) VALUES (?,?,?,?)",
            (fid, normalize(text), normalize(topic or ""), normalize(keywords or "")),
        )
        self.log(actor, "created", fid, {"text": text[:200], "importance": importance})
        return {"id": fid, "is_new": True}

    def replace_fact(self, old_id: str, text: str, **kw) -> dict:
        """New version: the old one becomes 'replaced'. Nothing is destroyed.

        The correction inherits the layer: if the old text held a core seat, the new
        one is promoted there. Promotion happens AFTER the seat is freed, otherwise
        the limit of 20 refuses it and the seat disappears.
        """
        old = self.conn.execute(
            "SELECT importance FROM facts WHERE id=?", (old_id,)
        ).fetchone()
        inherits_seat = bool(old) and int(old["importance"]) == 0
        new = self.add_fact(text, force=True, **kw)
        if new.get("id") and new.get("is_new"):
            now = _now()
            self.conn.execute(
                "UPDATE facts SET status='replaced', replaced_by=?, valid_until=?, updated_at=? WHERE id=?",
                (new["id"], now, now, old_id),
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO links(src,dst,rel,created_at) VALUES (?,?,?,?)",
                (new["id"], old_id, "replaces", now),
            )
            self.log("agent", "replacement", old_id, {"by": new["id"]})
            if inherits_seat:
                seat = self.promote(new["id"])
                new["seat"] = seat
        return new

    # -- temporary notes --------------------------------------------
    def add_note(self, text: str, expires_at: str, kind: str = "fact",
                 session_id: str = "") -> dict:
        """Create an active note. Expiry is stored, never enforced by deletion."""
        text = (text or "").strip()
        if not text:
            return {"id": None, "error": "empty text"}
        kind = (kind or "fact").strip().lower()
        if kind not in ("task", "question", "fact"):
            kind = "fact"
        nid = _uid("n")
        now = _now()
        self.conn.execute(
            "INSERT INTO notes(id, text, created_at, expires_at, status, kind, session_id)"
            " VALUES (?,?,?,?,'active',?,?)",
            (nid, text, now, expires_at, kind, session_id or None),
        )
        self.log("agent", "note_created", None,
                 {"id": nid, "expires_at": expires_at, "kind": kind})
        return {"id": nid, "created_at": now, "expires_at": expires_at, "kind": kind}

    def active_notes(self, limit: int = 8, now: str = "") -> list[sqlite3.Row]:
        """Active notes that have not expired, oldest first."""
        return list(self.conn.execute(
            "SELECT id, text, created_at, expires_at, status, closed_at, kind, session_id"
            " FROM notes "
            "WHERE status='active' AND expires_at > ? "
            "ORDER BY created_at ASC, id ASC LIMIT ?",
            (now or _now(), max(0, int(limit))),
        ))

    def close_note(self, nid: str) -> bool:
        cur = self.conn.execute(
            "UPDATE notes SET status='closed', closed_at=? WHERE id=? AND status='active'",
            (_now(), nid),
        )
        if cur.rowcount:
            self.log("agent", "note_closed", None, {"id": nid})
            return True
        return False

    def delete_note(self, nid: str) -> bool:
        cur = self.conn.execute("DELETE FROM notes WHERE id=?", (nid,))
        if cur.rowcount:
            self.log("agent", "note_deleted", None, {"id": nid})
            return True
        return False

    def remove_fact(self, fid: str, reason: str = "") -> bool:
        cur = self.conn.execute(
            "UPDATE facts SET status='removed', updated_at=? WHERE id=? AND status!='removed'",
            (_now(), fid),
        )
        if cur.rowcount:
            self.log("agent", "removal", fid, {"reason": reason})
            return True
        return False

    # -- layer 0: the core -------------------------------------------
    def core(self, limit: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            """SELECT * FROM facts WHERE importance=0 AND status='active'
               ORDER BY updated_at DESC, created_at DESC LIMIT ?""",
            (limit,),
        ))

    def free_seats(self, limit: int) -> int:
        n = self.conn.execute(
            "SELECT count(*) n FROM facts WHERE importance=0 AND status='active'"
        ).fetchone()["n"]
        return max(0, limit - n)

    def promote(self, fid: str, limit: Optional[int] = None) -> dict:
        """Try to grant a seat. Never refuses: returns the actual state."""
        if limit is None:
            limit = int(self.meta_get("seat_limit", str(DEFAULTS["seat_limit"])))
        if self.free_seats(limit) <= 0:
            self.conn.execute(
                "UPDATE facts SET updated_at=? WHERE id=?", (_now(), fid)
            )
            self.meta_set("seat_pressure", fid)
            self.log("agent", "seat_pressure", fid, {"limit": limit})
            return {"seat": False, "pressure": True, "limit": limit}
        self.conn.execute("UPDATE facts SET importance=0, updated_at=? WHERE id=?", (_now(), fid))
        self.meta_set("seat_pressure", "")
        self.log("agent", "seat_promotion", fid, None)
        return {"seat": True, "pressure": False}

    # -- work queue --------------------------------------------------
    def enqueue_turn(self, session_id: str, user_text: str, assistant_text: str, turn: str = "") -> None:
        self.conn.execute(
            "INSERT INTO pending(session_id, turn, user_text, assistant_text, ts) VALUES (?,?,?,?,?)",
            (session_id, turn, (user_text or "")[:8000], (assistant_text or "")[:8000], _now()),
        )

    def queue_head(self, limit: int = 10) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM pending WHERE status='new' ORDER BY id LIMIT ?", (limit,)
        ))

    def mark_done(self, pid: int) -> None:
        self.conn.execute("UPDATE pending SET status='done' WHERE id=?", (pid,))

    def mark_error(self, pid: int, error: str) -> None:
        self.conn.execute(
            "UPDATE pending SET attempts=attempts+1, error=? WHERE id=?",
            (error[:400], pid),
        )

    def queue_size(self) -> int:
        return self.conn.execute("SELECT count(*) n FROM pending WHERE status='new'").fetchone()["n"]

    # -- session -----------------------------------------------------
    def mark_injected(self, session_id: str, ids: Iterable[str]) -> None:
        now = _now()
        ids = list(ids)
        self.conn.executemany(
            "INSERT OR IGNORE INTO injected(session_id, fact_id, ts) VALUES (?,?,?)",
            [(session_id, i, now) for i in ids],
        )
        # The usage counter is incremented on every turn where the fact is kept,
        # not only the first time in the session.
        self.conn.executemany(
            "UPDATE facts SET uses = coalesce(uses,0) + 1, last_used_at=? WHERE id=?",
            [(now, i) for i in ids],
        )

    def already_injected(self, session_id: str) -> set[str]:
        return {r["fact_id"] for r in self.conn.execute(
            "SELECT fact_id FROM injected WHERE session_id=?", (session_id,)
        )}

    def forget_injections(self, session_id: str) -> None:
        self.conn.execute("DELETE FROM injected WHERE session_id=?", (session_id,))

    # -- reading -----------------------------------------------------
    def search(self, query: str, *, limit: int = 60) -> list[dict]:
        """FTS5 lexical search. Returns dicts with a normalized score."""
        terms = query_terms(query)
        if not terms:
            return []
        expr = fts_expression(terms)
        try:
            rows = list(self.conn.execute(
                """SELECT f.*, bm25(facts_fts) AS rank
                   FROM facts_fts JOIN facts f ON f.id = facts_fts.fact_id
                   WHERE facts_fts MATCH ? AND f.status='active'
                   ORDER BY rank LIMIT ?""",
                (expr, limit),
            ))
        except sqlite3.OperationalError:
            return []
        if not rows:
            return []

        worst = min(r["rank"] for r in rows)     # bm25 is negative: the most negative is best
        term_set = set(terms)
        output = []
        q_norm = normalize(query)
        for r in rows:
            base = 1.0 if worst == 0 else max(0.0, min(1.0, r["rank"] / worst))
            score = base
            # Capped length penalty: beyond 600 chars a text is not "vaguer",
            # it is just longer (a sheet stays a sheet).
            _t = int(r["size"])
            score /= (1.0 + max(0, min(_t, 600) - 120) / 600.0)
            # importance: core > facts > sheets
            imp = int(r["importance"])
            score *= {0: 1.15, 1: 1.0, 2: 0.9}.get(imp, 1.0)
            # topic bonus: the title carries the theme, which is a strong signal
            title = f"{r['topic'] or ''} {r['keywords'] or ''}".strip()
            if title:
                common = len(set(normalize(title).split()) & term_set)
                if common >= 2:
                    score *= 1.35
                elif common == 1:
                    score *= 1.15
            # exact phrase bonus
            if len(q_norm) > 6 and q_norm in normalize(r["text"]):
                score *= 1.6
            # confidence: a certain fact beats a guessed one, all else equal
            if r["confidence"] == "certain":
                score *= 1.08
            output.append({"row": r, "score": round(score, 4)})
        output.sort(key=lambda d: d["score"], reverse=True)
        return output

    def fact(self, fid: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone()

    def facts_to_index(self, limit: int = 64) -> list[tuple[str, str]]:
        """Active facts without a vector — to send to the embedding server."""
        rows = self.conn.execute(
            """SELECT f.id, f.text FROM facts f
               LEFT JOIN vec v ON v.fact_id = f.id
               WHERE v.fact_id IS NULL AND f.status='active'
               ORDER BY f.created_at DESC LIMIT ?""", (limit,)
        )
        return [(r["id"], r["text"]) for r in rows]

    def indexed_count(self) -> int:
        return self.conn.execute("SELECT count(*) n FROM vec").fetchone()["n"]

    def count(self) -> dict:
        c = self.conn
        return {
            "facts": c.execute("SELECT count(*) n FROM facts WHERE status='active'").fetchone()["n"],
            "core": c.execute("SELECT count(*) n FROM facts WHERE status='active' AND importance=0").fetchone()["n"],
            "sheets": c.execute("SELECT count(*) n FROM facts WHERE status='active' AND importance=2").fetchone()["n"],
            "queue": self.queue_size(),
            "contested": c.execute("SELECT count(*) n FROM facts WHERE status='contested'").fetchone()["n"],
        }
