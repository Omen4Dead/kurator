#!/usr/bin/env python3
"""Померить пик памяти при чтении снимка одним способом.

Повод: на живом архиве индексация доходила до 1507 МБ, и причина оказалась
не в матрицах расстояний, а в том, как читается файл. Приходилось мерить
все варианты.

Почему по одному варианту на процесс: пик RSS у процесса не убывает.
Прогнать четыре способа подряд в одном интерпретаторе нельзя — все четыре
покажут пик самого тяжёлого. Запускается так:

    for v in read_bytes open_path thumbnail reduce; do
        uv run python scripts/bench_read.py "$v" "/путь/к/архиву" 150
    done
"""

from __future__ import annotations

import gc
import io
import resource
import sys
import time
from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kurator.config import IMAGE_EXTENSIONS  # noqa: E402

SIDE = 224


def peak_mb() -> float:
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / (1024 * 1024) if sys.platform == "darwin" else raw / 1024


def read_bytes_variant(path: Path):
    """Как было: файл целиком в память, потом ещё раз в BytesIO."""
    with Image.open(io.BytesIO(path.read_bytes())) as img:
        img.draft("RGB", (SIDE, SIDE))
        return ImageOps.exif_transpose(img).convert("RGB")


def open_path(path: Path):
    """Pillow читает сам: копии файла в памяти нет."""
    with Image.open(path) as img:
        img.draft("RGB", (SIDE, SIDE))
        return ImageOps.exif_transpose(img).convert("RGB")


def thumbnail(path: Path):
    """draft плюс thumbnail — уменьшает кадр и там, где draft не сработал."""
    with Image.open(path) as img:
        img.draft("RGB", (SIDE, SIDE))
        img = ImageOps.exif_transpose(img)
        img.thumbnail((SIDE, SIDE), Image.BICUBIC)
        return img.convert("RGB")


def reduce_variant(path: Path):
    """draft, а где он не работает — reduce до уменьшенного размера."""
    with Image.open(path) as img:
        img.draft("RGB", (SIDE, SIDE))
        img = ImageOps.exif_transpose(img)
        big = max(img.size)
        if big > SIDE * 8:
            img = img.reduce(max(1, big // (SIDE * 8)))
        return img.convert("RGB")


VARIANTS = {
    "read_bytes": read_bytes_variant,
    "open_path": open_path,
    "thumbnail": thumbnail,
    "reduce": reduce_variant,
}


def collect(root: Path, limit: int) -> list[Path]:
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if len(found) >= limit:
            break
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
            found.append(path)
    return found


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(
            "запуск: bench_read.py вариант /путь [сколько]\n"
            f"варианты: {', '.join(VARIANTS)}"
        )
    name = sys.argv[1]
    if name not in VARIANTS:
        raise SystemExit(f"неизвестный вариант {name}; есть: {', '.join(VARIANTS)}")

    root = Path(sys.argv[2])
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 150
    files = collect(root, limit)
    if not files:
        raise SystemExit(f"не нашёл изображений под {root}")

    counts: dict[str, int] = {}
    for path in files:
        counts[path.suffix.lower()] = counts.get(path.suffix.lower(), 0) + 1

    func = VARIANTS[name]
    gc.collect()
    started = time.time()
    size = "—"
    for path in files:
        img = func(path)
        size = f"{img.size[0]}×{img.size[1]}"
        img.close()
    took = time.time() - started

    print(
        f"{name:<10} {len(files):>4} файлов {str(counts):<28} "
        f"{took:6.1f} с   кадр {size:<11} пик {peak_mb():6.0f} МБ"
    )


if __name__ == "__main__":
    main()