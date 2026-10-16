#!/usr/bin/env python3
"""Собрать иконку .ico из PNG для сборки под Windows.

Отдельный скрипт, а не разовая команда: .ico лежит в репозитории, и
кто-то рано или поздно поменяет логотип. Дальше иконка в Windows
перестанет обновляться молча, а заметят это пользователи — сборка
пройдёт, иконка останется прежней.

Запуск:

    uv run python scripts/make_ico.py           # собрать
    uv run python scripts/make_ico.py --check   # только проверить, ничего не меняя

`--check` нужен CI и сборке. Без него сборка на Windows перезаписывала
иконку молча, и в рабочей копии появлялось изменение, которого никто не
заметил, пока не дошло до коммита.

Windows ждёт несколько размеров в одном файле, иначе система растянет
256×256 на всю панель задач и зазубрится. Поэтому их набор, а не один.
"""

from __future__ import annotations

import argparse
import io
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "assets" / "icon-1024.png"
TARGET = ROOT / "assets" / "kurator.ico"

# Windows использует эти размеры сам, без нашего участия, поэтому
# набора из одного большого недостаточно.
SIZES = (16, 24, 32, 48, 64, 128, 256)


def build() -> bytes:
    """Собрать иконку и вернуть её байтами, ничего не записывая."""
    if not SOURCE.is_file():
        raise SystemExit(f"нет исходника: {SOURCE}")

    image = Image.open(SOURCE).convert("RGBA")
    if image.size != (1024, 1024):
        raise SystemExit(f"исходник {image.size}, ожидался (1024, 1024)")

    # Достаточно одного `sizes=` на исходнике целиком: PIL сам нарежет кадры.
    # `append_images` здесь не просто не нужен, а вреден — с ним файл
    # получается в 723 байта с единственным размером 16×16, потому что
    # запись берёт первый кадр и на этом заканчивает. Проверено на
    # PIL 12.3: без append_images — 41 КБ и все семь размеров.
    buffer = io.BytesIO()
    image.save(buffer, format="ICO", sizes=[(size, size) for size in SIZES])
    return buffer.getvalue()


def pixels(data: bytes) -> dict[tuple[int, int], bytes]:
    """Развернуть ICO в пиксели по каждому размеру.

    Сравнивать байты файла нельзя: один и тот же Pillow на macOS и на
    Linux кодирует ICO по-разному — 41802 байта против 42078 при
    одинаковых картинках. Кодировщик зависит от системы, а не от входа.
    Поэтому сверяется то, что действительно важно: сами пиксели.
    """
    out: dict[tuple[int, int], bytes] = {}
    with Image.open(io.BytesIO(data)) as image:
        for size in sorted(image.info.get("sizes", [])):
            image.size = size
            image.load()
            out[size] = image.convert("RGBA").tobytes()
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="только проверить, что закоммиченная иконка свежая",
    )
    args = parser.parse_args()

    fresh = build()

    if args.check:
        if not TARGET.is_file():
            raise SystemExit(
                f"нет {TARGET.relative_to(ROOT)}. Собери: uv run python scripts/make_ico.py"
            )
        committed = pixels(TARGET.read_bytes())
        expected = pixels(fresh)

        if committed == expected:
            print(f"иконка свежая, размеры: {sorted(committed)}")
            return

        only_old = sorted(set(committed) - set(expected))
        only_new = sorted(set(expected) - set(committed))
        differing = sorted(
            size
            for size in set(committed) & set(expected)
            if committed[size] != expected[size]
        )
        raise SystemExit(
            f"иконка {TARGET.relative_to(ROOT)} разошлась с {SOURCE.name}.\n"
            f"  размеров только в репозитории: {only_old}\n"
            f"  размеров только в сборке:     {only_new}\n"
            f"  отличаются пиксели:           {differing}\n"
            f"  Обнови: uv run python scripts/make_ico.py"
        )

    TARGET.write_bytes(fresh)
    check = Image.open(TARGET)
    print(f"готово: {TARGET.relative_to(ROOT)}")
    print(f"размеры: {sorted(check.info.get('sizes', []))}")
    print(f"весит: {TARGET.stat().st_size / 1024:.1f} КБ")


if __name__ == "__main__":
    main()