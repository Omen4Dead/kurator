"""Поштучный возврат из корзины и правда счёта в ней.

Две вещи, которые выяснились при переработке корзины.

Первая — откат был только целым переносом. `undo_run` возвращает всё,
что переносили одной кнопкой, а в корзине человек отмечает файлы
галочками и ждёт, что вернётся именно отмеченное. `restore_paths`
делает поштучный возврат и переписывает манифест, иначе возвращённый
файл остался бы в истории и вернулся повторно.

Вторая — счётчик в шапке врал. Он считался по `/api/runs`, а тот
режет выборку двадцатью переносами, так что при большем количестве
переносов кнопка «Вернуть из карантина» показывала меньше, чем там
лежало. Источник правды один и он без предела — `/api/trash`.
"""

from __future__ import annotations

import json
from pathlib import Path

from kurator.db import Store
from kurator.quarantine import (
    apply_moves,
    list_runs,
    list_trash,
    plan_moves,
    quarantine_root,
    restore_paths,
)


def _store_with_files(root: Path, names: list[str], payload: bytes = b"x" * 512) -> Store:
    store = Store.open(root)
    for name in names:
        (root / name).write_bytes(payload)
    store.commit()
    return store


def _keys_of(store: Store) -> dict[str, dict]:
    return {item["key"]: item for item in list_trash(store)}


def test_restore_one_file_leaves_others_quarantined(tmp_path):
    """Вернули один файл из трёх — остальные два лежат на месте."""
    store = _store_with_files(tmp_path, ["a.txt", "b.txt", "c.txt"])
    moves = plan_moves(tmp_path, ["a.txt", "b.txt", "c.txt"])
    assert len(moves) == 3
    apply_moves(store, moves, "by-hand", dry_run=False)

    keys = sorted(_keys_of(store))
    result = restore_paths(store, [keys[0]])
    store.close()

    assert result["restored"] == 1, result
    assert result["failed"] == []
    # Возвращённый файл вернулся на прежнее место и больше не в корзине.
    assert (tmp_path / "a.txt").is_file()
    assert len(list_trash(_reopen(tmp_path))) == 2
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["a.txt"]


def _reopen(root: Path) -> Store:
    return Store.open(root)


def test_manifest_keeps_only_files_still_quarantined(tmp_path):
    """Частичный возврат не оставляет возвращённое в истории.

    Если манифест не переписать, файл вернётся повторно при следующем
    откате этого же переноса — а это ровно тот баг, из-за которого
    поштучный возврат и пришлось писать с переписыванием.
    """
    store = _store_with_files(tmp_path, ["a.txt", "b.txt", "c.txt"])
    apply_moves(store, plan_moves(tmp_path, ["a.txt", "b.txt", "c.txt"]), "by-hand", False)
    runs_before = list_runs(store)
    assert len(runs_before) == 1
    manifest = Path(
        store.query("SELECT manifest FROM runs")[0]["manifest"]
    )

    keys = sorted(_keys_of(store))
    restore_paths(store, [keys[0]])
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    still = {m["to"] for m in payload["moves"]}
    store.close()

    assert keys[0] not in still, "возвращённый файл остался в манифесте"
    assert len(still) == 2
    assert payload["count"] == 2
    assert payload["bytes"] > 0, "размер считается по адресу в карантине, не в архиве"


def test_restore_all_files_removes_the_run(tmp_path):
    """Вернули всё из переноса — записи о переносе больше нет."""
    store = _store_with_files(tmp_path, ["a.txt", "b.txt"])
    apply_moves(store, plan_moves(tmp_path, ["a.txt", "b.txt"]), "by-hand", False)

    keys = sorted(_keys_of(store))
    result = restore_paths(store, keys)
    rows = store.query("SELECT COUNT(*) AS n FROM runs")[0]["n"]
    store.close()

    assert result["restored"] == 2
    assert rows == 0, "перенос возвращён целиком, запись о нём обязана исчезнуть"
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["a.txt", "b.txt"]


