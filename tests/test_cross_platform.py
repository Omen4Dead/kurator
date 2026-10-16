"""Проверки, которых раньше не было: платформенная независимость.

Всё это проверяется на любой системе, хотя предмет правок — Windows,
Linux и поведение на медленном DNS. Локально такие поломки не видны:
на macOS `sips` есть, разделитель пути один, а резолвер отвечает мгновенно.
"""

from __future__ import annotations

from pathlib import PureWindowsPath

from kurator.config import rel_posix, unflatten
from kurator.contact import FONT_CANDIDATES, _font_is_readable
from kurator.imaging import file_times


def test_relative_paths_are_posix_on_every_platform(tmp_path):
    """Путь в индексе всегда через «/», независимо от системы.

    `str(path.relative_to(root))` на Windows даёт обратные слэши, и дальше
    путь режется через `split("/")` в отчёте и в `app.js`. Результат —
    пустые заголовки и битые ссылки на превью.
    """
    windows_root = PureWindowsPath("D:/Photos")
    windows_rel = PureWindowsPath("D:/Photos/Отпуск/Барселона/IMG_0001.jpg")

    # Смысл проверки — в разделителе: `relative_to` срезает общий префикс,
    # а `as_posix` приводит остаток к прямому слэшу независимо от системы.
    rel = windows_rel.relative_to(windows_root).as_posix()

    assert rel == "Отпуск/Барселона/IMG_0001.jpg"
    assert "\\" not in rel

    # Обратно разворачивается в Path текущей системы.
    rebuilt = unflatten(rel)
    assert rebuilt.parts == ("Отпуск", "Барселона", "IMG_0001.jpg")
    assert len(rebuilt.parts) == 3, "разделитель не развернулся в части пути"


def test_rel_posix_matches_as_posix(tmp_path):
    """Помощник совпадает с as_posix на вложенных путях."""
    nested = tmp_path / "Photos" / "2024" / "Отпуск" / "IMG_0001.jpg"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"x")

    rel = rel_posix(nested, tmp_path)

    assert rel == "Photos/2024/Отпуск/IMG_0001.jpg"
    assert "\\" not in rel


def test_fonts_are_searched_on_all_platforms():
    """Список шрифтов не должен состоять из одних макосовских путей.

    Раньше это были три пути в `/System/Library/Fonts`, и на любой другой
    системе список оказывался пустым — контакт-листы рисовались
    нечитаемым растром.
    """
    assert FONT_CANDIDATES, "список шрифтов пуст"

    macos = [p for p in FONT_CANDIDATES if p.startswith("/System")]
    windows = [p for p in FONT_CANDIDATES if p.startswith("C:/")]
    linux = [p for p in FONT_CANDIDATES if p.startswith("/usr/share/fonts")]

    assert macos, "нет кандидатов на macOS"
    assert windows, "нет кандидатов на Windows"
    assert linux, "нет кандидатов на Linux"

    # CI гоняет этот файл на всех трёх системах, и на каждой должен
    # находиться хотя бы один шрифт: если нет, подписи рисуются заглушкой,
    # и это надо чтобы падало здесь, а не выглядело молча.
    assert _font_is_readable(), (
        "ни один шрифт не найден: подписи на контакт-листах будут "
        "нечитаемыми. Поставить DejaVuSans или Noto Sans."
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


def test_pull_model_script_has_no_macos_only_tools():
    """Скрипт скачивания весов должен работать и на Linux.

    `shasum` есть на macOS, но на ubuntu может отсутствовать, и проверка
    хеша молча проходила бы как «уже на месте».
    """
    from pathlib import Path

    import kurator

    script = Path(kurator.__file__).parent.parent.parent / "scripts" / "pull-model.sh"
    if not script.is_file():
        return  # в установленном пакете скрипта нет, это не ошибка

    text = script.read_text(encoding="utf-8")
    assert "sha256sum" in text, "нет запасного пути без shasum"
    # `shasum` остаётся как запасной вариант для macOS, поэтому само по
    # себе упоминание не считается проблемой.

    # Скрипт должен оставаться исполняемым: иначе `bash pull-model.sh`
    # придётся писать руками, а в CI он зовётся напрямую.
    assert script.stat().st_mode & 0o111, "скрипт потерял бит исполнения"