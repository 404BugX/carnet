"""Carnet — dashboard backend.

Exposed on ``/api/plugins/carnet/`` by the dashboard plugin mount.
The interface (``~/.hermes/desktop-plugins/carnet/plugin.js``) calls these routes
through ``ctx.rest('/…')``.

Why a backend: the renderer cannot read the SQLite database, and editing must go
through the single mutation point (``store.py``).

The module is loaded "flat" by the gateway (the ``dashboard/`` folder is not on
``sys.path``): ``store.py`` is loaded by path, never through ``import store``.
"""
import hashlib
import importlib.util
import json
import math
import os
import re
import sqlite3
import sys
import time
from array import array
from operator import mul
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# ------------------------------------------------------------------ loading
_ROOT = Path(__file__).resolve().parent.parent      # …/plugins/carnet


def _hermes_home() -> Path:
    """Resolve the Hermes home on every call.

    This module is loaded flat by the gateway, so it cannot import the package's
    own helpers; and the gateway can switch profile while it runs, so a constant
    resolved at import time would freeze the first home and serve another
    profile's folder.
    """
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


SECRET_KEYS = ("embed_api_key", "model_api_key")


def _secret(name: str) -> str:
    """Read a credential from the agent's secret scope.

    Keys never live in ``config/carnet.json``: that file is plaintext, world
    readable, and copied with the profile. The gateway resolves the ones declared
    in ``config_schema`` from the profile environment.
    """
    try:
        from agent.secret_scope import get_secret

        return str(get_secret(name, "") or "")
    except Exception:
        return ""


def _load(name: str, file: Path):
    """Load a sibling module by path: the gateway imports this file "flat",
    so `import store` would fail (folder absent from sys.path) and pollute sys.modules."""
    spec = importlib.util.spec_from_file_location(name, file)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unreadable module: {file}")
    mod = importlib.util.module_from_spec(spec)
    # Register BEFORE executing: pydantic resolves deferred annotations
    # (``from __future__ import annotations``) through sys.modules, and fails otherwise.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


store = _load("carnet_store", _ROOT / "store.py")
loc_mod = _load("carnet_loc", _ROOT / "loc.py")
localmodel = _load("carnet_localmodel", _ROOT / "localmodel.py")
rephrase_mod = _load("carnet_rephrase", _ROOT / "rephrase.py")


def _watch():
    """The watch, reloaded as soon as the file changes on disk.

    The backend runs in the app process, which lives for days: without this
    check, a fix to watch.py stays invisible until someone kills the process.
    One `stat` per scan costs nothing, an app restart costs a session.
    """
    file = _ROOT / "watch.py"
    mtime = file.stat().st_mtime
    if getattr(_watch, "mtime", None) != mtime:
        _watch.mod = _load("carnet_watch", file)
        _watch.mtime = mtime
    return _watch.mod

def _rephrase():
    """The rephraser, reloaded as soon as rephrase.py changes on disk."""
    file = _ROOT / "rephrase.py"
    mtime = file.stat().st_mtime
    if getattr(_rephrase, "mtime", None) != mtime:
        _rephrase.mod = _load("carnet_rephrase", file)
        _rephrase.mtime = mtime
    return _rephrase.mod


# Import trace: proof that the gateway mounted this backend (diagnostic).
try:
    _trace = _hermes_home() / "logs/carnet-api-import.log"
    with open(_trace, "a", encoding="utf-8") as _f:
        _f.write(f"{__import__('datetime').datetime.now().isoformat()} imported from {_ROOT}\n")
except Exception:
    pass

router = APIRouter()


@router.get("/strings")
def strings() -> dict:
    """The key tree of the active language: the dashboard renders from it."""
    return loc_mod.tree()


# ------------------------------------------------------------------ settings
class LanguageSetting(BaseModel):
    language: str


def _language_payload() -> dict:
    return {
        "language": loc_mod.language(),
        "source": loc_mod.source(),
        "languages": loc_mod.available(),
    }


@router.get("/settings")
def settings() -> dict:
    """Effective language, model mode and whether temporary notes are on."""
    payload = _language_payload()
    payload["model_mode"] = _model_values()["mode"]
    payload["notes_enabled"] = bool(_global_config().get("notes_enabled", False))
    payload["onboarding_done"] = bool(_global_config().get("onboarding_done", False))
    return payload


class OnboardingSetting(BaseModel):
    done: Optional[bool] = None


@router.post("/settings/onboarding")
def set_onboarding(setting: OnboardingSetting) -> dict:
    """Remember that the guided setup was completed (or dismissed)."""
    values = {"onboarding_done": bool(setting.done)}
    _write_global_config(values)
    return {"done": values["onboarding_done"]}


@router.post("/settings/language")
def set_language(setting: LanguageSetting) -> dict:
    try:
        loc_mod.set_language(setting.language)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _language_payload()


# ------------------------------------------------------------------ about
def _manifest() -> dict:
    """Name, version and languages from plugin.yaml — no YAML parser needed."""
    fields: dict[str, str] = {}
    languages: list[str] = []
    in_languages = False
    for line in (_ROOT / "plugin.yaml").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^(name|version|author|repository):\s*(.+)$", line)
        if m:
            fields[m.group(1)] = m.group(2).strip().strip("\"'")
            in_languages = False
            continue
        if re.match(r"^  languages:\s*$", line):
            in_languages = True
            continue
        if in_languages:
            m = re.match(r"^\s+-\s*(\S+)\s*$", line)
            if m:
                languages.append(m.group(1))
            elif line.strip() and not line.startswith("    "):
                in_languages = False
    return {
        "name": fields.get("name", ""),
        "version": fields.get("version", ""),
        "license": "MIT",
        "author": fields.get("author", ""),
        "repository": fields.get("repository", ""),
        "languages": languages,
    }


@router.get("/about")
def about() -> dict:
    return _manifest()


# ------------------------------------------------------------------ embedding
EMBED_MODES = {"disabled", "lmstudio", "custom", "local"}
EMBED_DEFAULT_URL = store.DEFAULTS["embed_url"]
EMBED_DEFAULT_MODEL = store.DEFAULTS["embed_model"]


