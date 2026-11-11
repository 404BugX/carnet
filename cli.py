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


def _report(results: list[dict]) -> None:
    for r in results:
        print(translate(f"cli.link_{r['status']}", profile=r["profile"]))


def _link(args) -> None:
    from .profiles import link_all
    _report(link_all())


def _unlink(args) -> None:
    from .profiles import unlink_all
    _report(unlink_all())


def _carnet_command(args) -> None:
    sub = getattr(args, "carnet_command", None)
    if sub == "stats":
        _stats(args)
    elif sub == "link":
        _link(args)
    elif sub == "unlink":
        _unlink(args)
    else:
        print(translate("cli.usage"))


def register_cli(subparser) -> None:
    subs = subparser.add_subparsers(dest="carnet_command")
    subs.add_parser("stats", help=translate("cli.help_stats"))
    subs.add_parser("link", help=translate("cli.help_link"))
    subs.add_parser("unlink", help=translate("cli.help_unlink"))
    subparser.set_defaults(func=_carnet_command)
