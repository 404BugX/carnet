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
import logging
import os
import re
import sqlite3
import stat
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from agent.memory_provider import MemoryProvider, RecallStatus, spawn_context_thread

from .embedder import Embedder
from .store import (DEFAULTS, STOPWORDS, Carnet, coverage, looks_like_fragment,
                    normalize, normalize_settings)

log = logging.getLogger("carnet")

GLYPH = "📓"

# Tool schemas: six, and each one is paid for on every turn. The note pair is
# small on purpose: a note is written once and closed once.
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
            "a precise fact before asserting something. Returns identifiers and excerpts. "
            "`score` is the share of your own query terms the fact contains (1.0 = every term), "
            "not a confidence: `weak: true` means less than half of them are there. When nothing "
            "reaches the floor, the answer is an empty list plus a `nothing relevant` message — "
            "that means the memory does not know, so say it instead of guessing."
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
            "the next session, with its id. Close it with `note_close(id)` once handled. "
            "Set `kind` to say what it is: a task to perform, a question left open, or a "
            "fact to keep in mind until confirmed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The note, self-contained and short."},
                "kind": {
                    "type": "string",
                    "enum": ["task", "question", "fact"],
                    "description": "task = something to do, question = left open for the user, fact = to keep in mind.",
                },
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
    # `(?<!\w)` : without it, a trigger fires inside "notebook", "annotate",
    # "denote". `note` is handled separately on purpose: as a bare word it fires
    # in any prose ("see the note below", "note_close"), and that is how a pasted
    # review ends up sliced into facts. It only counts before a first person.
    r"(?<!\w)(?:remember|memorize|remind me)\s+(?:that\s+)?(.{10,400})",
    re.IGNORECASE,
)
_RE_NOTE = re.compile(
    r"(?<!\w)note\s+that\s+((?:I|my|we|our)\b.{10,400})",
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
        # The trigger word was consumed, so the tail legitimately starts on a
        # lowercase word: restore the capital before judging the shape.
        fragment = (fragment or "").strip()
        if fragment and fragment[0].islower():
            fragment = fragment[0].upper() + fragment[1:]
        if not _self_contained(fragment, minimum) or len(fragment) > 500:
            return
        # Last gate: it must read as a sentence on its own, never as the tail of
        # whatever surrounded it in the original message. Same rule as the tool
        # for a layer-1 fact (strict is reserved for a core seat).
        if looks_like_fragment(fragment, strict=False):
            return
        output.append({"text": fragment, "confidence": confidence, "source": "user"})

    # A long paste is not a statement: a real "remember this" lives in the first
    # lines, and scanning further only harvests fragments of someone else's text.
    body = (user_text or "")[:1200]

    for m in _RE_EXPLICIT.finditer(body):
        _add(_first_sentence(m.group(1)), 10, "certain")
    for m in _RE_NOTE.finditer(body):
        _add(_first_sentence(m.group(1)), 10, "certain")
    if not output:
        for m in _RE_PREFERENCE.finditer(body):
            lead = body[max(0, m.start() - 40):m.start()]
            # Quoted prose, a pasted log or a bulleted list is not a preference:
            # the speaker is reporting someone else's words, not stating a taste.
            # No "and a colon" escape: `He wrote "I hate X"` is a quotation too.
            if any(q in lead for q in ('"', "\u201c", "\u201d", "\u00ab", "\u00bb", ":", ">", "- ", "* ")):
                continue
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

def _secret(name: str) -> str:
    """Read a credential through Hermes's own route.

    A plugin must not invent its own credential store: `agent.secret_scope`
    honours the active profile, so a multiplexed gateway never hands one
    profile's key to another. The plugin config remains a fallback for keys
    typed in the settings screen before this route existed.
    """
    try:
        from agent.secret_scope import get_secret

        return str(get_secret(name, "") or "")
    except Exception:
        return ""


def _fix_mojibake(text: str) -> str:
    """Undo a UTF-8 text that was decoded as CP1252 somewhere upstream.

    The desktop app can hand us text like "Tr\xe2\x88\x9a\xc2\xaes": the bytes are
    valid UTF-8 read through the wrong table. Repairing here keeps the journal,
    the vectors and the facts free of it.
    """
    if not text or not isinstance(text, str):
        return text or ""
    if all(ord(ch) < 0x80 for ch in text):
        return text
    # Two tables produce this: CP1252 (the usual one) and MacRoman, which turns
    # "à" into "√†". Try both and keep the first that decodes cleanly.
    best = text
    for encoding in ("cp1252", "mac_roman"):
        try:
            candidate = text.encode(encoding, errors="strict").decode("utf-8", errors="strict")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if candidate and candidate.count("\ufffd") <= best.count("\ufffd"):
            best = candidate
    return best


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


def _canonical(path: str | Path) -> str:
    """The same folder reached through a symlink is the same folder.

    The clone check used to compare raw strings, so a home opened via a symlink
    looked like a different home and the memory was wiped. Resolve both sides,
    and never raise: a path that no longer exists still has to compare.
    """
    try:
        resolved = str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError):
        resolved = str(Path(path).expanduser().absolute())
    # macOS APFS is case-insensitive by default: /Users/X and /users/x are the
    # same folder. normcase folds them on the platforms that do that.
    return os.path.normcase(resolved)