def _runtime_config() -> dict:
    """The plugin runtime config, merged over the defaults."""
    cfg = dict(store.DEFAULTS)
    path = _folder() / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in store.DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def _write_runtime_config(values: dict) -> None:
    path = _folder() / "config.json"
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    values = {k: v for k, v in values.items() if k not in SECRET_KEYS}
    for key in SECRET_KEYS:            # scrub what an older version wrote here
        data.pop(key, None)
    data.update(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Keep the file readable by its owner only: it holds endpoint settings.
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _clean_setting(value) -> str:
    """Drop the "[object Object]" a bad input binding may have saved.

    Older builds stored a change event instead of its text; those values are
    unusable and must not be shown as if the user had typed them.
    """
    text = str(value or "").strip()
    return "" if "[object Object]" in text else text


def _embedding_mode(cfg: dict) -> str:
    backend = str(cfg.get("embed_backend") or "lmstudio")
    if backend == "sidecar":
        return "local"
    return backend if backend in ("disabled", "custom") else "lmstudio"


def _embedding_payload() -> dict:
    cfg = _runtime_config()
    return {
        "mode": _embedding_mode(cfg),
        "url": _clean_setting(cfg.get("embed_url")),
        "model": _clean_setting(cfg.get("embed_model")),
        "api_key_set": bool(_secret("CARNET_EMBED_API_KEY").strip()),
        "local": localmodel.status(_hermes_home()),
    }


class EmbeddingSetting(BaseModel):
    mode: str
    url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None


class DownloadConsent(BaseModel):
    consent: bool = False


@router.get("/settings/embedding")
def embedding_settings() -> dict:
    """Mode, endpoint fields and the local model state. Never returns the key."""
    return _embedding_payload()


@router.post("/settings/embedding")
def set_embedding(setting: EmbeddingSetting) -> dict:
    mode = (setting.mode or "").strip().lower()
    if mode not in EMBED_MODES:
        raise HTTPException(400, loc_mod.translate("errors.embedding_mode"))
    values: dict = {}
    if mode == "disabled":
        values["embed_backend"] = "disabled"
    elif mode == "lmstudio":
        values.update(
            embed_backend="lmstudio",
            embed_url=(setting.url or "").strip() or EMBED_DEFAULT_URL,
            embed_model=(setting.model or "").strip() or EMBED_DEFAULT_MODEL,
        )
    elif mode == "custom":
        url = _clean_setting(setting.url)
        model = _clean_setting(setting.model)
        if not url.startswith(("http://", "https://")):
            raise HTTPException(400, loc_mod.translate("errors.embedding_url"))
        if not model:
            raise HTTPException(400, loc_mod.translate("errors.embedding_model"))
        values.update(embed_backend="custom", embed_url=url, embed_model=model)
    else:
        values["embed_backend"] = "sidecar"
    if (setting.api_key or "").strip():
        raise HTTPException(400, "set CARNET_EMBED_API_KEY in the profile environment; "
                                 "keys are not stored by the plugin")
    _write_runtime_config(values)
    return _embedding_payload()


@router.post("/settings/embedding/download")
def download_embedding(consent: DownloadConsent) -> dict:
    if not consent.consent:
        raise HTTPException(400, loc_mod.translate("errors.embedding_consent"))
    return localmodel.start_download(_hermes_home())


# ------------------------------------------------------------------ memory settings
MEMORY_STEPS = {
    key: (0.01 if key in getattr(store, "FLOAT_KEYS", {"abstention_threshold"}) else 1)
    for key in store.MEMORY_KEYS
}


def _scrub_secrets() -> None:
    """Remove credentials an older version wrote into the shared config file.

    Called from the two settings payloads: the file is read there anyway, and
    rewriting it only when it actually holds a key keeps this side-effect free
    in the normal case.
    """
    for path in (loc_mod.config_path(), _folder() / "config.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not any(k in data for k in SECRET_KEYS):
            continue
        for key in SECRET_KEYS:
            data.pop(key, None)
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        except OSError:
            pass


def _global_config() -> dict:
    """The shared plugin config: $HERMES_HOME/config/carnet.json."""
    path = loc_mod.config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_global_config(values: dict, remove: tuple = ()) -> None:
    path = loc_mod.config_path()
    data = _global_config()
    values = {k: v for k, v in values.items() if k not in SECRET_KEYS}
    for key in SECRET_KEYS:          # scrub what an older version wrote
        data.pop(key, None)
    data.update(values)
    for key in remove:
        data.pop(key, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _memory_values() -> dict:
    cfg = dict(store.DEFAULTS)
    saved = _global_config()
    for key in store.MEMORY_KEYS:
        if key in saved:
            cfg[key] = saved[key]
    return store.normalize_settings(cfg)


def _memory_payload() -> dict:
    values = _memory_values()
    fields = []
    for key in store.MEMORY_KEYS:
        low, high = store.SETTING_BOUNDS[key]
        fields.append({
            "key": key,
            "value": values[key],
            "default": store.DEFAULTS[key],
            "min": low,
            "max": high,
            "step": MEMORY_STEPS[key],
        })
    return {"fields": fields, "path": str(loc_mod.config_path())}


class MemorySetting(BaseModel):
    values: dict[str, Any] = {}


@router.get("/settings/memory")
def memory_settings() -> dict:
    return _memory_payload()


@router.post("/settings/memory")
def set_memory(setting: MemorySetting) -> dict:
    values: dict = {}
    for key, raw in (setting.values or {}).items():
        if key not in store.SETTING_BOUNDS:
            continue
        low, high = store.SETTING_BOUNDS[key]
        try:
            number = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(400, loc_mod.translate("errors.setting_number", field=key))
        if not low <= number <= high:
            raise HTTPException(400, loc_mod.translate(
                "errors.setting_range", field=key, min=low, max=high))
        # Every float setting keeps its decimals: rounding them to integers is
        # how a documented 0.34 floor became 0.0 and silently disabled the
        # "nothing relevant" guarantee.
        if key in getattr(store, "FLOAT_KEYS", {"abstention_threshold"}):
            values[key] = round(number, 3)
        else:
            values[key] = int(round(number))
    if not values:
        raise HTTPException(400, loc_mod.translate("errors.setting_empty"))
    _write_global_config(values)
    return _memory_payload()


@router.post("/settings/memory/reset")
def reset_memory() -> dict:
    _write_global_config({}, remove=store.MEMORY_KEYS)
    return _memory_payload()


# ------------------------------------------------------------------ status bar
# ------------------------------------------------------------------ model settings
MODEL_MODES = {"off", "lmstudio", "custom"}


def _model_values() -> dict:
    data = _global_config()
    mode = str(data.get("model_mode") or "off").strip().lower()
    return {
        "mode": mode if mode in MODEL_MODES else "off",
        "url": str(data.get("model_url") or ""),
        "model": str(data.get("model_name") or ""),
        "api_key": _secret("CARNET_MODEL_API_KEY"),
    }


def _model_payload() -> dict:
    cfg = _model_values()
    return {
        "mode": cfg["mode"],
        "url": _clean_setting(cfg["url"]),
        "model": _clean_setting(cfg["model"]),
        "api_key_set": bool(cfg["api_key"].strip()),
    }


class ModelSetting(BaseModel):
    mode: str
    url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None


@router.post("/settings/model/auto")
def auto_model() -> dict:
    """Point the rewrite model at whatever the local server is already serving.

    The guided setup must not ask for a model name: the user who runs LM Studio
    already has one loaded, and everyone else has nothing to type.
    """
    cfg = _model_values()
    url = cfg["url"] or str(store.DEFAULTS.get("model_url") or "http://127.0.0.1:1234")
    try:
        data = rephrase_mod._get_json(url, "/api/v0/models", 3.0)
    except Exception:
        raise HTTPException(400, loc_mod.translate("errors.model_server_unreachable"))
    picked = ""
    for entry in data.get("data") or []:
        kind = str(entry.get("type") or "").lower()
        if kind in ("embeddings", "embedding"):
            continue
        picked = str(entry.get("id") or "")
        if picked:
            break
    if not picked:
        raise HTTPException(400, loc_mod.translate("errors.model_none_loaded"))
    values = {"model_mode": "lmstudio", "model_url": url, "model_name": picked}
    _write_global_config(values)
    return _model_payload()


@router.get("/settings/model")
def model_settings() -> dict:
    return _model_payload()


@router.post("/model/verify")
def model_verify() -> dict:
    """Hash the local model on demand: sizes alone prove nothing."""
    return localmodel.verify(_hermes_home())


@router.post("/settings/model")
def set_model(setting: ModelSetting) -> dict:
    mode = (setting.mode or "").strip().lower()
    if mode not in MODEL_MODES:
        raise HTTPException(400, loc_mod.translate("errors.model_mode"))
    values: dict = {"model_mode": mode}
    if mode in ("custom", "lmstudio"):
        url = _clean_setting(setting.url)
        model = _clean_setting(setting.model)
        if mode == "lmstudio" and not url:
            url = str(store.DEFAULTS["embed_url"])
        if not url.startswith(("http://", "https://")):
            raise HTTPException(400, loc_mod.translate("errors.model_url"))
        if not model:
            raise HTTPException(400, loc_mod.translate("errors.embedding_model"))
        values.update(model_url=url, model_name=model)
    if (setting.api_key or "").strip():
        raise HTTPException(400, "set CARNET_MODEL_API_KEY in the profile environment; "
                                 "keys are not stored by the plugin")
    _write_global_config(values)
    return _model_payload()


# ------------------------------------------------------------------ notes settings
NOTE_STEP = {key: 1 for key in store.NOTE_KEYS}


def _notes_values() -> dict:
    cfg = dict(store.DEFAULTS)
    saved = _global_config()
    for key in ("notes_enabled", *store.NOTE_KEYS):
        if key in saved:
            cfg[key] = saved[key]
    return store.normalize_settings(cfg)


def _notes_payload() -> dict:
    values = _notes_values()
    fields = []
    for key in store.NOTE_KEYS:
        low, high = store.NOTE_BOUNDS[key]
        fields.append({
            "key": key,
            "value": values[key],
            "default": store.DEFAULTS[key],
            "min": low,
            "max": high,
            "step": NOTE_STEP[key],
        })
    return {"enabled": values["notes_enabled"], "fields": fields,
            "path": str(loc_mod.config_path())}


class NotesSetting(BaseModel):
    enabled: Optional[bool] = None
    max_notes: Optional[int] = None
    expiry_days: Optional[int] = None
    note_char_limit: Optional[int] = None


@router.get("/settings/notes")
def notes_settings() -> dict:
    return _notes_payload()


@router.post("/settings/notes")
def set_notes(setting: NotesSetting) -> dict:
    values: dict = {}
    if setting.enabled is not None:
        values["notes_enabled"] = bool(setting.enabled)
    for key in store.NOTE_KEYS:
        raw = getattr(setting, key, None)
        if raw is None:
            continue
        low, high = store.NOTE_BOUNDS[key]
        try:
            number = int(float(raw))
        except (TypeError, ValueError):
            raise HTTPException(400, loc_mod.translate("errors.setting_number", field=key))
        if not low <= number <= high:
            raise HTTPException(400, loc_mod.translate(
                "errors.setting_range", field=key, min=low, max=high))
        values[key] = number
    if not values:
        raise HTTPException(400, loc_mod.translate("errors.setting_empty"))
    _write_global_config(values)
    return _notes_payload()


# ------------------------------------------------------------------ notes
@router.get("/notes")
def notes() -> dict:
    """Active, non-expired notes, oldest first."""
    c = _base()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    out = []
    for row in c.active_notes(limit=500, now=now.isoformat()):
        try:
            created = datetime.fromisoformat(row["created_at"])
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = max(0, int((now - created).total_seconds() // 86400))
        except (TypeError, ValueError):
            age = 0
        out.append({
            "id": row["id"], "text": row["text"], "created_at": row["created_at"],
            "expires_at": row["expires_at"], "age_days": age, "status": row["status"],
        })
    return {"notes": out}


@router.post("/notes/{nid}/delete")
def delete_note(nid: str) -> dict:
    if not _base().delete_note(nid):
        raise HTTPException(404, loc_mod.translate("errors.note_not_found"))
    return {"ok": True}


# Fields editable from the dashboard. Everything else is derived (hash, size, dates).
# Everything the table stores outside derived columns: `text_hash` and `size`
# are recomputed from `text`, `updated_at` is set on every write, `id` is
# the key. The rest is editable — including provenance and validity bounds.
EDITABLE_FIELDS = {"text", "topic", "keywords", "predicate", "value", "importance",
                   "confidence", "status", "evidence", "evidence_missing", "source",
                   "session_id", "valid_from", "valid_until", "replaced_by", "created_at"}

CONFIDENCE_VALUES = {"certain", "probable"}
STATUS_VALUES = {"active", "replaced", "removed"}


def _folder() -> Path:
    """The database folder of the profile being served, and no other.

    There used to be a fallback that borrowed the only database found under
    ``profiles/*``. That is worse than an empty page: a profile then displays
    another agent's memories as if they were its own, which is exactly the kind
    of mistake a memory must never make. Each profile reads its own home.
    """
    return _hermes_home() / "carnet"


def _base():
    folder = _folder()
    if not (folder / "carnet.db").exists():
        raise HTTPException(404, loc_mod.translate("errors.database_not_found", path=folder / "carnet.db"))
    return store.Carnet(folder)


def _line(r: sqlite3.Row) -> dict:
    d = dict(r)
    d.pop("blob", None)
    return d


SORTS = {
    # Chronological sorting does NOT group by layer: "Newest" must answer a
    # single question, otherwise core facts squat the top of the list and a
    # fact from yesterday ends up behind a rule from September.
    "recent": "created_at DESC, id DESC",
    "oldest": "created_at ASC, id ASC",
    "importance": "importance ASC, text COLLATE NOCASE ASC",
    "topic": "ifnull(nullif(topic,''),'~') ASC, text COLLATE NOCASE ASC",
    "text": "text COLLATE NOCASE ASC",
    "uses": "coalesce(uses,0) DESC, ifnull(last_used_at,'') DESC",
}


@router.get("/facts")
def facts(q: str = "", status: str = "active", importance: str = "",
          sort: str = "recent", limit: int = 100, offset: int = 0) -> dict:
    c = _base()
    where, args = [], []
    if status:
        where.append("status = ?")
        args.append(status)
    if importance != "":
        try:
            args.append(int(importance))
        except (TypeError, ValueError):
            raise HTTPException(400, loc_mod.translate("errors.bad_importance"))
        where.append("importance = ?")
    query = q.strip()
    if query and re.fullmatch(r"f-[0-9a-f]{2,8}", query.lower()):
        # An identifier is not text: it is looked up as is, prefix accepted.
        where.append("id LIKE ?")
        args.append(query.lower() + "%")
    elif query:
        where.append("(text LIKE ? OR ifnull(topic,'') LIKE ? OR ifnull(keywords,'') LIKE ?)")
        pattern = f"%{query}%"
        args += [pattern, pattern, pattern]
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = c.conn.execute(f"SELECT count(*) n FROM facts {clause}", args).fetchone()["n"]
    rows = c.conn.execute(
        f"SELECT id, text, topic, keywords, predicate, value, importance, confidence, status,"
        f" source, evidence, evidence_missing, session_id, created_at, updated_at, valid_from, valid_until,"
        f" replaced_by, text_hash, size,"
        f" coalesce(uses,0) AS uses, last_used_at"
        f" FROM facts {clause} ORDER BY {SORTS.get(sort, SORTS['recent'])} LIMIT ? OFFSET ?",
        args + [limit, offset],
    ).fetchall()
    return {"total": total, "facts": [_line(r) for r in rows]}


@router.get("/facts/{fid}")
def fact(fid: str) -> dict:
    c = _base()
    r = c.fact(fid)
    if not r:
        raise HTTPException(404, loc_mod.translate("errors.fact_not_found"))
    return _line(r)


class Edit(BaseModel):
    text: Optional[str] = None
    topic: Optional[str] = None
    keywords: Optional[str] = None
    predicate: Optional[str] = None
    value: Optional[str] = None
    importance: Optional[int] = None
    confidence: Optional[str] = None
    status: Optional[str] = None
    evidence: Optional[str] = None
    source: Optional[str] = None
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    evidence_missing: Optional[int] = None
    session_id: Optional[str] = None
    replaced_by: Optional[str] = None
    created_at: Optional[str] = None


@router.patch("/facts/{fid}")
def edit(fid: str, e: Edit) -> dict:
    c = _base()
    r = c.fact(fid)
    if not r:
        raise HTTPException(404, loc_mod.translate("errors.fact_not_found"))
    fields = {k: v for k, v in e.model_dump().items() if v is not None and k in EDITABLE_FIELDS}
    if not fields:
        raise HTTPException(400, loc_mod.translate("errors.no_editable_field"))
    if "importance" in fields and fields["importance"] not in (0, 1, 2):
        raise HTTPException(400, loc_mod.translate("errors.bad_importance"))
    if "confidence" in fields and fields["confidence"] not in CONFIDENCE_VALUES:
        raise HTTPException(400, loc_mod.translate("errors.bad_confidence"))
    if "status" in fields and fields["status"] not in STATUS_VALUES:
        raise HTTPException(400, loc_mod.translate("errors.bad_status"))
    if "evidence_missing" in fields and int(fields["evidence_missing"]) not in (0, 1):
        raise HTTPException(400, loc_mod.translate("errors.bad_evidence_missing"))
    if "evidence_missing" in fields:
        fields["evidence_missing"] = int(fields["evidence_missing"])
    if "text" in fields:
        fields["text"] = fields["text"].strip()
        if not fields["text"]:
            raise HTTPException(400, loc_mod.translate("errors.empty_text"))
        # The same layer limit the agent gets: a fact edited from the page must
        # not be able to store 800 characters that the prompt will truncate.
        layer = int(fields.get("importance", r["importance"]))
        if layer < 2:
            key = "seat_char_limit" if layer == 0 else "fact_char_limit"
            limit = int(_memory_values()[key])
            if len(fields["text"]) > limit:
                raise HTTPException(400, loc_mod.translate(
                    "errors.text_too_long", limit=limit))
        fields["text_hash"] = store.fingerprint(fields["text"])
        fields["size"] = len(fields["text"])
    wants_seat = fields.get("importance") == 0 and int(r["importance"]) != 0
    if wants_seat:
        # A seat is granted by promote(), never by a column update: otherwise the
        # page can create a 26th core fact that the prompt will never show.
        fields.pop("importance")
    fields["updated_at"] = store._now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    # One transaction for the edit and the promotion: a refused seat must leave
    # nothing behind, otherwise a 409 is a lie about a text that did change.
    c.conn.execute("BEGIN IMMEDIATE")
    try:
        c.conn.execute(f"UPDATE facts SET {sets} WHERE id = ?", list(fields.values()) + [fid])
        if wants_seat:
            seat = c.promote(fid, limit=int(_memory_values()["seat_limit"]))
            if not seat.get("seat"):
                c.conn.execute("ROLLBACK")
                raise HTTPException(409, loc_mod.translate(
                    "errors.seats_full", free=seat.get("free", 0)))
        c.conn.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        # facts_hash is UNIQUE (text_hash, valid_from): returning a duplicate
        # must read clearly, not surface as a 500.
        c.conn.execute("ROLLBACK")
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(409, loc_mod.translate("errors.duplicate_fact"))
        raise HTTPException(400, loc_mod.translate("errors.write_refused", error=exc))
    except HTTPException:
        raise
    except Exception as exc:
        c.conn.execute("ROLLBACK")
        raise HTTPException(400, loc_mod.translate("errors.write_refused", error=exc))
    # The text changed: the vector is worthless. Removing it is enough — the
    # plugin worker reindexes everything that has no vector.
    if "text" in fields:
        c.conn.execute("DELETE FROM vec WHERE fact_id = ?", (fid,))
        c.conn.commit()
    # The full-text index keeps its own copy: without this rewrite, an edited
    # fact would still be recalled on its old text (and unfindable on the new one).
    if {"text", "topic", "keywords"} & set(fields):
        row = c.fact(fid)
        c.conn.execute("DELETE FROM facts_fts WHERE fact_id = ?", (fid,))
        c.conn.execute(
            "INSERT INTO facts_fts(fact_id, text, topic, keywords) VALUES (?,?,?,?)",
            (fid, store.normalize(row["text"]), store.normalize(row["topic"] or ""),
             store.normalize(row["keywords"] or "")),
        )
        c.conn.commit()
    c.log("dashboard", "edit", fid, {"fields": list(fields)})
    return {"ok": True, "modified": sorted(fields)}


def _text_limit(c, importance: int) -> Optional[int]:
    """The size the layer imposes on the text: 250 for a core seat, 500 for a
    fact, none for a sheet."""
    if importance >= 2:
        return None
    key = "seat_char_limit" if importance == 0 else "fact_char_limit"
    return int(_memory_values()[key])


@router.post("/facts/{fid}/rephrase")
def rephrase_fact(fid: str) -> dict:
    """Propose a clean rewrite of the fact. WRITES NOTHING: the human validates."""
    c = _base()
    r = c.fact(fid)
    if not r:
        raise HTTPException(404, loc_mod.translate("errors.fact_not_found"))
    cfg = _model_values()
    if cfg["mode"] not in ("custom", "lmstudio"):
        return {"id": fid, "disabled": True, "reason": "model_off"}
    mod = _rephrase()
    chosen = cfg["model"]
    limit = _text_limit(c, int(r["importance"] or 0))
    try:
        proposal = mod.rephrase(r["text"], url=cfg["url"], model=chosen,
                                api_key=cfg["api_key"], limit=limit, wait=False)
    except getattr(mod, "ModelUnavailable", RuntimeError) as exc:
        raise HTTPException(503, str(exc))
    except Exception as exc:
        raise HTTPException(503, loc_mod.translate("errors.model_unreachable", model=chosen, error=exc))
    if not proposal:
        raise HTTPException(502, loc_mod.translate("errors.model_empty"))
    c.log("dashboard", "rephrase", fid, {"model": chosen, "limit": limit})
    return {
        "id": fid,
        "original": r["text"],
        "text": proposal,
        "model": chosen,
        "limit": limit,
        "length": len(proposal),
        "over": bool(limit and len(proposal) > limit),
    }


@router.post("/facts/{fid}/promote")
def promote(fid: str) -> dict:
    c = _base()
    if not c.fact(fid):
        raise HTTPException(404, loc_mod.translate("errors.fact_not_found"))
    res = c.promote(fid, limit=int(_memory_values()["seat_limit"]))
    c.log("dashboard", "promotion", fid, res)
    return res


@router.post("/facts/{fid}/remove")
def remove(fid: str, reason: str = "") -> dict:
    c = _base()
    ok = c.remove_fact(fid, reason)
    if not ok:
        raise HTTPException(404, loc_mod.translate("errors.fact_gone"))
    c.log("dashboard", "removal", fid, {"reason": reason})
    return {"ok": True}


@router.get("/core")
def core() -> dict:
    c = _base()
    limit = int(_memory_values()["seat_limit"])
    rows = c.core(limit)
    return {"limit": limit, "free_seats": c.free_seats(limit),
            "seats": [_line(r) for r in rows]}


@router.get("/graphs")
def graphs() -> dict:
    """Series for the evolution charts: the corpus, recalls, topics."""
    c = _base()

    created = {r["j"]: r["n"] for r in c.conn.execute(
        "SELECT substr(created_at,1,10) j, count(*) n FROM facts WHERE status='active' GROUP BY j")}
    recalls = {r["j"]: r["n"] for r in c.conn.execute(
        "SELECT substr(ts,1,10) j, count(*) n FROM injected GROUP BY j")}
    days = sorted(set(created) | set(recalls))

    cumulative, series = 0, []
    for j in days:
        n = created.get(j, 0)
        cumulative += n
        series.append({"day": j, "created": n, "recalls": recalls.get(j, 0), "cumulative": cumulative})

    layers = {str(r["importance"]): r["n"] for r in c.conn.execute(
        "SELECT importance, count(*) n FROM facts WHERE status='active' GROUP BY importance")}
    confidence = {r["confidence"]: r["n"] for r in c.conn.execute(
        "SELECT confidence, count(*) n FROM facts WHERE status='active' GROUP BY confidence")}
    no_topic = loc_mod.translate("dashboard.stats.no_topic")
    topics = [{"topic": r["s"] or no_topic, "n": r["n"]} for r in c.conn.execute(
        "SELECT lower(ifnull(nullif(trim(topic),''), ?)) s, count(*) n"
        " FROM facts WHERE status='active' GROUP BY s ORDER BY n DESC LIMIT 10", (no_topic,))]
    sources = [{"source": r["s"], "n": r["n"]} for r in c.conn.execute(
        "SELECT source s, count(*) n FROM facts WHERE status='active' GROUP BY s ORDER BY n DESC LIMIT 6")]
    used = c.conn.execute(
        "SELECT count(*) n FROM facts WHERE status='active' AND coalesce(uses,0)>0").fetchone()["n"]
    never = c.conn.execute(
        "SELECT count(*) n FROM facts WHERE status='active' AND coalesce(uses,0)=0").fetchone()["n"]

    # Length distribution: an overlong fact is not reread, an over-short fact
    # says nothing. The histogram shows where the corpus sits.
    sizes = [0] * 6
    for r in c.conn.execute("SELECT length(text) n FROM facts WHERE status='active'"):
        n = r["n"] or 0
        sizes[min(5, n // 100)] += 1

    # Most frequent keywords: what memory is actually indexed on.
    word_counts: dict[str, int] = {}
    for r in c.conn.execute("SELECT keywords FROM facts WHERE status='active' AND ifnull(keywords,'')<>''"):
        for word in (r["keywords"] or "").replace(";", ",").split(","):
            word = word.strip().lower()
            if 3 <= len(word) <= 24:
                word_counts[word] = word_counts.get(word, 0) + 1
    words = sorted(word_counts.items(), key=lambda kv: -kv[1])[:10]

    # Buckets are the reader's hours, not UTC: a recall at 23:00 in California must
    # land in the 23:00 bar, not in the 07:00 one.
    by_hour = [0] * 24
    for r in c.conn.execute("SELECT ts FROM injected"):
        try:
            moment = datetime.fromisoformat(str(r["ts"]))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            by_hour[moment.astimezone().hour] += 1
        except Exception:
            continue

    most_recalled = [dict(r) for r in c.conn.execute(
        "SELECT id, substr(text,1,90) AS text, coalesce(uses,0) AS n FROM facts"
        " WHERE status='active' AND coalesce(uses,0)>0"
        " ORDER BY n DESC, last_used_at DESC LIMIT 8")]

    # Average PER LAYER: mixing 300-character facts and 40 000-character sheets
    # gave an average that described neither.
    averages = {str(r["importance"]): round(r["m"] or 0) for r in c.conn.execute(
        "SELECT importance, avg(length(text)) m FROM facts WHERE status='active' GROUP BY importance")}

    return {
        "series": series,
        "layers": layers,
        "confidence": confidence,
        "topics": topics,
        "sources": sources,
        "coverage": {"used": used, "never": never},
        "sizes": sizes,
        "keywords": [{"word": m, "n": n} for m, n in words],
        "by_hour": by_hour,
        "most_recalled": most_recalled,
        "averages": averages,
        "average_length": averages.get("1", 0),
    }


@router.get("/usage")
def usage(limit: int = 12) -> dict:
    """Usage leaderboard: which memories actually come out, and when."""
    c = _base()
    top = [dict(r) for r in c.conn.execute(
        "SELECT id, substr(text,1,120) AS text, topic, importance,"
        " coalesce(uses,0) AS uses, last_used_at"
        " FROM facts WHERE status='active' AND coalesce(uses,0) > 0"
        " ORDER BY uses DESC, last_used_at DESC LIMIT ?", (limit,)
    )]
    by_day = [dict(r) for r in c.conn.execute(
        "SELECT substr(ts,1,10) AS day, count(*) AS n FROM injected GROUP BY day ORDER BY day"
    )]
    never = c.conn.execute(
        "SELECT count(*) n FROM facts WHERE status='active' AND coalesce(uses,0)=0"
    ).fetchone()["n"]
    total = c.conn.execute(
        "SELECT coalesce(sum(uses),0) n FROM facts WHERE status='active'"
    ).fetchone()["n"]
    return {"top": top, "by_day": by_day, "never_used": never, "total_uses": total}


@router.get("/injections")
def injections(limit: int = 20) -> dict:
    """What the plugin really sent to the model, turn by turn."""
    p = _folder() / "injections.jsonl"
    if not p.exists():
        return {"turns": []}
    lines = p.read_text(encoding="utf-8").splitlines()[-max(1, min(limit, 200)):]
    turns = []
    for line in lines:
        try:
            turns.append(json.loads(line))
        except Exception:
            continue
    return {"turns": list(reversed(turns))}


# ------------------------------------------------------------------ watch
@router.get("/proposals")
def proposals(status: str = "pending") -> dict:
    c = _base()
    return {"proposals": _watch().list_proposals(c, status)}


@router.post("/watch/scan")
def scan() -> dict:
    """Find suspicious pairs and have the missing proposals written."""
    c = _base()
    cfg = _model_values()
    if cfg["mode"] not in ("custom", "lmstudio"):
        return {"disabled": True, "proposals": 0, "skipped": 0, "failures": 0,
                "candidates": 0, "pending": len(_watch().list_proposals(c, "pending"))}
    res = _watch().propose(c, url=cfg["url"], model=cfg["model"],
                           api_key=cfg["api_key"], wait=False)
    res["pending"] = len(_watch().list_proposals(c, "pending"))
    return res


@router.post("/proposals/{pid}/accept")
def accept(pid: str, keep: str = "a") -> dict:
    """For a contradiction: `keep=a|b` says which of the two versions stays."""
    return _watch().apply(_base(), pid, keep=keep)


@router.post("/proposals/{pid}/reject")
def reject(pid: str) -> dict:
    return _watch().reject(_base(), pid)


@router.post("/proposals/{pid}/regenerate")
def regenerate(pid: str) -> dict:
    cfg = _model_values()
    if cfg["mode"] not in ("custom", "lmstudio"):
        return {"ok": False, "disabled": True}
    return _watch().regenerate(_base(), pid, url=cfg["url"], model=cfg["model"],
                               api_key=cfg["api_key"], wait=False)


@router.get("/journal")
def journal(limit: int = 50) -> dict:
    c = _base()
    rows = c.conn.execute(
        "SELECT ts, actor, op, fact_id, payload FROM journal ORDER BY id DESC LIMIT ?",
        (max(1, min(limit, 300)),),
    ).fetchall()
    return {"entries": [_line(r) for r in rows]}
RING_INNER = 150        # clear margin: no memory under the central glyph
RING_OUTER = 300
RING_COUNT = 5
EDGE_MIN = 0.55          # minimum similarity for a link
EDGE_K = 3               # neighbours kept per node


def _ts(iso: str) -> int:
    """ISO 8601 → epoch secondes (0 si illisible)."""
    if not iso:
        return 0
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


# Golden angle: the only way to spread N points on a disc without ever creating
# alignments or holes, whatever N is.
ANGLE_OR = 2.399963229728653


def _semantic_angles(centroids: Dict[str, list]) -> Dict[str, float]:
    """Angle of each topic, derived from a force-directed placement.

    Nothing is placed from this result: only kinship is kept. Two topics talking
    about the same thing get neighbouring angles, so their fact_rows drift close
    instead of being spread at random.
    """
    noms = list(centroids)
    n = len(noms)
    if n < 3:
        return {nom: 2 * math.pi * i / max(1, n) for i, nom in enumerate(noms)}

    pos = [[math.cos(i * ANGLE_OR) * math.sqrt((i + 0.5) / n),
            math.sin(i * ANGLE_OR) * math.sqrt((i + 0.5) / n)] for i in range(n)]

    sim = [[0.0] * n for _ in range(n)]
    for i in range(n):
        vi = centroids[noms[i]]
        for j in range(i + 1, n):
            val = sum(map(mul, vi, centroids[noms[j]]))
            sim[i][j] = sim[j][i] = val

    SEUIL = 0.80
    temperature = 0.28
    for _ in range(110):
        deplacement = [[0.0, 0.0] for _ in range(n)]
        for i in range(n):
            xi, yi = pos[i]
            for j in range(i + 1, n):
                dx = xi - pos[j][0]
                dy = yi - pos[j][1]
                dist = math.hypot(dx, dy) or 1e-4
                force = (1.0 / dist - (dist * (sim[i][j] - SEUIL) * 9.0 if sim[i][j] > SEUIL else 0.0)) / dist
                deplacement[i][0] += dx * force
                deplacement[i][1] += dy * force
                deplacement[j][0] -= dx * force
                deplacement[j][1] -= dy * force
        for i in range(n):
            dx, dy = deplacement[i]
            norme = math.hypot(dx, dy) or 1.0
            pas = min(norme, temperature)
            pos[i][0] += dx / norme * pas
            pos[i][1] += dy / norme * pas
        temperature *= 0.975

    raw_angles = {noms[i]: math.atan2(pos[i][1], pos[i][0]) % (2 * math.pi) for i in range(n)}
    # Raw angles pile up on one side of the disc (force placement stretches in one
    # direction). The ORDER is kept, but the anchors are spread evenly, so every
    # topic gets its own sector and neighbours stay neighbours.
    # voisins.
    ordered = sorted(raw_angles, key=lambda nm: raw_angles[nm])
    return {nm: 2 * math.pi * i / len(ordered) for i, nm in enumerate(ordered)}


def _label(f: dict, topic: str) -> str:
    """Short, distinct label: the start of the fact, not the topic name."""
    text = " ".join((f.get("text") or "").split())
    if not text:
        return topic or f.get("id", "")
    if len(text) > 42:
        cut = text[:42].rsplit(" ", 1)[0]
        return (cut or text[:42]) + "…"
    return text


def _oriented_angle(rank: int, seed: str, topic_angle: float, force: float = 0.5) -> float:
    """Angle of a fact: the golden step, bent toward its topic.

    Only a fraction of the facts is pulled toward their topic's angle, with a wide
    tolerance (about a radian). The rest keeps the uniform spread, so the result is
    a gentle slope rather than tight balls.
    """
    base = (rank * ANGLE_OR) % (2 * math.pi)
    force = max(0.0, min(1.0, force))
    if force <= 0.0:
        return base
    draw = int(hashlib.sha256((seed + "|topic").encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    # Share of the facts affected: 0 leaves everything scattered, 1 bends them all.
    if draw > force:
        return base
    delta = (topic_angle - base + math.pi) % (2 * math.pi) - math.pi
    # Never a perfect snap: even at full force, only 85% of the way is covered.
    blend = 0.08 + 0.62 * force   # even at full force, 30% randomness remains
    return base + delta * blend


def _dispersion(rank: int, seed: str) -> tuple:
    """Position (angle, facteur de rayon) stable et sans motif visible.

    Deterministic: same identifier, same place, otherwise the graph would jump on
    every refresh. The radius factor breaks perfect circles apart.
    """
    angle = (rank * ANGLE_OR) % (2 * math.pi)
    bruit = int(hashlib.sha256(seed.encode()).hexdigest()[:4], 16) % 1000 / 1000.0
    return angle, 0.86 + 0.14 * bruit


def _graph(influence: float = 0.5) -> Dict[str, Any]:
    if not (_folder() / "carnet.db").exists():
        return {"nodes": [], "edges": [], "rings": [], "clusters": [],
                "stats": {"nodes": 0, "edges": 0, "fiches": 0, "facts": 0}, "zg_ok": False}

    c = _base().conn
    fact_rows = [dict(r) for r in c.execute(
        "SELECT id, text AS text, topic AS topic, keywords AS mots_cles, importance, "
        "confidence AS confiance, created_at AS cree_le, source "
        "FROM facts WHERE status = 'active'")]

    if not fact_rows:
        return {"nodes": [], "edges": [], "rings": [], "clusters": [],
                "stats": {"nodes": 0, "edges": 0, "fiches": 0, "facts": 0}, "zg_ok": True}

    timestamps = [(_ts(f["cree_le"]) or 0) for f in fact_rows]
    ts_by_id = {f["id"]: ts for f, ts in zip(fact_rows, timestamps)}
    t_min, t_max = min(timestamps), max(timestamps)
    span_seconds = max(1, t_max - t_min)

    # Rings: distributed by RANK, not by value. On a corpus where almost everything
    # was written in the same minute, proportional buckets stack 145 nodes in the
    # first ring and leave the others empty. Rank guarantees a balanced crown
    # whatever the real distribution is.
    ordered = sorted(fact_rows, key=lambda f: ts_by_id.get(f["id"], 0))
    total_count = len(ordered)
    rank = {f["id"]: i for i, f in enumerate(ordered)}
    per_ring = {i: [] for i in range(RING_COUNT)}
    for f in ordered:
        idx = min(RING_COUNT - 1, rank[f["id"]] * RING_COUNT // max(1, total_count))
        per_ring[idx].append(f)

    # A ring's label is the median date of its content: it describes what is on
    # screen, not a theoretical bound.
    if span_seconds >= 60 * 86400:
        fmt = "%b %Y"
    elif span_seconds >= 3 * 86400:
        fmt = "%b %-d, %Y"
    else:
        fmt = "%b %-d · %H:%M"

    # Time discriminates nothing when everything was written within the same hour:
    # all five rings would carry the same date. Writing order takes over then.
    buckets = {}
    for ts in timestamps:
        if ts > 0:
            buckets[ts // 3600] = buckets.get(ts // 3600, 0) + 1
    max_share = max(buckets.values()) / max(1, len(timestamps)) if buckets else 1.0
    axe_temporel = span_seconds >= 6 * 3600 and max_share < 0.7

    rings = []
    running = 0
    for i in range(RING_COUNT):
        r = RING_INNER + (RING_OUTER - RING_INNER) * (i + 1) / RING_COUNT
        members = per_ring[i]
        running += len(members)
        if axe_temporel and members:
            middle = ts_by_id.get(members[0]["id"], 0)
            label = time.strftime(fmt, time.localtime(middle)) if middle else ""
        elif members:
            label = f"{running} souvenirs"
        else:
            label = ""
        rings.append({"r": round(r, 1), "label": label})

    # Topic angles: computed once, before anything is placed.
    angle_vectors: Dict[str, array] = {}
    try:
        for r_v in c.execute("SELECT fact_id, blob FROM vec"):
            angle_vectors[r_v["fact_id"]] = array("f", r_v["blob"])
    except Exception:
        pass
    group_ids: Dict[str, list] = {}
    for f_g in fact_rows:
        group_ids.setdefault((f_g["topic"] or "sans topic").strip().lower(), []).append(f_g["id"])

    centroides_tmp: Dict[str, list] = {}
    for nom_t, ids_t in group_ids.items():
        vecs_t = [angle_vectors[i] for i in ids_t if i in angle_vectors]
        if not vecs_t:
            continue
        dim_t = len(vecs_t[0])
        acc_t = [0.0] * dim_t
        for v_t in vecs_t:
            for k_t in range(dim_t):
                acc_t[k_t] += v_t[k_t]
        norme_t = math.sqrt(sum(x * x for x in acc_t)) or 1.0
        centroides_tmp[nom_t] = [x / norme_t for x in acc_t]
    angles_sujet = _semantic_angles(centroides_tmp)

    nodes = []
    for idx, members in per_ring.items():
        rayon = rings[idx]["r"]
        n = len(members)
        for i, f in enumerate(members):
            a, facteur = _dispersion(rank[f["id"]], f["id"])
            if angles_sujet:
                a = _oriented_angle(
                    rank[f["id"]], f["id"],
                    angles_sujet.get((f["topic"] or "sans topic").strip().lower(), 0.0),
                    influence,
                )
            rayon_noeud = rayon * facteur
            fiche = int(f["importance"] or 1) == 2
            ts_f = ts_by_id.get(f["id"], 0)
            rec = (rank[f["id"]] / max(1, total_count - 1)) if total_count > 1 else 0.5
            nodes.append({
                "id": f["id"],
                "rec": round(rec, 4),
                "ring": idx,
                "title": _label(f, f["topic"]),
                "text": f["text"],
                "kind": "fiche" if fiche else "fact",
                "cluster": (f["topic"] or "sans topic").strip().lower(),
                "x": round(math.cos(a) * rayon_noeud, 1),
                "y": round(math.sin(a) * rayon_noeud, 1),
                "radius": 4.4 if fiche else 3.0,
                "timestamp": ts_f,
                "date_min": ts_f,
                "date_max": ts_f,
                "keywords": [k.strip() for k in (f["mots_cles"] or "").replace(";", ",").split(",") if k.strip()],
            })

    # Links: nearest neighbours by similarity, symmetrised and deduplicated.
    vecteurs: Dict[str, array] = {}
    try:
        for r in c.execute("SELECT fact_id, blob FROM vec"):
            vecteurs[r["fact_id"]] = array("f", r["blob"])
    except Exception:
        pass

    aretes: Dict[tuple, float] = {}
    groupes: Dict[str, list] = {}
    for n in nodes:
        if n["id"] in vecteurs:
            groupes.setdefault(n["cluster"], []).append(n["id"])

    for members in groupes.values():
        for i, a in enumerate(members):
            va = vecteurs[a]
            scores = []
            for b in members[i + 1:]:
                sc = sum(map(mul, va, vecteurs[b]))
                if sc >= EDGE_MIN:
                    scores.append((sc, b))
            scores.sort(reverse=True)
            for sc, b in scores[:EDGE_K]:
                cle = tuple(sorted((a, b)))
                aretes[cle] = max(aretes.get(cle, 0.0), round(sc, 3))

    edges = [{"source": a, "target": b, "weight": w} for (a, b), w in aretes.items()]

    clusters: Dict[str, int] = {}
    for n in nodes:
        clusters[n["cluster"]] = clusters.get(n["cluster"], 0) + 1

    return {
        "nodes": nodes,
        "edges": edges,
        "rings": rings,
        "clusters": [{"name": k, "count": v} for k, v in sorted(clusters.items(), key=lambda kv: -kv[1])],
        "stats": {
            "nodes": len(nodes),
            "edges": len(edges),
            "fiches": sum(1 for n in nodes if n["kind"] == "fiche"),
            "facts": sum(1 for n in nodes if n["kind"] == "fact"),
            "clusters": len(clusters),
        },
        "zg_ok": bool(vecteurs),
    }






# ------------------------------------------------------------------ graph
_GRAPH_CACHE: dict = {"key": None, "value": None}


def _db_stamp(db) -> tuple:
    """Fingerprint of the database *including* its WAL files.

    The main file's mtime barely moves while writes sit in carnet.db-wal, which
    is the normal state of a WAL database between checkpoints. A cache keyed on
    that mtime alone serves a stale layout long after new facts were written,
    which is why a fresh graph can stay empty after a restart.
    """
    stamp = []
    for suffix in ("", "-wal", "-shm"):
        path = db.parent / (db.name + suffix)
        try:
            st = path.stat()
            stamp.append((st.st_mtime_ns, st.st_size))
        except OSError:
            stamp.append((0, 0))
    return tuple(stamp)


@router.get("/graph")
def graph(refresh: int = 0, influence: float = 0.5):
    """Nodes already laid out on time rings, their links and their clusters.

    The layout costs real time on a large corpus: it is cached for as long as the
    database does not move, and `?refresh=1` forces the recomputation.
    """
    db = _folder() / "carnet.db"
    # The path is part of the key: the gateway keeps this module loaded across
    # profile switches, and two profiles can have databases of the same size and
    # mtime (a copy keeps both), so the stamp alone served another profile's
    # memories.
    key = (str(Path(db).resolve()), _db_stamp(db), refresh,
           round(max(0.0, min(1.0, influence)), 3))
    if not refresh and _GRAPH_CACHE["key"] == key and _GRAPH_CACHE["value"] is not None:
        return _GRAPH_CACHE["value"]
    value = _graph(influence)
    _GRAPH_CACHE["key"], _GRAPH_CACHE["value"] = key, value
    return value


@router.get("/node/{node_id}")
def node(node_id: str):
    """One node in full, for the detail panel."""
    c = _base().conn
    row = c.execute("SELECT * FROM facts WHERE id = ?", (node_id,)).fetchone()
    if not row:
        return {"id": node_id, "title": node_id, "text": "",
                "error": loc_mod.translate("errors.fact_not_found")}
    d = dict(row)
    return {
        "id": d["id"],
        "title": d.get("topic") or d["id"],
        "text": d.get("text") or "",
        "path": "",
        "keywords": [k.strip() for k in (d.get("keywords") or "").replace(";", ",").split(",") if k.strip()],
        "sources": [d["evidence"]] if d.get("evidence") else [],
        "sessions": [],
        "timestamp": _ts(d.get("created_at") or ""),
        "date_min": _ts(d.get("created_at") or ""),
        "date_max": _ts(d.get("created_at") or ""),
        "cluster": (d.get("topic") or "no topic").strip().lower(),
        "importance": d.get("importance"),
        "confidence": d.get("confidence"),
        "status": d.get("status"),
    }


# One migration pass at load: a credential an older version wrote into the
# shared config is removed before any route can serve it.
try:
    _scrub_secrets()
except Exception:      # never block the plugin on a migration
    pass
