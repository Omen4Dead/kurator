import datetime as dt
import shutil
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


def test_list_trash_shows_quarantined_files_with_origin(tmp_path):
    """Корзина показывает, что лежит в карантине и откуда файл пришёл."""
    from kurator.db import Photo
    from kurator.quarantine import apply_moves, list_trash, plan_moves

    store = Store.open(tmp_path)
    for name in ("a.jpg", "b.jpg"):
        p = tmp_path / name
        p.write_bytes(name.encode() * 10)
        info = p.stat()
        store.upsert(Photo(rel_path=name, size=info.st_size, mtime=info.st_mtime, md5=name))
    store.commit()

    apply_moves(store, plan_moves(tmp_path, ["a.jpg", "b.jpg"]), kind="by-hand", dry_run=False)
    items = list_trash(store)

    assert {i["from_path"] for i in items} == {"a.jpg", "b.jpg"}
    assert all(i["to_path"].startswith(".kurator/quarantine/") for i in items)
    assert all(i["size"] > 0 and i["orphan"] is False for i in items)
    store.close()


def test_delete_trash_removes_files_and_manifest(tmp_path):
    """Необратимый выброс из корзины чистит файл, манифест и запись в базе."""
    from kurator.db import Photo
    from kurator.quarantine import apply_moves, delete_trash, list_trash, plan_moves

    store = Store.open(tmp_path)
    for name in ("a.jpg", "b.jpg"):
        p = tmp_path / name
        p.write_bytes(name.encode() * 10)
        info = p.stat()
        store.upsert(Photo(rel_path=name, size=info.st_size, mtime=info.st_mtime, md5=name))
    store.commit()

    apply_moves(store, plan_moves(tmp_path, ["a.jpg", "b.jpg"]), kind="by-hand", dry_run=False)
    manifest = Path(store.query("SELECT manifest FROM runs ORDER BY id DESC LIMIT 1")[0]["manifest"])
    assert manifest.is_file()

    result = delete_trash(store, [list_trash(store)[0]["key"]])

    assert result["deleted"] == 1
    assert result["failed"] == []
    assert result["bytes"] > 0
    assert len(list_trash(store)) == 1, "второй файл обязан уцелеть"
    assert manifest.is_file(), "манифест с оставшимся переносом не трогаем"
    store.close()


def test_delete_trash_drops_manifest_when_last_move_gone(tmp_path):
    """Выброшенный последний файл убирает манифест и запись о переносе."""
    from kurator.db import Photo
    from kurator.quarantine import apply_moves, delete_trash, list_trash, plan_moves

    store = Store.open(tmp_path)
    p = tmp_path / "a.jpg"
    p.write_bytes(b"a" * 10)
    info = p.stat()
    store.upsert(Photo(rel_path="a.jpg", size=info.st_size, mtime=info.st_mtime, md5="a"))
    store.commit()

    apply_moves(store, plan_moves(tmp_path, ["a.jpg"]), kind="by-hand", dry_run=False)
    manifest = Path(store.query("SELECT manifest FROM runs ORDER BY id DESC LIMIT 1")[0]["manifest"])

    delete_trash(store, [list_trash(store)[0]["key"]])

    assert list_trash(store) == []
    assert manifest.is_file() is False, "манифест без переносов не должен висеть"
    assert store.query("SELECT * FROM runs") == []
    store.close()


def test_delete_trash_refuses_paths_outside_quarantine(tmp_path):
    """Ключ вне карантина удалять нельзя, даже если он подставлен вручную."""
    from kurator.db import Photo
    from kurator.quarantine import delete_trash

    store = Store.open(tmp_path)
    (tmp_path / "precious.jpg").write_bytes("\u043d\u0435 \u0442\u0440\u043e\u043d\u044c".encode())
    info = (tmp_path / "precious.jpg").stat()
    store.upsert(Photo(rel_path="precious.jpg", size=info.st_size, mtime=info.st_mtime, md5="p"))
    store.commit()

    result = delete_trash(store, ["precious.jpg"])

    assert result["deleted"] == 0
    assert result["failed"] and "отказ" in result["failed"][0]["error"]
    assert (tmp_path / "precious.jpg").is_file(), "файл вне карантина обязан уцелеть"
    store.close()


def test_list_trash_reports_files_without_manifest(tmp_path):
    """Подложенный в карантин файл без манифеста виден и помечен сиротой."""
    from kurator.quarantine import list_trash, quarantine_root

    store = Store.open(tmp_path)
    stray = quarantine_root(tmp_path) / "Фото" / "подкидыш.jpg"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"x" * 4)

    items = list_trash(store)

    assert len(items) == 1
    assert items[0]["orphan"] is True
    assert items[0]["from_path"] is None
    store.close()


