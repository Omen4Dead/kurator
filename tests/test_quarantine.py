import datetime as dt

from kurator.db import Store
from kurator.quarantine import apply_moves, plan_moves, quarantine_root, undo_last_run


def seed(root):
    store = Store.open(root)
    store.upsert_row = None
    return store


def make_store(root):
    from kurator.db import Photo

    store = Store.open(root)
    for name, payload in (("a.txt", b"one"), ("b.txt", b"one"), ("c.txt", b"three")):
        path = root / name
        path.write_bytes(payload)
        store.upsert(
            Photo(rel_path=name, size=path.stat().st_size, mtime=path.stat().st_mtime, md5="x")
        )
    store.commit()
    return store


def test_plan_moves_skips_missing_and_collisions(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.jpg").write_bytes(b"x")
    quarantine_root(tmp_path).joinpath("sub").mkdir(parents=True)
    quarantine_root(tmp_path).joinpath("sub", "a.jpg").write_bytes(b"y")

    moves = plan_moves(tmp_path, ["sub/a.jpg", "sub/nope.jpg"])
    assert moves == []


def test_apply_then_undo_round_trip(tmp_path):
    store = make_store(tmp_path)
    moves = plan_moves(tmp_path, ["b.txt"])
    assert len(moves) == 1

    preview = apply_moves(store, moves, kind="exact", dry_run=True)
    assert preview["dry_run"] is True
    assert (tmp_path / "b.txt").exists(), "dry-run не должен ничего двигать"

    result = apply_moves(store, moves, kind="exact", dry_run=False)
    assert result["moved"] == 1
    assert not (tmp_path / "b.txt").exists()
    assert (quarantine_root(tmp_path) / "b.txt").exists()

    undo = undo_last_run(store)
    assert undo["moved"] == 1
    assert (tmp_path / "b.txt").exists()
    assert (tmp_path / "b.txt").read_bytes() == b"one"
    assert not quarantine_root(tmp_path).exists()
    store.close()


def test_undo_without_runs_is_harmless(tmp_path):
    make_store(tmp_path)
    store = Store.open(tmp_path)
    result = undo_last_run(store)
    assert result["moved"] == 0
    store.close()


def test_apply_updates_index(tmp_path):
    store = make_store(tmp_path)
    moves = plan_moves(tmp_path, ["b.txt"])
    apply_moves(store, moves, kind="exact", dry_run=False)
    paths = [p.rel_path for p in store.photos()]
    assert "b.txt" not in paths
    assert "a.txt" in paths
    store.close()