"""Инварианты индекса и окружения, которые не должны разъезжаться.

Всё это проверяется на любой системе, но предмет — контракты kurator:
пути через «/», регистронезависимый ключ, дата без EXIF, отсутствие
внешних вызовов в рантайме.
"""

from __future__ import annotations

from kurator.config import rel_posix, unflatten
from kurator.contact import FONT_CANDIDATES, _font_is_readable
from kurator.imaging import file_times


def test_relative_paths_are_posix(tmp_path):
    """Путь в индексе всегда через «/», и обратно он разворачивается.

    Индекс, манифесты и веб-интерфейс режут пути через `split("/")`:
    путь, записанный с другим разделителем, не находится ни в отчёте,
    ни в индексе.
    """
    nested = tmp_path / "Photos" / "2024" / "Отпуск" / "IMG_0001.jpg"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"x")

    rel = rel_posix(nested, tmp_path)

    assert rel == "Photos/2024/Отпуск/IMG_0001.jpg"
    assert "\\" not in rel

    rebuilt = unflatten(rel)
    assert rebuilt.parts == ("Photos", "2024", "Отпуск", "IMG_0001.jpg")


def test_fonts_are_searched_and_found():
    """Список шрифтов не должен быть пустым и обязан находиться на macOS.

    Если не нашёлся ни один кандидат, подписи на контакт-листах рисуются
    заглушкой, и это должно падать здесь, а не выглядеть молча.
    """
    assert FONT_CANDIDATES, "список шрифтов пуст"
    assert all(p.startswith("/System") or p.startswith("/Library") for p in FONT_CANDIDATES), (
        "целевая платформа — macOS, кандидаты должны быть её путями"
    )
    assert _font_is_readable(), (
        "ни один шрифт не найден: подписи на контакт-листах будут нечитаемыми"
    )


def test_no_subprocess_in_runtime():
    """Сетевого и внешнего кода в рантайме быть не должно.

    Единственный выход наружу — `scripts/pull-model.sh`, и при сборке.
    `sips` был вторым таким местом: подстановка команды macOS, которая вне
    её молча возвращала пустоту.
    """
    import ast
    from pathlib import Path

    import kurator

    offenders: list[str] = []
    for path in Path(kurator.__file__).parent.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if "subprocess" in names:
                offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, f"subprocess в рантайме: {offenders}"


def test_file_times_falls_back_to_mtime(tmp_path):
    """Дата без EXIF берётся из системы и не пропадает.

    Раньше последним источником была макосовская `sips`, вне её
    возвращавшая `None`, и все файлы без EXIF теряли дату.
    """
    target = tmp_path / "no-exif.jpg"
    target.write_bytes(b"jpeg-ish")

    moment = file_times(target)

    assert moment.year >= 2020, "дата выглядит неправдоподобно"
    assert moment.tzinfo is None, "сравнение с mtime ждёт наивное время"

    # И indexer._fallback_date должен давать то же самое строкой.
    from kurator.indexer import _fallback_date

    as_text = _fallback_date(target)
    assert as_text is not None
    assert as_text.startswith(str(moment.year))


def test_pull_model_script_verifies_the_hash():
    """Скрипт скачивания весов обязан сверять sha256 всегда.

    Иначе несовпадение хеша молча проходит как «уже на месте», и веса
    оказываются битыми без единого слова.
    """
    from pathlib import Path

    import kurator

    script = Path(kurator.__file__).parent.parent.parent / "scripts" / "pull-model.sh"
    if not script.is_file():
        return  # в установленном пакете скрипта нет, это не ошибка

    text = script.read_text(encoding="utf-8")
    assert "shasum -a 256" in text, "скрипт не сверяет sha256"


def test_index_key_ignores_case(tmp_path):
    """Ключ индекса не должен зависеть от регистра имени.

    На APFS по умолчанию `IMG_1.JPG` и `img_1.jpg` — один и тот же файл:
    файловая система регистр не различает, второй файл туда просто не
    запишется. А индекс обязан различать, иначе при переиндексации оба
    ключа оказываются в `known_state` с одинаковым размером и mtime, и
    инкрементальная индексация объявляет «не изменился» сразу двум.

    Проверка на настоящем индексе, а не на строке: `rel_path UNIQUE` в
    SQLite регистрозависим, и это как раз то, что приходилось обходить.
    """
    from kurator.db import Photo, Store

    store = Store.open(tmp_path)
    # Один и тот же файл, записанный под разным регистром. На ФС, которая
    # регистр не различает, второй записи там просто не будет — а в индексе
    # она появится, если ключ регистрозависим.
    for name in ("IMG_1.JPG", "img_1.jpg"):
        store.upsert(Photo(rel_path=name, size=32, mtime=1000.0, md5="same"))
    store.commit()

    # Из двух записей остаётся одна: это один файл.
    rows = store.query("SELECT rel_path, rel_key FROM photos")
    assert len(rows) == 1, f"в индексе {len(rows)} записей на один файл: {[r['rel_path'] for r in rows]}"

    # known_state отдаёт регистронезависимый ключ — иначе инкрементальная
    # индексация не найдёт файл, переименованный только регистром.
    known = store.known_state()
    assert len(known) == 1
    (key,) = known
    assert key == key.casefold()
    assert key == "img_1.jpg"

    store.close()


