"""Carnet — the watch: duplicates, contradictions, proposals.

The principle: the deterministic part sorts (similarity, topic, predicate), the
small local model writes the proposal, the human decides. No write without
validation.

Three steps, kept separate so they are testable and replayable:

1. ``candidates()``   — suspicious pairs, without a model, without writing.
2. ``propose()``      — one LM Studio call per pair, constrained JSON output.
3. ``apply()``        — accepts a proposal: memory is rewritten, nothing is lost.

No extra process: LM Studio is already running for embeddings.
"""
import hashlib
import importlib.util
import json
import sys
import urllib.request
from array import array
from datetime import datetime, timezone
from operator import mul
from pathlib import Path
from typing import Optional

DEFAULT_URL = "http://127.0.0.1:1234"
DEFAULT_MODEL = "gemma-4-e2b-it-mlx"


def _neighbor(name: str, filename: str):
    """Load a sibling module by path: the gateway imports these files "flat",
    so the plugin folder is not on sys.path."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().with_name(filename))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unreadable module: {filename}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Loading the model is a single responsibility: it lives in rephrase.py,
# and the watch defers to it instead of redefining it its own way.
_rephrase = _neighbor("carnet_rephrase_watch", "rephrase.py")
ModelUnavailable = _rephrase.ModelUnavailable

DUPLICATE_THRESHOLD = 0.93       # above: two phrasings of the same fact
CONTEXT_THRESHOLD = 0.85        # below: same theme, not the same fact
MAX_CANDIDATES = 400            # memory guard: we do not compare 50 M pairs
MAX_CALLS = 25                  # guard: a watch must not last for minutes


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _id(*parts: str) -> str:
    return "p-" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]


def pair_fingerprint(text_a: str, text_b: str) -> str:
    """Signature of the content of both facts, INDEPENDENT of order.

    Without the sort, the same pair presented as (B,A) produced a different
    fingerprint from (A,B): the rejection was forgotten and the proposal came
    back on the next scan.
    """
    first, second = sorted((text_a, text_b))
    return hashlib.sha256(f"{first}\x00{second}".encode()).hexdigest()[:16]


def legacy_fingerprints(text_a: str, text_b: str) -> set:
    """Fingerprints computed before the sort, still present in the database."""
    return {
        hashlib.sha256(f"{text_a}\x00{text_b}".encode()).hexdigest()[:16],
        hashlib.sha256(f"{text_b}\x00{text_a}".encode()).hexdigest()[:16],
    }


def _cos(a: array, b: array) -> float:
    return sum(map(mul, a, b))


# ------------------------------------------------------------------ model calls
def _post(url: str, payload: dict, timeout: float = 60.0, api_key: str = "") -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        _rephrase._chat_url(url),
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as rep:
        return json.loads(rep.read().decode())


def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "json_schema", "json_schema": {
        "name": "proposal", "strict": True,
        "schema": {"type": "object", "properties": properties, "required": required},
    }}


RELATION_SCHEMA = _schema(
    {"relation": {"type": "string", "enum": ["duplicate", "contradiction", "distinct"]},
     "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
     "text": {"type": "string"}, "reason": {"type": "string"}},
    ["relation", "confidence", "text", "reason"],
)

RELATION_PROMPT = (
    "You compare two memories belonging to the same person. Answer in English.\n"
    "Choose ONE relation:\n"
    "- `duplicate`: both say the same thing in different words, without any "
    "incompatible information. Then write in `text` the merge: a self-contained "
    "sentence of fewer than 300 characters, keeping every detail from both, "
    "without inventing anything.\n"
    "- `contradiction`: both assign DIFFERENT values to the same attribute of the "
    "same subject (same database, same language, same version, same address…), even if "
    "both could theoretically coexist. Then write in `text` the version to keep, or "
    "exactly \"undetermined\" if nothing allows a decision. NEVER merge two different "
    "values into a single sentence.\n"
    "- `distinct`: they talk about different things. Leave `text` empty.\n"
    "Careful: two sentences that differ only by a value (Kotlin / Java) are VERY "
    "close in similarity, and that is precisely a contradiction, not a duplicate.\n"
    "Two facts that talk about the same theme but say different things (a tool used "
    "/ a tool installed, a folder / a repository name) are `distinct`: vocabulary "
    "proximity does not make a duplicate.\n"
    "`confidence` describes the relation, not your mood: use `high` when the relation "
    "is visible in the text, `low` only if you really hesitate between two relations.\n"
    "Example: A \"the whiteboard port is 8080\" / B \"the whiteboard listens on 8080\" → "
    "duplicate, high confidence. A \"Resolve 20.3.2 free\" / B \"Resolve 21.1.1 Studio\" → "
    "contradiction, high confidence, text undetermined. A \"the keyboard is wired\" / B \"the "
    "keyboard has a WB32FQ95 controller\" → distinct."
)


def ask(url: str, model: str, system: str, user: str, schema: dict,
        api_key: str = "") -> Optional[dict]:
    try:
        rep = _post(url, {
            "model": model, "temperature": 0,
            "max_tokens": 300,
            "response_format": schema,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }, api_key=api_key)
        content = rep["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception:
        return None


# ------------------------------------------------------------------ candidates
def load_vectors(store) -> dict:
    out = {}
    for r in store.conn.execute("SELECT fact_id, blob FROM vec"):
        out[r["fact_id"]] = array("f", r["blob"])
    return out


def candidates(store, duplicate_threshold: float = DUPLICATE_THRESHOLD) -> dict:
    """Suspicious pairs, without a model. Changes nothing."""
    facts = {r["id"]: dict(r) for r in store.conn.execute(
        "SELECT id, text, topic, predicate, value, importance, confidence FROM facts "
        "WHERE status = 'active'")}
    vecs = load_vectors(store)
    ids = [i for i in facts if i in vecs]

    duplicates = []
    for n, i in enumerate(ids):
        for j in ids[n + 1:]:
            s = _cos(vecs[i], vecs[j])
            if s >= duplicate_threshold:
                duplicates.append((i, j, round(s, 3)))

    # Pairs for the model to decide: above the context threshold, without
    # assuming the relation. A pair that differs only by a value (Kotlin / Java)
    # has the HIGHEST similarity — that is precisely a contradiction, not a duplicate.
    # We only compare within the same topic: two facts on different topics do not
    # contradict each other, and comparing everything against everything is O(n²)
    # (50 million pairs at 10 000 facts, for nothing).
    groups: dict = {}
    for i in ids:
        topic = (facts[i].get("topic") or "").strip().lower()
        if topic:
            groups.setdefault(topic, []).append(i)

    pairs = []
    for members in groups.values():
        for n, i in enumerate(members):
            for j in members[n + 1:]:
                sc = _cos(vecs[i], vecs[j])
                if sc >= CONTEXT_THRESHOLD:
                    pairs.append((i, j, round(sc, 3), True))
    pairs.sort(key=lambda x: x[2], reverse=True)
    return {"facts": facts, "pairs": pairs[:MAX_CANDIDATES],
            "close": [p for p in pairs if p[2] >= duplicate_threshold]}


def already_handled(store, fact_a: str, fact_b: str, fingerprint: str = "",
                    inherited: set = frozenset()) -> bool:
    """True if this pair was already proposed and is pending, or was rejected identically.

    A rejection is final for *these two texts*: rerunning the watch must not ask
    the same question again. If one of the facts is modified, the fingerprint
    changes and the pair becomes a candidate again.
    """
    r = store.conn.execute(
        "SELECT status, fingerprint FROM propositions WHERE "
        "((fact_a=? AND fact_b=?) OR (fact_a=? AND fact_b=?))",
        (fact_a, fact_b, fact_b, fact_a),
    ).fetchall()
    for row in r:
        if row["status"] == "pending":
            return True
        if row["status"] == "rejected":
            stored = row["fingerprint"]
            if not stored:
                return True                      # old rejection, without fingerprint: we honour it
            if stored == fingerprint or stored in inherited:
                return True
    return False


def record(store, kind: str, fact_a: str, fact_b: str, text: str,
           reason: str, score: float, fingerprint: str = "") -> str:
    pid = _id(fact_a, fact_b)
    store.conn.execute(
        "INSERT INTO propositions(id, type, fact_a, fact_b, text, reason, score, status, fingerprint, created_at) "
        "VALUES (?,?,?,?,?,?,?,'pending',?,?) ON CONFLICT(id) DO UPDATE SET "
        "type=excluded.type, text=excluded.text, reason=excluded.reason, "
        "score=excluded.score, fingerprint=excluded.fingerprint, status='pending', updated_at=?",
        (pid, kind, fact_a, fact_b, text, reason, score, fingerprint, _now(), _now()),
    )
    store.conn.commit()
    return pid


def propose(store, url: str = DEFAULT_URL, model: str = DEFAULT_MODEL,
            max_calls: int = MAX_CALLS, api_key: str = "", wait: bool = True) -> dict:
    """Have the model classify close pairs and record the proposals.

    Idempotent: a pair already pending is skipped. One question per pair —
    "duplicate, contradiction or distinct?" — because similarity alone cannot
    tell them apart: two facts that differ only by a value are the closest.
    """
    c = candidates(store)
    facts = c["facts"]
    done, skipped, failures = 0, 0, 0

    # Seen pairs are filtered out first, then the model is loaded ONCE before any
    # question: LM Studio loads JIT, and querying a still-cold model gives degraded
    # answers — or twenty-five failures in a row.
    to_ask = []
    for i, j, score, _same_topic in c["pairs"][:max_calls]:
        ta, tb = facts[i]["text"], facts[j]["text"]
        emp = pair_fingerprint(ta, tb)
        if already_handled(store, i, j, emp, legacy_fingerprints(ta, tb)):
            skipped += 1
            continue
        to_ask.append((i, j, score, emp))

    if to_ask and wait:
        try:
            _rephrase.ensure_loaded(url, model)
        except ModelUnavailable as exc:
            # Nothing is written, nothing is dated: the watch remains to be done.
            return {"proposals": 0, "skipped": skipped, "failures": 0,
                    "candidates": len(c["pairs"]), "error": str(exc)}

    for i, j, score, emp in to_ask:
        rep = ask(url, model, RELATION_PROMPT,
                  f"Fact A: {facts[i]['text']}\nFact B: {facts[j]['text']}",
                  RELATION_SCHEMA, api_key)
        if not rep:
            failures += 1
            continue
        relation = rep.get("relation")
        if relation not in ("duplicate", "contradiction"):
            skipped += 1
            continue
        if rep.get("confidence") == "low" and relation == "duplicate":
            # Merging on a doubt damages a fact; proposing a doubtful contradiction
            # costs nothing, since the human decides.
            skipped += 1
            continue
        text = (rep.get("text") or "").strip()
        reason = (rep.get("reason") or "").strip()
        if text.lower().startswith("undetermined"):
            # "undetermined (because…)" : the explanation is not kept in the
            # field meant for the value to preserve.
            rest = text[len("undetermined"):].strip(" ()")
            if rest:
                reason = (reason + " " + rest).strip()
            text = ""
        if relation == "duplicate" and not text:
            failures += 1
            continue
        record(store, relation, i, j, text, reason, score, emp)
        done += 1

    store.meta_set("last_watch", _now())
    return {"proposals": done, "skipped": skipped, "failures": failures,
            "candidates": len(c["pairs"])}


# ------------------------------------------------------------------ applying
def _status(row) -> str:
    """sqlite3.Row has no .get(): read the column, default to active."""
    try:
        return str(row["status"] or "active")
    except (IndexError, KeyError):
        return "active"

def apply(store, pid: str, keep: str = "a") -> dict:
    p = store.conn.execute("SELECT * FROM propositions WHERE id=?", (pid,)).fetchone()
    if not p:
        return {"ok": False, "error": "unknown proposal"}
    if p["status"] != "pending":
        return {"ok": False, "error": f"already {p['status']}"}

    a = store.fact(p["fact_a"])
    b = store.fact(p["fact_b"]) if p["fact_b"] else None
    if not a or not b:
        return {"ok": False, "error": "original fact disappeared"}
    # A fact that was removed since the proposal was written must not be
    # "merged": replace_fact refuses an inactive source, and removing the other
    # one anyway would empty the memory and report success.
    if _status(a) != "active":
        return {"ok": False, "error": "the kept fact is no longer active"}
    if _status(b) != "active":
        return {"ok": False, "error": "the other fact is no longer active"}

    if p["type"] == "duplicate":
        if not p["text"]:
            return {"ok": False, "error": "empty proposal"}
        new = store.replace_fact(
            a["id"], p["text"],
            importance=min(int(a["importance"]), int(b["importance"])),
            confidence=a["confidence"], source="watch",
        )
        if new.get("error") or not new.get("id"):
            return {"ok": False, "error": str(new.get("error") or "the merge produced no fact")}
        store.remove_fact(b["id"], f"merged into {new['id']}")
        store.conn.execute("UPDATE facts SET replaced_by=? WHERE id=?", (new["id"], b["id"]))
        store.conn.commit()
        result = {"ok": True, "fact": new["id"]}
    else:
        # Contradiction: this is a human choice. By default A is kept, but the
        # route accepts `keep=b` when the other version is the right one.
        kept, removed = (a, b) if keep != "b" else (b, a)
        store.remove_fact(removed["id"], p["reason"] or "contradiction resolved")
        store.conn.execute("UPDATE facts SET replaced_by=? WHERE id=?", (kept["id"], removed["id"]))
        result = {"ok": True, "fact": kept["id"]}

    store.conn.execute("UPDATE propositions SET status='accepted', updated_at=? WHERE id=?", (_now(), pid))
    store.conn.commit()
    store.log("watch", f"apply_{p['type']}", a["id"], {"proposal": pid})
    return result


def reject(store, pid: str) -> dict:
    store.conn.execute("UPDATE propositions SET status='rejected', updated_at=? WHERE id=?", (_now(), pid))
    store.conn.commit()
    return {"ok": True}


def regenerate(store, pid: str, url: str = DEFAULT_URL, model: str = DEFAULT_MODEL,
               api_key: str = "", wait: bool = True) -> dict:
    """Run the model again on the same pair (a failed merge can be retried)."""
    p = store.conn.execute("SELECT * FROM propositions WHERE id=?", (pid,)).fetchone()
    if not p:
        return {"ok": False, "error": "unknown proposal"}
    a, b = store.fact(p["fact_a"]), store.fact(p["fact_b"])
    if not a or not b:
        return {"ok": False, "error": "original fact disappeared"}
    if wait:
        try:
            _rephrase.ensure_loaded(url, model)
        except ModelUnavailable as exc:
            return {"ok": False, "error": str(exc)}
    rep = ask(url, model, RELATION_PROMPT,
              f"Fact A: {a['text']}\nFact B: {b['text']}", RELATION_SCHEMA, api_key)
    if not rep:
        return {"ok": False, "error": "the model returned nothing"}
    relation = rep.get("relation")
    if relation not in ("duplicate", "contradiction"):
        return {"ok": False, "error": f"the model now says: {relation}"}
    text = (rep.get("text") or "").strip()
    store.conn.execute("UPDATE propositions SET type=?, text=?, reason=?, updated_at=? WHERE id=?",
                       (relation, text, rep.get("reason", ""), _now(), pid))
    store.conn.commit()
    return {"ok": True, "relation": relation, "text": text}


def list_proposals(store, status: str = "pending") -> list[dict]:
    rows = store.conn.execute(
        "SELECT p.*, a.text AS text_a, b.text AS text_b FROM propositions p "
        "LEFT JOIN facts a ON a.id = p.fact_a LEFT JOIN facts b ON b.id = p.fact_b "
        "WHERE p.status = ? ORDER BY p.score DESC, p.created_at DESC", (status,)
    ).fetchall()
    return [dict(r) for r in rows]
