"""Translations for the carnet plugin.

One YAML file per language in ``locales/``: a plain tree of keys, no includes,
no plural machinery, no interpolation syntax beyond ``{name}``. The files are
read by a minimal parser (nested ``key: value`` lines with quoted scalars),
because PyYAML is not a dependency of the plugin and must not become one.

The active language follows Hermes itself (``HERMES_LANGUAGE``, then
``display.language`` read from ``$HERMES_HOME/config.yaml``), so the plugin
speaks whatever the interface speaks. A ``language`` field in
``$HERMES_HOME/config/carnet.json`` overrides it, which is the manual switch in
the dashboard. A language with no locale file here falls back to English.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

DEFAULT = "en"
_LOCALES = Path(__file__).resolve().parent / "locales"

_trees: dict[str, dict] = {}
_active: str | None = None
_stamp: tuple[int | None, str] | None = None

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _scalar(raw: str) -> str:
    """A value on the right of the colon: quoted (JSON escapes) or plain."""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        if raw[0] == '"':
            return json.loads(raw)
        return raw[1:-1].replace("''", "'")
    return raw


def _parse(text: str) -> dict:
    """Parse the strict subset used by the locale files: nested mappings, one
    ``key: value`` per line, values on a single line, ``#`` comments."""
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        key, sep, value = stripped.partition(":")
        if not sep or not key.strip():
            raise ValueError(f"unsupported line: {line!r}")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        key = key.strip()
        value = value.strip()
        if not value:
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _scalar(value)
    return root


def _tree(language: str) -> dict:
    """The parsed file for one language, cached after the first load."""
    if language in _trees:
        return _trees[language]
    tree: dict = {}
    try:
        tree = _parse((_LOCALES / f"{language}.yaml").read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        tree = {}
    _trees[language] = tree
    return tree


def language() -> str:
    """The configured language, or English when absent, unknown or unreadable.

    The config file is only re-read when its mtime changes: editing
    ``carnet.json`` applies without restarting anything.
    """
    global _active, _stamp
    path = _home() / "config" / "carnet.json"
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        stamp = None
    explicit = ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            explicit = str(data.get("language") or "").strip().lower()
    except (OSError, ValueError):
        explicit = ""
    name = explicit or _hermes_language()
    key = (stamp, name)
    if _active is not None and key == _stamp:
        return _active
    _active = _available(name)
    _stamp = key
    return _active


def _hermes_language() -> str:
    """Hermes' own active language (``HERMES_LANGUAGE`` > ``display.language``).

    Resolved locally with the same minimal YAML parser as the locale files: no
    private import from the core, and an unreadable or incomplete config falls
    back to English.
    """
    explicit = os.environ.get("HERMES_LANGUAGE", "").strip().lower()
    if explicit:
        return explicit
    try:
        tree = _parse((_home() / "config.yaml").read_text(encoding="utf-8"))
        display = tree.get("display")
        if isinstance(display, dict):
            return str(display.get("language") or "").strip().lower()
    except (OSError, ValueError):
        pass
    return ""


def _available(name: str) -> str:
    """The locale file to use for ``name``: itself, its base language, or English."""
    if not name:
        return DEFAULT
    if (_LOCALES / f"{name}.yaml").exists():
        return name
    base = name.split("-")[0]
    if base and (_LOCALES / f"{base}.yaml").exists():
        return base
    return DEFAULT


def config_path() -> Path:
    """The plugin's own settings file, beside the rest of the profile's config."""
    return _home() / "config" / "carnet.json"


def explicit_language() -> str:
    """The language written in ``carnet.json``, or '' when the file says nothing."""
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("language") or "").strip().lower()


def source() -> str:
    """Where the effective language comes from: ``config``, ``hermes`` or ``default``."""
    if explicit_language():
        return "config"
    return "hermes" if _hermes_language() else "default"


def available() -> list[str]:
    """The languages shipped with the plugin, English first."""
    names = sorted(p.stem for p in _LOCALES.glob("*.yaml"))
    if DEFAULT in names:
        names.remove(DEFAULT)
        names.insert(0, DEFAULT)
    return names


def set_language(name: str) -> str:
    """Write ``language`` into ``carnet.json``, preserving the other keys.

    The file is the switch: the next ``language()`` call sees the new mtime and
    the dashboard picks the strings up without a restart.
    """
    name = (name or "").strip().lower()
    if name not in available():
        raise ValueError(f"unsupported language: {name}")
    path = config_path()
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    data["language"] = name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    global _active, _stamp
    _active = None
    _stamp = None
    return language()


def tree() -> dict:
    """The key tree of the active language, as served to the dashboard."""
    return _tree(language())


def _lookup(node: dict, key: str):
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, str) else None


def translate(key: str, **variables) -> str:
    """Resolve ``key`` in the active language, then in English.

    A key missing from both comes back as written: no exception, ever.
    """
    lang = language()
    value = _lookup(_tree(lang), key)
    if value is None and lang != DEFAULT:
        value = _lookup(_tree(DEFAULT), key)
    if value is None:
        return key
    if not variables:
        return value
    return _PLACEHOLDER.sub(
        lambda m: str(variables[m.group(1)]) if m.group(1) in variables else m.group(0),
        value,
    )