def _db_id(path: str | Path) -> tuple[str, Optional[str]]:
    """(state, identity) of another home's database.

    state is "missing" when there is no database there, "unreadable" when one is
    there but cannot be read (a writer holds it, a slow volume, a FIFO), and "ok"
    when it was read — in which case the identity may still be None, because a
    database written before this version has none.
    """
    target = Path(path).expanduser()
    try:
        info = target.lstat()
    except OSError:
        return ("missing", None)
    # A FIFO or a device at that path would block the open for ever: rule it out
    # before touching SQLite.
    if not stat.S_ISREG(info.st_mode):
        return ("unreadable", None)
    try:
        # "?" and "#" in a path break a SQLite URI and turn mode=ro into a create.
        uri = "file:" + urllib.parse.quote(str(target)) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            row = conn.execute("SELECT v FROM meta WHERE k='db_id'").fetchone()
            return ("ok", str(row[0]).strip() if row and row[0] else None)
        finally:
            conn.close()
    except (sqlite3.DatabaseError, OSError, ValueError):
        return ("unreadable", None)


def _home_marker(home: str | Path) -> Optional[str]:
    """The identifier of a home, stored beside the database, not inside it.

    It survives a quarantine of the database (which renames `carnet.db` aside),
    so a home whose memory was set aside is still recognisable as itself.
    """
    try:
        return ((Path(home).expanduser() / "carnet" / "home-id").read_text() or "").strip() or None
    except OSError:
        return None


def _ensure_home_marker(home: str | Path) -> Optional[str]:
    """Read the identifier of this home, creating it on first run."""
    existing = _home_marker(home)
    if existing:
        return existing
    value = uuid.uuid4().hex
    try:
        folder = Path(home).expanduser() / "carnet"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "home-id"
        target.write_text(value + "\n")
        os.chmod(target, 0o600)
        return value
    except OSError as exc:
        log.warning("carnet: could not write the home identifier: %s", exc)
        return None


