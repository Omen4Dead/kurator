import datetime as dt
from pathlib import Path

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


def test_undo_returns_file_to_index(tmp_path):
    """После отката файл обязан снова попасть в индекс, иначе пропадёт из отчёта."""
    from PIL import Image

    from kurator.db import Photo
    from kurator.quarantine import apply_moves, plan_moves, undo_last_run

    photo = tmp_path / "shot.jpg"
    Image.new("RGB", (64, 48), (120, 90, 60)).save(photo, "JPEG")

    store = Store.open(tmp_path)
    info = photo.stat()
    store.upsert(
        Photo(rel_path="shot.jpg", size=info.st_size, mtime=info.st_mtime, md5="m")
    )
    store.commit()
    assert "shot.jpg" in {p.rel_path for p in store.photos()}

    apply_moves(store, plan_moves(tmp_path, ["shot.jpg"]), kind="exact", dry_run=False)
    assert "shot.jpg" not in {p.rel_path for p in store.photos()}

    undo_last_run(store)
    assert (tmp_path / "shot.jpg").is_file()
    assert "shot.jpg" in {p.rel_path for p in store.photos()}
    store.close()


def test_skip_decisions_persist_and_clear(tmp_path):
    make_store(tmp_path)
    store = Store.open(tmp_path)

    store.record_decision(["b.txt", "c.txt"], "skip")
    store.commit()
    assert store.skipped() == {"b.txt", "c.txt"}

    store.clear_decisions("skip")
    store.commit()
    assert store.skipped() == set()

    store.record_decision(["a.txt"], "remove")
    store.commit()
    assert store.decided() == {"a.txt"}
    assert store.skipped() == set()
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

def test_history_remembers_last_roots(tmp_path, monkeypatch):
    from kurator.web import server as web

    monkeypatch.setattr(web, "history_path", lambda: tmp_path / "roots.json")
    for name in ("one", "two"):
        (tmp_path / name).mkdir()
    web.remember_root(tmp_path / "one")
    web.remember_root(tmp_path / "two")
    web.remember_root(tmp_path / "one")

    history = web.read_history()
    assert history[0].endswith("one")
    assert len(history) == 2, "повторный выбор не должен дублировать запись"


def test_last_root_skips_missing_and_broad(tmp_path, monkeypatch):
    from kurator.web import server as web

    monkeypatch.setattr(web, "history_path", lambda: tmp_path / "roots.json")
    (tmp_path / "good").mkdir()
    (tmp_path / "good" / "a.jpg").write_bytes(b"x")
    web.remember_root(tmp_path / "good")
    web.remember_root(tmp_path / "vanished")

    found = web.last_root()
    assert found is not None and found.name == "good"


def test_index_status_reads_existing_db(tmp_path):
    from PIL import Image

    from kurator.config import db_path
    from kurator.web.server import index_status

    assert index_status(tmp_path) is None

    store = Store.open(tmp_path)
    photo = tmp_path / "a.jpg"
    Image.new("RGB", (32, 32), (10, 20, 30)).save(photo, "JPEG")
    info = photo.stat()
    from kurator.db import Photo

    store.upsert(Photo(rel_path="a.jpg", size=info.st_size, mtime=info.st_mtime, md5="m"))
    store.set_meta("last_index_at", "1700000000")
    store.commit()
    store.close()

    status = index_status(tmp_path)
    assert status is not None
    assert status["files"] == 1
    assert status["last_index_at"] == 1700000000
    assert db_path(tmp_path).is_file()


def test_index_status_survives_missing_meta(tmp_path):
    """Индекс без строки last_index_at — обычное дело, читать его всё равно надо."""
    from PIL import Image

    from kurator.db import Photo
    from kurator.web.server import index_status

    store = Store.open(tmp_path)
    photo = tmp_path / "a.jpg"
    Image.new("RGB", (16, 16), (1, 2, 3)).save(photo, "JPEG")
    info = photo.stat()
    store.upsert(Photo(rel_path="a.jpg", size=info.st_size, mtime=info.st_mtime, md5="m"))
    store.commit()
    store.close()

    status = index_status(tmp_path)
    assert status is not None
    assert status["files"] == 1
    assert status["last_index_at"] is None


def test_blocked_roots_are_rejected():
    from kurator.web.server import too_broad

    assert too_broad(Path("/")) is True
    assert too_broad(Path("/System")) is True


def test_dry_run_writes_nothing(tmp_path):
    """Пробный прогон не должен оставлять следов в папке карантина."""
    make_store(tmp_path)
    store = Store.open(tmp_path)

    moves = plan_moves(tmp_path, ["b.txt"])
    apply_moves(store, moves, kind="exact", dry_run=True)

    assert not quarantine_root(tmp_path).exists(), "dry-run создал каталог карантина"
    assert (tmp_path / "b.txt").exists()
    store.close()