def test_restore_refuses_when_old_place_occupied(tmp_path):
    """Занятое прежнее место не затирается: файл остаётся в корзине."""
    store = _store_with_files(tmp_path, ["a.txt"])
    apply_moves(store, plan_moves(tmp_path, ["a.txt"]), "by-hand", False)

    (tmp_path / "a.txt").write_bytes(b"someone else is here")
    keys = sorted(_keys_of(store))
    result = restore_paths(store, keys)
    store.close()

    assert result["restored"] == 0
    assert len(result["failed"]) == 1
    assert "уже есть файл" in result["failed"][0]["error"]
    assert (tmp_path / "a.txt").read_bytes() == b"someone else is here"
    # И в корзине он остался, а не потерялся.
    assert len(list_trash(_reopen(tmp_path))) == 1


def test_orphan_without_manifest_is_reported_not_lost(tmp_path):
    """Файл без манифеста вернуть нечем — но об этом надо сказать."""
    store = _store_with_files(tmp_path, ["a.txt"])
    orphan = quarantine_root(tmp_path) / "b.jpg"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan")

    keys = sorted(_keys_of(store))
    result = restore_paths(store, keys)
    store.close()

    assert result["restored"] == 0
    assert result["orphans"] == keys
    assert orphan.is_file(), "файл без манифеста обязан остаться на месте"


def test_restore_puts_file_back_into_index(tmp_path):
    """Возвращённый файл снова находится в индексе.

    Иначе после возврата он не будет ни в карантине, ни в архиве по
    мнению kurator — то есть пропадёт ровно тем же способом, каким
    раньше пропадали файлы, удалённые с диска.

    Файл здесь настоящий JPEG, а не `a.txt`: индекс собирает только
    медиафайлы, и служебный файл в него не попадает ни при каких
    условиях — на этом первом варианте теста и поймал несуществующий
    баг.
    """
    from PIL import Image

    from kurator.db import Photo

    photo = tmp_path / "shot.jpg"
    Image.new("RGB", (64, 48), (120, 90, 60)).save(photo, "JPEG")

    store = Store.open(tmp_path)
    info = photo.stat()
    store.upsert(Photo(rel_path="shot.jpg", size=info.st_size, mtime=info.st_mtime, md5="m"))
    store.commit()

    apply_moves(store, plan_moves(tmp_path, ["shot.jpg"]), "by-hand", False)
    keys = sorted(_keys_of(store))
    restore_paths(store, keys)
    names = {p.rel_path for p in store.photos()}
    store.close()

    assert "shot.jpg" in names
    assert (tmp_path / "shot.jpg").is_file()


def test_assets_never_render_a_bare_global():
    """В разметку не попадает `top` без объявления.

    `window.top` — это глобальное имя браузера, и строка вида
    `main.innerHTML = top + ...` не бросает ошибку, а молча подставляет
    объект окна, который на экране выглядит как `[object Window]`.
    Ошибку такого рода `node --check` не видит, а человек замечает
    только глазами, и то если знает, что искать.
    """
    import re

    source = (Path(__file__).resolve().parent.parent
              / "src" / "kurator" / "web" / "assets" / "app.js").read_text(encoding="utf-8")
    stray = re.findall(r"(?<![\w.$])top(?![\w$])", source)
    assert not stray, (
        "в app.js есть `top` без объявления — это window.top, и на экране "
        "появится [object Window]"
    )


def test_trash_count_has_no_limit_unlike_runs(tmp_path):
    """Счётчик должен видеть всё, а не двадцать переносов.

    Именно на этом счётчик в шапке и врал: он считал по `/api/runs`,
    где стоит `limit=20`. Двадцать пять переносов показывались как
    двадцать файлов, и пять переносов становились недоступны для
    отката из интерфейса молча.
    """
    store = Store.open(tmp_path)
    for i in range(25):
        name = f"photo_{i:02d}.jpg"
        (tmp_path / name).write_bytes(b"y" * 256)
        apply_moves(store, plan_moves(tmp_path, [name]), "by-hand", False)

    in_trash = list_trash(store)
    in_runs = list_runs(store)
    store.close()

    assert len(in_runs) == 20, "предел list_runs на месте — это и есть причина"
    assert len(in_trash) == 25, "корзина обязана видеть всё"