def test_different_files_keep_separate_rows(tmp_path):
    """Схлопывание не должно пожирать разные файлы.

    Регрессия-страховка к тесту выше: если ключ сделать
    регистронезависимым слишком широко, `IMG_1.JPG` и `PHOTO.JPG` склеятся
    в одну запись и один снимок выпадет из индекса молча.
    """
    from kurator.db import Photo, Store

    store = Store.open(tmp_path)
    for name in ("IMG_1.JPG", "PHOTO.JPG", "img_2.jpg"):
        store.upsert(Photo(rel_path=name, size=10, mtime=1000.0, md5=f"md5-{name}"))
    store.commit()

    rows = store.query("SELECT rel_path FROM photos")
    assert len(rows) == 3, f"разные файлы схлопнулись: {[r['rel_path'] for r in rows]}"
    assert len(store.known_state()) == 3

    store.close()


def test_rel_path_keeps_original_spelling(tmp_path):
    """Ключ регистронезависимый, а показываемое имя — как на диске.

    Иначе в отчёте и в интерфейсе у человека окажется `img_1.jpg` вместо
    `IMG_1.JPG`, и он не найдёт файл тем именем, которым его называл сам.
    """
    from kurator.db import Photo, Store

    store = Store.open(tmp_path)
    target = tmp_path / "IMG_1.JPG"
    target.write_bytes(b"y" * 16)
    info = target.stat()
    store.upsert(
        Photo(rel_path="IMG_1.JPG", size=info.st_size, mtime=info.st_mtime, md5="m")
    )
    store.commit()

    (row,) = store.query("SELECT rel_path, rel_key FROM photos")

    assert row["rel_path"] == "IMG_1.JPG", "исходное написание потерялось"
    assert row["rel_key"] == "img_1.jpg"
    store.close()


def test_migration_fills_rel_key_on_old_database(tmp_path):
    """Старая база без `rel_key` должна доехать до рабочего состояния.

    `CREATE TABLE IF NOT EXISTS` не добавляет колонки в уже созданную
    таблицу, поэтому архив, проиндексированный до этой правки, вёл бы
    себя как схема без ключа. Миграция обязана и добавить колонку, и
    заполнить её, и схлопнуть записи, отличающиеся регистром.
    """
    import sqlite3

    from kurator.db import db_path

    db_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(tmp_path))
    # Схема в том виде, какой была до появления rel_key.
    conn.executescript(
        """
        CREATE TABLE photos (
            id INTEGER PRIMARY KEY,
            rel_path TEXT NOT NULL UNIQUE,
            size INTEGER NOT NULL,
            mtime REAL NOT NULL,
            md5 TEXT, phash TEXT, dhash TEXT, taken_at TEXT,
            width INTEGER, height INTEGER,
            embed BLOB, embed_model TEXT, embed_dim INTEGER,
            is_video INTEGER NOT NULL DEFAULT 0, indexed_at REAL
        );
        INSERT INTO photos (rel_path, size, mtime, md5)
        VALUES ('IMG_1.JPG', 10, 1000.0, 'a');
        INSERT INTO photos (rel_path, size, mtime, md5)
        VALUES ('img_1.jpg', 10, 1000.0, 'b');
        """
    )
    conn.commit()
    conn.close()

    from kurator.db import Store

    store = Store.open(tmp_path)

    columns = {r["name"] for r in store.conn.execute("PRAGMA table_info(photos)")}
    assert "rel_key" in columns, "миграция не добавила колонку"

    rows = store.query("SELECT rel_path, rel_key FROM photos")
    assert len(rows) == 1, f"записи не схлопнулись: {[r['rel_path'] for r in rows]}"
    assert rows[0]["rel_key"] == rows[0]["rel_path"].casefold()
    assert len(store.known_state()) == 1

    store.close()