def test_undo_all_runs_empties_quarantine_at_once(tmp_path):
    """Кнопка «Вернуть из карантина» должна опустошать папку за один клик.

    Регрессия: откатывался только последний перенос, поэтому при
    нескольких переносах в карантине кнопка возвращала по одному файлу
    за нажатие и выглядела как нерабочая.
    """
    from kurator.db import Photo
    from kurator.quarantine import (apply_moves, plan_moves, undo_all_runs,
                                   list_trash, quarantine_root)

    store = Store.open(tmp_path)
    names = [f"{i}.jpg" for i in range(5)]
    for name in names:
        p = tmp_path / name
        p.write_bytes(name.encode() * 8)
        info = p.stat()
        store.upsert(Photo(rel_path=name, size=info.st_size, mtime=info.st_mtime, md5=name))
    store.commit()

    for name in names:
        apply_moves(store, plan_moves(tmp_path, [name]), kind="by-hand", dry_run=False)
    assert len(list_trash(store)) == 5

    result = undo_all_runs(store)

    assert result["moved"] == 5, "все пять файлов обязаны вернуться за один вызов"
    assert result["runs"] == 5
    assert list_trash(store) == []
    assert store.query("SELECT * FROM runs") == []
    for name in names:
        assert (tmp_path / name).is_file(), f"{name} не вернулся на место"
    assert quarantine_root(tmp_path).exists() is False
    store.close()


def test_migration_moves_quarantine_and_keeps_undo_working(tmp_path):
    """Переезд карантина в .kurator/quarantine не должен ломать откат.

    Регрессия: пути в манифесте записаны от корня, поэтому простая
    смена папки оставила бы файлы на месте, а манифест искал бы их
    по старому адресу и рапортовал «файла уже нет».
    """
    from kurator.db import Photo
    from kurator.quarantine import (apply_moves, list_trash, migrate_legacy_quarantine,
                                   plan_moves, quarantine_root, undo_all_runs)

    store = Store.open(tmp_path)
    for name in ("a.jpg", "b.jpg"):
        p = tmp_path / name
        p.write_bytes(name.encode() * 12)
        info = p.stat()
        store.upsert(Photo(rel_path=name, size=info.st_size, mtime=info.st_mtime, md5=name))
    store.commit()

    # раскладываем карантин вручную по старой схеме: файлы именно
    # переезжают из корня, как это делает apply_moves
    legacy = tmp_path / "_kurator_quarantine"
    legacy.mkdir()
    shutil.move(str(tmp_path / "a.jpg"), str(legacy / "a.jpg"))
    shutil.move(str(tmp_path / "b.jpg"), str(legacy / "b.jpg"))
    manifest = legacy / "manifest_by-hand_1_2.json"
    manifest.write_text(
        '{"kind":"by-hand","root":"%s","created_at":"2026-01-01T00:00:00","count":2,'
        '"bytes":24,"moves":[{"from":"a.jpg","to":"_kurator_quarantine/a.jpg"},'
        '{"from":"b.jpg","to":"_kurator_quarantine/b.jpg"}]}' % tmp_path,
        encoding="utf-8",
    )
    store.start_run("by-hand", manifest)
    store.commit()

    assert migrate_legacy_quarantine(tmp_path)["moved"] == 2
    assert legacy.exists() is False, "старая папка должна исчезнуть"
    assert quarantine_root(tmp_path).is_dir()

    rewritten = (quarantine_root(tmp_path) / manifest.name).read_text(encoding="utf-8")
    assert "_kurator_quarantine/a.jpg" not in rewritten
    assert ".kurator/quarantine/a.jpg" in rewritten

    items = list_trash(store)
    assert len(items) == 2, "после переезда оба файла должны быть видны в корзине"

    result = undo_all_runs(store)

    assert result["moved"] == 2
    for name in ("a.jpg", "b.jpg"):
        assert (tmp_path / name).read_bytes() == name.encode() * 12
    assert store.query("SELECT * FROM runs") == []
    store.close()


def test_migration_is_idempotent(tmp_path):
    """Повторный вызов миграции не должен ничего портить."""
    from kurator.quarantine import migrate_legacy_quarantine, quarantine_root

    legacy = tmp_path / "_kurator_quarantine"
    legacy.mkdir()
    (legacy / "a.jpg").write_bytes(b"a")

    first = migrate_legacy_quarantine(tmp_path)
    second = migrate_legacy_quarantine(tmp_path)

    assert first["moved"] == 1
    assert second["moved"] == 0
    assert (quarantine_root(tmp_path) / "a.jpg").is_file()
