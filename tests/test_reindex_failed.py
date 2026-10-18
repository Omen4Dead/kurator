"""Файл, который не прочитался, не должен застревать в индексе навсегда.

Поломка найдена на живом архиве: в индексе лежало 810 файлов `.heic`
размером с тетрадный лист, и ни у одного не было ни эмбеддинга, ни pHash.
Причина была не в том, что kurator не умел их читать, — к тому моменту
умел. Он их не перечитывал.

Инкрементальная индексация пропускает файл, если размер и mtime совпали с
записанными. Оба признака у непрочитанного файла не меняются никогда,
поэтому он объявлялся обработанным при каждом запуске. Итог: после
обновления kurator старый архив сам по себе не чинится, и нужно знать про
`index --rehash`, о чём обычный пользователь не знает.
"""

from __future__ import annotations

from pathlib import Path

from kurator.db import Store
from kurator.indexer import index_root


def _broken_archive(root: Path) -> None:
    """Файл, который Pillow открыть не сможет ни при каком раскладе.

    Имя с расширением, которое kurator обещает, а содержимое — не картинка.
    Ровно тот случай, который даёт строку в индексе без эмбеддинга.
    """
    (root / "нечитаемый.jpg").write_bytes("не картинка, а 20 байт мусора".encode())


def test_unreadable_file_is_retried_on_the_next_run(tmp_path):
    """Второй проход обязан снова взять файл, который не удалось прочитать."""
    store = Store.open(tmp_path)
    _broken_archive(tmp_path)

    first = index_root(tmp_path, store)
    assert first["seen"] == 1
    rows = store.conn.execute(
        "SELECT embed IS NOT NULL, phash IS NOT NULL FROM photos"
    ).fetchall()
    assert len(rows) == 1
    assert not rows[0][0], "эмбеддинга у нечитаемого файла взяться неоткуда"

    second = index_root(tmp_path, store)

    assert second["skipped"] == 0, (
        "файл без эмбеддинга объявлен обработанным и больше никогда не "
        "будет перечитан: размер и mtime не меняются. После починки формата "
        "или после смены настроек он так и останется пустым."
    )
    assert second["seen"] == 1
    store.close()


def test_healthy_file_is_still_skipped(tmp_path):
    """Починка не должна ломать экономию: готовый файл пропускается."""
    store = Store.open(tmp_path)
    good = tmp_path / "нормальный.png"
    good.write_bytes(Path("assets/icon-256.png").read_bytes())

    index_root(tmp_path, store)
    second = index_root(tmp_path, store)

    assert second["skipped"] == 1, (
        "файл с эмбеддингом обязан пропускаться, иначе повторный index "
        "пересчитывает весь архив заново"
    )
    store.close()


def test_known_state_reports_whether_anything_was_computed(tmp_path):
    """База обязана отличать «посчитали» от «записали и не смогли»."""
    store = Store.open(tmp_path)
    _broken_archive(tmp_path)
    index_root(tmp_path, store)

    known = store.known_state()
    (entry,) = [v for v in known.values()]

    assert len(entry) == 3, "третье значение — признак обработки, его нет"
    assert entry[2] is False, (
        "файл без единого хеша помечен обработанным — инкрементальный проход "
        "пропустит его навсегда"
    )
    store.close()


def test_video_is_not_rehashed_on_every_run(tmp_path):
    """Видео не имеют ни эмбеддинга, ни pHash — и это не причина читать их заново.

    По замыслу у видео только md5. Если считать «обработанным» файл по
    наличию хеша образа, то шестьсот роликов на живом архиве
    перечитывались при каждом запуске, и каждый раз считался заново их
    md5 — целиком по файлу.
    """
    store = Store.open(tmp_path)
    clip = tmp_path / "ролик.mp4"
    clip.write_bytes("не настоящее видео, но расширение видео".encode())

    index_root(tmp_path, store)
    second = index_root(tmp_path, store)

    assert second["skipped"] == 1, (
        "видео перечитывается при каждом индексировании: у него нет ни "
        "эмбеддинга, ни pHash, и признак обработки никогда не "
        "устанавливается. Нужен отдельный флажок is_video."
    )
    store.close()
