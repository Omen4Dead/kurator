"""Удаление пропавших файлов из индекса.

Поломка найдена на живом архиве. `index` отрапортовал «удалено из индекса
262», а строк в базе стало на пять больше, а не на 262 меньше: удаление
не удаляло ничего.

Причина: `forget` искал по `rel_path`, а получал из `known_state` ключи,
уже свёрнутые в нижний регистр. На архиве с `Yandex.Disk.localized` и
`Фото/Photos from 2013/…/IMG.JPG` совпадения нет никогда.

Две вещи, которые из-за этого тихо идут не так: в индексе копились
призраки файлов, которых на диске нет давно, а в выводе стояло число,
обратное правде.
"""

from __future__ import annotations

from pathlib import Path

from kurator.db import Store
from kurator.indexer import index_root


def _two_files(root: Path) -> None:
    src = Path("assets/icon-256.png").read_bytes()
    (root / "Первый").mkdir(exist_ok=True)
    (root / "Первый" / "СНИМОК.JPG").write_bytes(src)


def test_removed_file_is_actually_removed(tmp_path):
    """Файл исчез с диска — исчезнуть должен и из индекса."""
    store = Store.open(tmp_path)
    _two_files(tmp_path)

    index_root(tmp_path, store)
    assert store.conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 1

    (tmp_path / "Первый" / "СНИМОК.JPG").unlink()
    stats = index_root(tmp_path, store)

    left = store.conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    assert left == 0, (
        f"файла на диске нет, но в индексе осталось {left} строк. "
        "Удаление идёт по rel_path, а ключ приходит свёрнутым в "
        "нижний регистр, — и не находит ничего."
    )
    assert stats["removed"] == 1, (
        f"счётчик говорит об удалении {stats['removed']}, а в индексе "
        f"осталось {left} строк: число в выводе неправда"
    )
    store.close()


def test_uppercase_path_is_not_a_barrier_to_deletion(tmp_path):
    """Регистр в имени на диске не должен мешать удалению."""
    store = Store.open(tmp_path)
    _two_files(tmp_path)
    index_root(tmp_path, store)

    (rel,) = [r[0] for r in store.conn.execute("SELECT rel_path FROM photos")]
    assert rel != rel.casefold(), "нужен путь с заглавными, иначе тест не про то"

    store.forget([rel.casefold()])
    left = store.conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    assert left == 0, f"forget по свёрнутому ключу оставил {left} строк"
    store.close()


def test_forget_accepts_the_original_spelling_too(tmp_path):
    """`index_paths` зовёт `forget` прямо из командной строки.

    Там приходит исходное написание, а не свёрнутый ключ, — значит
    `forget` обязан понимать оба.
    """
    store = Store.open(tmp_path)
    _two_files(tmp_path)
    index_root(tmp_path, store)

    (rel,) = [r[0] for r in store.conn.execute("SELECT rel_path FROM photos")]
    store.forget([rel])

    left = store.conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    assert left == 0, "forget по исходному написанию не сработал"
    store.close()
