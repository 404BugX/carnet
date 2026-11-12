"""CLI commands for the carnet provider: `hermes carnet <cmd>`."""
from __future__ import annotations

from pathlib import Path

from .loc import translate


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        import os
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _stats(args) -> None:
    from .store import Carnet
    c = Carnet(_home() / "carnet")
    c.init_schema()
    d = c.count()
    print("carnet")
    for k, v in d.items():
        print(f"  {k:<12} {v}")
    print(f"  {translate('cli.stat_core_updated'):<12} {c.meta_get('core_updated_at', '-')}")
    print(f"  {translate('cli.stat_last_watch'):<12} {c.meta_get('last_watch', '-')}")


def _carnet_command(args) -> None:
    sub = getattr(args, "carnet_command", None)
    if sub == "stats":
        _stats(args)
    else:
        print(translate("cli.usage"))


def register_cli(subparser) -> None:
    subs = subparser.add_subparsers(dest="carnet_command")
    subs.add_parser("stats", help=translate("cli.help_stats"))
    subparser.set_defaults(func=_carnet_command)
