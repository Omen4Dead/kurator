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


def test_skip_decisions_are_per_group(tmp_path):
    """Пропуск хранится по отпечатку группы, а не по файлам.

    Хранили по файлам пропуск одной группы прятал ещё и соседнюю, с
    которой она делит файл: на широком пороге такое случается постоянно.
    """
    make_store(tmp_path)
    store = Store.open(tmp_path)

    store.record_decision("near:aaa", ["b.txt", "c.txt"], "skip")
    store.record_decision("near:bbb", ["x.jpg", "y.jpg"], "skip")
    store.commit()
    assert store.skipped() == {"near:aaa", "near:bbb"}
    assert len(store.skipped_groups()) == 2

    assert store.unskip("near:aaa") == 1
    store.commit()
    assert store.skipped() == {"near:bbb"}

    store.clear_decisions("skip")
    store.commit()
    assert store.skipped() == set()
    store.close()


def test_group_signature_is_unique_for_shared_first_member():
    """Две группы с общим первым файлом обязаны иметь разные отпечатки."""
    from kurator.dedupe import group_signature

    a = group_signature("near-strong", ["shared.jpg", "one.jpg"])
    b = group_signature("near-strong", ["shared.jpg", "two.jpg"])
    assert a != b
    assert group_signature("near-strong", ["one.jpg", "shared.jpg"]) == a
    assert group_signature("burst", ["shared.jpg", "one.jpg"]) != a


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


def test_undo_one_group_keeps_other_quarantined_files(tmp_path):
    """Откат одной группы не должен трогать чужие файлы карантина.

    Регрессия: откат обходил всё дерево карантина и удалял каждый файл,
    кроме json. Возвращая две фотографии одной группы, он сносил все
    переносы, сделанные раньше.
    """
    from kurator.db import Photo
    from kurator.quarantine import apply_moves, plan_moves, undo_run

    store = Store.open(tmp_path)
    for name in ("a1.jpg", "a2.jpg", "b1.jpg", "b2.jpg"):
        p = tmp_path / name
        p.write_bytes(name.encode())
        info = p.stat()
        store.upsert(Photo(rel_path=name, size=info.st_size, mtime=info.st_mtime, md5=name))
    store.commit()

    first = apply_moves(store, plan_moves(tmp_path, ["a1.jpg", "a2.jpg"]),
                       kind="by-hand", dry_run=False)
    second = apply_moves(store, plan_moves(tmp_path, ["b1.jpg", "b2.jpg"]),
                        kind="by-hand", dry_run=False)
    assert first["run_id"] != second["run_id"]

    quarantined = sorted(p.name for p in quarantine_root(tmp_path).rglob("*.jpg"))
    assert quarantined == ["a1.jpg", "a2.jpg", "b1.jpg", "b2.jpg"]

    undo_run(store, first["run_id"])

    assert (tmp_path / "a1.jpg").exists(), "свои файлы должны вернуться"
    assert (tmp_path / "b1.jpg").is_file() is False
    assert (tmp_path / "b2.jpg").is_file() is False
    assert sorted(p.name for p in quarantine_root(tmp_path).rglob("*.jpg")) == [
        "b1.jpg", "b2.jpg"
    ], "чужие файлы карантина обязаны уцелеть"
    store.close()


def test_list_runs_hides_runs_whose_files_are_gone(tmp_path):
    """Полосы карантина не должны показывать переносы без файлов.

    Регрессия: если файл карантина исчез (старый откат его удалил),
    в UI оставалась полоса, на которой «Вернуть» падал с ошибкой.
    """
    from kurator.db import Photo
    from kurator.quarantine import apply_moves, list_runs, plan_moves

    store = Store.open(tmp_path)
    for name in ("a.jpg", "b.jpg"):
        p = tmp_path / name
        p.write_bytes(name.encode())
        info = p.stat()
        store.upsert(Photo(rel_path=name, size=info.st_size, mtime=info.st_mtime, md5=name))
    store.commit()

    apply_moves(store, plan_moves(tmp_path, ["a.jpg"]), kind="by-hand", dry_run=False)
    apply_moves(store, plan_moves(tmp_path, ["b.jpg"]), kind="by-hand", dry_run=False)

    assert len(list_runs(store)) == 2

    next(quarantine_root(tmp_path).rglob("b.jpg")).unlink()
    runs = list_runs(store)

    assert [r["count"] for r in runs] == [1], "остаться должна только живая полоса"
    assert all(r["paths"] != ["b.jpg"] for r in runs)
    store.close()


def test_list_runs_survives_broken_manifest(tmp_path):
    """Битый или удалённый манифест не должен ронять весь список."""
    from kurator.db import Photo
    from kurator.quarantine import apply_moves, list_runs, plan_moves

    store = Store.open(tmp_path)
    p = tmp_path / "a.jpg"
    p.write_bytes(b"a")
    info = p.stat()
    store.upsert(Photo(rel_path="a.jpg", size=info.st_size, mtime=info.st_mtime, md5="a"))
    store.commit()

    apply_moves(store, plan_moves(tmp_path, ["a.jpg"]), kind="by-hand", dry_run=False)
    row = store.query("SELECT manifest FROM runs ORDER BY id DESC LIMIT 1")[0]["manifest"]
    Path(row).write_text("{не json", encoding="utf-8")

    assert list_runs(store) == []
    store.close()