def _looks_like_home(path: str | Path) -> bool:
    """Does this path still hold a Hermes home with a Carnet database?

    Existence is not enough: a file, or an empty folder recreated later at the
    same place, would make a *moved* profile look like a *copy* and empty it.
    """
    try:
        root = Path(path).expanduser()
        return root.is_dir() and (root / "carnet" / "carnet.db").exists()
    except OSError:
        return False


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
        self._worker = None
        self._worker_gen = 0
        self._drain_lock = threading.Lock()
        self._lock = threading.Lock()
        self._first_turn = True
        self._notes_seen_session = ""

    # -- identity ----------------------------------------------------
    @property
    def name(self) -> str:
        return "carnet"

    def is_available(self) -> bool:
        return True

    def _check_clone(self) -> None:
        """A copied home must not inherit another agent's memories.

        ``hermes profile create`` copies the whole home, memory database
        included. When the stamped home still exists, this database is a copy:
        the old file is set aside and a fresh one takes its place. When it is
        gone, the profile was moved and the memory has to be kept. Paths are
        compared after resolution, so the same home opened through a symlink is
        not mistaken for a move.
        """
        try:
            store = self._store
            if store is None:                  # initialize() sets it first
                return
            stamped = store.meta_get("profile_home", "")
            here = _canonical(self._home or "")
            if not stamped:
                _ensure_home_marker(self._home or "")
                store.meta_set("profile_home", here)
                # A database that already holds facts but has lost its stamp is
                # not a fresh profile: it was copied, or it is old enough to
                # predate the stamp. Adopting it silently is what the check
                # exists to prevent, so it is at least flagged.
                if store.count()["facts"] > 0:
                    store.meta_set("clone_suspect", "unknown origin")
                    log.warning("carnet: this memory has %d facts but no recorded "
                                "origin; adopting it and flagging it",
                                store.count()["facts"])
            elif _canonical(stamped) != here:
                if self._is_copy_of(stamped):
                    self._quarantine_memory(stamped)
                else:
                    log.info("carnet: database moved from %s to %s, memory kept", stamped, here)
                    store.meta_set("profile_home", here)
        except Exception as exc:
            log.warning("carnet: clone check failed: %s", exc)

    def _is_copy_of(self, origin: str) -> bool:
        """Is this database a copy of the memory that lives at `origin`?

        The database carries its own identity, written once, and it travels with
        a copy as well as with a move. So the question is not "does that folder
        still exist" but "does the file over there have the same identity as
        mine": yes means this file is a copy of it, whatever the folders look
        like. A home identifier is a second witness, for databases written before
        this version and for an origin whose database was renamed aside.

        When both sides are identifiable and disagree, this file is not a copy of
        what sits there now. It may still hold a profile deleted since, and
        erasing it would destroy the only remaining copy: it is flagged instead.
        """
        ours = ""
        try:
            ours = self._store.meta_get("db_id", "") if self._store else ""
        except Exception:
            ours = ""
        origin_db = Path(origin).expanduser() / "carnet" / "carnet.db"
        state, theirs = _db_id(origin_db)
        mine = _home_marker(self._home or "")
        other = _home_marker(origin)
        if ours and theirs and ours == theirs:
            return True
        if mine and other and mine == other:
            return True
        if state == "unreadable":
            # The database is there but we could not read it (a writer holds it,
            # a slow volume). Not knowing is not the same as knowing it differs:
            # set the copy aside, which is reversible, rather than adopt another
            # profile's memory because the check happened to be unlucky.
            log.warning("carnet: could not read the identity of %s; treating this "
                        "memory as a copy and setting it aside", origin_db)
            return True
        if (ours or mine) and (theirs or other):
            self._flag_clone_suspect(origin)
            return False
        return _looks_like_home(origin)

    def _flag_clone_suspect(self, origin: str) -> None:
        try:
            if self._store:
                self._store.meta_set("clone_suspect", str(origin))
        except Exception:
            pass
        log.warning(
            "carnet: %s holds a memory that did not come from the profile now "
            "living at %s; keeping it and flagging it", self._home, origin)

    def _quarantine_memory(self, origin: str) -> None:
        """Set a copied database aside instead of erasing it.

        The home it came from still exists, so this file holds another agent's
        memories. Renaming it keeps the evidence: if the guess was wrong, nothing
        is lost and the old file sits right there with a timestamp in its name.
        """
        root, store = self._root, self._store
        if root is None or store is None:          # initialize() sets both first
            log.warning("carnet: quarantine requested before initialization")
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        candidate, n = stamp, 1
        while (root / f"carnet-{candidate}.db").exists():   # never overwrite an aside file
            n += 1
            candidate = f"{stamp}-{n}"
        stamp = candidate
        try:
            store.close()
        except Exception:
            pass
        moved = False
        for suffix in ("", "-wal", "-shm"):
            source = root / f"carnet.db{suffix}"
            if not source.exists():
                continue
            try:
                source.rename(root / f"carnet-{stamp}.db{suffix}")
                moved = moved or suffix == ""
            except OSError as exc:
                log.warning("carnet: could not set %s aside: %s", source.name, exc)
        if not moved:
            # The copy is still on disk: stamping it as ours would freeze another
            # agent's memory in place and the check would never run again.
            log.warning("carnet: keeping the copy in place, it will be re-checked next start")
            self._store = Carnet(root)
            self._store.init_schema()
            return
        # The injection journal holds another agent's queries: it is copied along
        # with the database and must not stay either.
        for name in ("injections.jsonl", "embed.log"):
            try:
                (root / name).unlink()
            except OSError:
                pass
        self._store = Carnet(root)
        self._store.init_schema()
        self._store.meta_set("profile_home", _canonical(self._home or root))
        log.info("carnet: database copied from %s, previous one kept as carnet-%s.db",
                 origin, stamp)

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
        self._check_clone()
        self._refresh_config()
        self._session_id = session_id or ""
        self._first_turn = True
        self._notes_seen_session = ""
        # Vectors: separate server, started in the background so the turn is never blocked.
        try:
            server = Path(__file__).with_name("sidecar") / "embed_server.py"
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
                                      api_key=_secret("CARNET_EMBED_API_KEY"),
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
            # Persisting the seat limit is a convenience, not a condition: under
            # a writer lock it must not abort the session. The in-memory config
            # is already the right one.
            try:
                self._store.meta_set("seat_limit", str(int(self._config["seat_limit"])))
            except sqlite3.OperationalError as exc:
                log.warning("carnet: could not persist seat_limit (%s); using %s from config",
                            exc, int(self._config["seat_limit"]))

    # -- layer 0: the core -------------------------------------------
    def system_prompt_block(self) -> str:
        if not self._store:
            return ""
        self._refresh_config()
        limit = int(self._config["seat_limit"])
        seats = self._store.core(limit)
        date = (self._store.meta_get("core_updated_at", "") or self._store.meta_get("created_at", ""))[:10]
        counts = {}
        try:
            counts = self._store.count()
        except Exception:
            counts = {}
        notes_n = 0
        try:
            notes_n = len(self._store.active_notes(limit=500)) if self._config.get("notes_enabled") else 0
        except Exception:
            notes_n = 0
        header = f"[MEMORY — permanent core, {len(seats)}/{limit} seats"
        if counts.get("facts"):
            header += f" · {counts['facts']} facts"
        if counts.get("sheets"):
            header += f" · {counts['sheets']} sheets"
        if notes_n:
            header += f" · {notes_n} notes"
        header += f", as of {date or 'n/a'}]"
        lines = [header]
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
            "Markers in what follows: ✓ certain · ✓? certain but without evidence · "
            "~ probable · [0] core seat, [1] fact, [2] sheet · each line ends with its id, "
            "usable with `forget` and `read_fiche`. A score in `recall` is the share of "
            "your query terms the fact contains, not a confidence in the answer."
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
        query = _fix_mojibake(query)
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
            has_terms = any(
                len(w) > 2 and w not in STOPWORDS for w in normalize(query).split()
            )

            # Beyond N facts already injected in the session, only the very relevant
            # are kept: a long session must not end with 70 facts in the context.
            session_limit = int(self._config.get("session_fact_limit", 40))
            tight = len(already) >= session_limit
            threshold = threshold * (1.6 if tight else 1.0)

            # Eight 2 000-character notes are 16 000 characters: the block is
            # capped to a third of the budget before anything else is counted.
            note_room = max(0, budget // 3)
            if len(notes_block) > note_room:
                notes_block = notes_block[:note_room].rstrip() + "…"

            # Everything that reaches the prompt is counted: the header, the
            # health line and the notes block are part of the bill. They used to
            # be appended after the accounting, which is how a 1 400-character
            # budget ended up sending 15 000.
            header_room = 90
            health = self._health_line()
            extras = [p for p in (notes_block, health) if p]
            reserve = header_room + sum(len(p) + 2 for p in extras)
            room = max(0, budget - reserve)

            kept, total, n_facts, n_sheets = [], 0, 0, 0
            for r in results:
                row = r["row"]
                if r["score"] < threshold:
                    break
                # Relevance before novelty: a fact injected only because it was
                # never seen costs context at every turn and dilutes the signal.
                # Skipped on a query made only of stopwords ("ok, merci"), where
                # no term could ever match.
                if has_terms and self._coverage(query, row["text"]) <= 0:
                    continue
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
                if total + len(line) > room:
                    if kept:
                        break
                    # A single item larger than the whole budget. Clip the body,
                    # never the pointer: the id and the read_fiche call are what
                    # let the agent open the rest (R5).
                    if room <= 0:
                        break
                    pointer = f" → read_fiche({row['id']})"
                    keep_chars = room - len(pointer) - 2
                    if keep_chars <= 0:
                        line = pointer.strip()
                    else:
                        line = line[:keep_chars].rstrip() + "…" + pointer
                kept.append((row["id"], line))
                total += len(line)

            self._first_turn = False
            if not kept:
                self._last_prefetch = []
                parts = [p for p in (notes_block, health) if p]
                return "\n\n".join(parts)

            self._store.mark_injected(sid, [i for i, _ in kept])
            self._last_prefetch = [i for i, _ in kept]
            header = f"[memory · {len(kept)} new · {len(already)} already in context]"
            block = [header] + [l for _, l in kept]
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
            # Silence reads as "nothing to remember", which is a lie: say the
            # channel is down so the model does not answer from an empty memory.
            return "[memory · unavailable this turn]"

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
            # Cap the journal: it is a debugging trace, not a record. Past 8 MB
            # the oldest half is dropped, so a long-lived profile cannot fill
            # the disk with turn transcripts.
            try:
                if p.exists() and p.stat().st_size > 8 * 1024 * 1024:
                    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                    p.write_text("\n".join(lines[-4000:]) + "\n", encoding="utf-8")
            except OSError:
                pass
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
        return f"[{row['importance']}] {mark} {date} · {row['id']} · {text}"

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
                kind = (row["kind"] or "fact") if "kind" in row.keys() else "fact"
                tag = f"({kind}) " if kind in ("task", "question") else ""
                lines.append(f"· [{age}d · {row['id']}] {tag}{text}")
        if not lines:
            return ""
        return "\n".join([
            f"[pending notes · {len(lines)} — close one with note_close(id) once handled]",
            *lines,
        ])

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
        # A memory that may have been copied from another profile is not a
        # detail to keep in the process log: the agent should know.
        suspect = self._store.meta_get("clone_suspect", "")
        if suspect:
            alerts.append(f"memory may come from another profile ({suspect})")
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
        # A worker left over from a previous initialize() may still be finishing a
        # drain. Clearing _stop for the new one would revive it: both would loop
        # and both would drain the same queue. Each worker therefore carries a
        # generation, and a worker that is no longer the current one leaves after
        # its current pass.
        self._worker_gen = getattr(self, "_worker_gen", 0) + 1
        generation = self._worker_gen
        old = self._worker
        if old and old.is_alive():
            self._stop.set()
            self._wake.set()
            old.join(timeout=2.0)
            if old.is_alive():
                log.info("carnet: previous worker still finishing; it will exit after this pass")
        self._stop.clear()

        def _loop():
            while not self._stop.is_set() and self._worker_gen == generation:
                self._wake.wait(timeout=5.0)
                self._wake.clear()
                if self._stop.is_set() or self._worker_gen != generation:
                    break
                try:
                    with self._drain_lock:
                        self._drain()
                except Exception as exc:
                    log.warning("carnet: worker: %s", exc)

        self._worker = spawn_context_thread(_loop, name="carnet-worker")
        try:
            self._worker.daemon = True      # a forgotten shutdown() must not hang exit
        except (AttributeError, RuntimeError):
            pass
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
        # `note` is declared even when the layer is off: the runtime builds its
        # tool registry once at session start, so a schema that only appears
        # after a settings change is rejected as an unknown tool. Availability is
        # enforced in the call itself, where the message can explain it.
        return list(TOOLS) + list(NOTE_TOOLS)

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
        text = _fix_mojibake((args.get("text") or "").strip())
        if not text:
            return json.dumps({"success": False, "error": "empty text"}, ensure_ascii=False)
        try:
            importance = int(args.get("importance", 1))
        except (TypeError, ValueError):
            importance = 1
        if importance not in (0, 1, 2):
            importance = 1
        # A fragment is not a memory. The core layer is checked harder because
        # each of its seats is charged to every single turn.
        if importance < 2:
            why = looks_like_fragment(text, strict=(importance == 0))
            if why:
                return json.dumps({
                    "success": False,
                    "error": "not a self-contained fact",
                    "reason": why,
                    "message": (
                        "Nothing was recorded. A fact is one full sentence about one thing, "
                        "starting with a capital and ending on a stop. Rewrite it in full "
                        "instead of copying a piece of your own message."
                    ),
                }, ensure_ascii=False)
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

    def _coverage(self, query: str, text: str) -> float:
        """Share of the query's own terms the text actually contains.

        This is what the score means in `recall`: not a confidence, not a cosine,
        but "how much of what you asked for is in this fact". 1.0 is therefore a
        full lexical match, and it is allowed to be a bad answer to a vague
        question — which is exactly what the `weak` flag says.
        """
        return coverage(query, text)

    def _tool_recall(self, args: dict) -> str:
        q = _fix_mojibake(args.get("query") or "")
        limit = max(1, min(20, int(args.get("limit", 6) or 6)))
        results = self._candidates(q)
        ranked = [(self._coverage(q, r["row"]["text"]), r) for r in results]
        ranked.sort(key=lambda pair: (pair[0], pair[1]["score"]), reverse=True)
        floor = float(self._config.get("recall_floor", 0.34))
        best = ranked[0][0] if ranked else 0.0

        # A confident false negative is the worst failure mode for a memory: say
        # plainly that nothing relevant was found, instead of returning noise.
        if not ranked or best < floor:
            return json.dumps({
                "success": True,
                "results": [],
                "coverage": best,
                "message": (
                    "nothing relevant"
                    if ranked else "nothing found"
                ) + f" — the best match covers {round(best * 100)}% of your query terms",
            }, ensure_ascii=False)

        out = []
        for cov, r in ranked[:limit]:
            row = r["row"]
            out.append({
                "id": row["id"],
                "score": cov,
                "weak": cov < 0.5,
                "importance": row["importance"],
                "confidence": row["confidence"],
                "date": (row["created_at"] or "")[:10],
                "text": row["text"][:400],
            })
        payload = {"success": True, "count": len(out), "coverage": best, "results": out}
        if best < 0.5:
            payload["note"] = (
                "weak match: the best fact covers less than half of your query terms, "
                "so the memory may simply not know this. Say so instead of guessing."
            )
        return json.dumps(payload, ensure_ascii=False)

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
            return json.dumps({
                "success": False,
                "error": "notes layer is disabled",
                "message": "Temporary notes are off; ask the user to enable them in Carnet > Settings.",
            }, ensure_ascii=False)
        text = _fix_mojibake((args.get("text") or "").strip())
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
        kind = str(args.get("kind") or "fact").strip().lower()
        res = self._store.add_note(text, expires_at, kind=kind, session_id=self._session_id)
        if res.get("error"):
            return json.dumps({"success": False, "error": res["error"]}, ensure_ascii=False)
        return json.dumps({"success": True, "message": "note added", "id": res["id"],
                           "kind": res.get("kind", "fact"),
                           "expires_at": expires_at, "_change": True}, ensure_ascii=False)

    def _tool_note_close(self, args: dict) -> str:
        nid = (args.get("id") or "").strip()
        ok = self._store.close_note(nid)
        return json.dumps({"success": ok, "message": "closed" if ok else "no change",
                           "_change": ok}, ensure_ascii=False)

    # -- misc --------------------------------------------------------
    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "",
                          reset: bool = False, rewound: bool = False, **kwargs) -> None:
        previous = self._session_id
        self._session_id = new_session_id or self._session_id
        if reset or rewound:
            # The conversation was rewound past the block that carried these
            # facts: they are no longer in context, so they must be injectable
            # again. Keeping the journal would silently starve the new branch.
            try:
                if self._store:
                    self._store.forget_injections(self._session_id or previous)
            except Exception as exc:
                log.warning("carnet: could not reset injections after a rewind: %s", exc)
            self._last_prefetch = []
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
