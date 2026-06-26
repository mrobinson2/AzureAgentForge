"""Offline test for the WAL-markers build-time patch (no Hermes deps needed)."""
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "patch_wal_markers",
    pathlib.Path(__file__).resolve().parents[1] / "patch-hermes-wal-markers.py",
)
patch_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(patch_mod)

SAMPLE = (
    "_WAL_INCOMPAT_MARKERS = (\n"
    '    "locking protocol",       # SQLITE_PROTOCOL on NFS/SMB\n'
    '    "not authorized",         # Some FUSE mounts block WAL pragma outright\n'
    '    "disk i/o error",         # Flaky network FS during WAL setup\n'
    ")\n"
)


def test_adds_database_is_locked_marker_inside_the_tuple():
    out = patch_mod.patch(SAMPLE)
    assert '"database is locked"' in out
    # inserted after the disk-i/o line and before the tuple closes
    assert out.index('"database is locked"') > out.index('"disk i/o error"')
    assert out.index('"database is locked"') < out.rindex(")")


def test_idempotent():
    once = patch_mod.patch(SAMPLE)
    twice = patch_mod.patch(once)
    assert once == twice
    assert once.count('"database is locked"') == 1


def test_missing_anchor_fails_loud():
    import pytest

    with pytest.raises(SystemExit):
        patch_mod.patch("_WAL_INCOMPAT_MARKERS = ()\n")
