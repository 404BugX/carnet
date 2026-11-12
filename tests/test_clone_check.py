"""The clone check must never destroy a memory it guessed wrong about.

Run: PYTHONPATH=~/.hermes/hermes-agent python3 -m unittest tests.test_clone_check -v
"""
import shutil
import sqlite3
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # …/Plugins

# The provider imports Hermes' MemoryProvider base. Run the suite inside a Hermes
# checkout, or point CARNET_HERMES_AGENT at one; otherwise skip rather than fail:
# the clone check itself needs nothing from Hermes.
agent_path = os.environ.get("CARNET_HERMES_AGENT") or ""
if agent_path:
    sys.path.insert(0, agent_path)
try:
    from carnet import provider as provider_mod
    from carnet.store import Carnet
except Exception as exc:                       # no Hermes on this machine
    raise unittest.SkipTest(f"carnet.provider needs Hermes' agent package: {exc}")


def _seed(root: Path, home: Path, n: int = 3) -> Carnet:
    """Create a database holding `n` facts, stamped as coming from `home`."""
    store = Carnet(root / "carnet")
    store.init_schema()
    for i in range(n):
        store.add_fact(f"fact number {i} about the garden and the fence")
    store.meta_set("profile_home", str(home))
    return store


def _store_of(prov) -> Carnet:
    """The store the provider is using right now (it is replaced on quarantine)."""
    assert prov._store is not None, "the provider lost its store"
    return prov._store


def _bare(root: Path, home: Path) -> provider_mod.CarnetProvider:
    """A provider without initialize(): the clone check only needs these three."""
    prov = provider_mod.CarnetProvider.__new__(provider_mod.CarnetProvider)
    prov._home = home
    prov._root = root / "carnet"
    prov._store = Carnet(prov._root)
    prov._store.init_schema()
    return prov


class CloneCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_same_home_through_symlink_is_not_a_move(self):
        real = self.tmp / "real"
        real.mkdir()
        alias = self.tmp / "alias"
        alias.symlink_to(real)
        _seed(real, real)
        prov = _bare(real, alias)          # opened through the symlink
        prov._check_clone()
        self.assertEqual(_store_of(prov).count()["facts"], 3, "memory must survive")
        self.assertEqual([p.name for p in (real / "carnet").glob("carnet-*.db")], [],
                         "nothing may be set aside")

    def test_copy_from_a_live_home_keeps_the_file(self):
        origin = self.tmp / "origin"
        origin.mkdir()
        _seed(origin, origin)
        copy = self.tmp / "copy"
        shutil.copytree(origin, copy)
        prov = _bare(copy, copy)
        prov._check_clone()
        self.assertEqual(_store_of(prov).count()["facts"], 0, "the copy starts empty")
        aside = list((copy / "carnet").glob("carnet-*.db"))
        self.assertEqual(len(aside), 1, "the copied database must be kept")
        kept = sqlite3.connect(aside[0]).execute(
            "SELECT count(*) FROM facts WHERE status='active'").fetchone()[0]
        self.assertEqual(kept, 3, "the aside file must still hold the facts")

    def test_moved_home_keeps_everything(self):
        gone = self.tmp / "gone"
        moved = self.tmp / "moved"
        moved.mkdir()
        _seed(moved, gone)
        prov = _bare(moved, moved)
        prov._check_clone()
        self.assertEqual(_store_of(prov).count()["facts"], 3, "a move keeps the memory")
        self.assertEqual(list((moved / "carnet").glob("carnet-*.db")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
