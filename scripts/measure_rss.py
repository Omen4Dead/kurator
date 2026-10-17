#!/usr/bin/env python3
"""Напечатать пик памяти команды.

Зачем это в CI: `evaluate` печатает метрики и молчит про расход памяти.
Из-за этого рост не замечали годами — на синтетике, где архив маленький,
матрицы расстояний съедают полмегабайта, и на живом архиве те же самые
строки съедают полтора гигабайта. Никто это не считал.

Число нужно не «больше или меньше», а конкретное и сравнимое между
прогонами, поэтому печатаем его рядом с этапом, а не «после всего».

Использование — оборачиваем команду, а не меряем сами себя:

    uv run python scripts/measure_rss.py индекс -- uv run kurator index /tmp/архив

Команда после `--` выполняется как есть, её код возврата пробрасывается,
а строка с памятью печатается в конце независимо от успеха. Печатается
в stderr, чтобы не мешать разбирать stdout команды.
"""

from __future__ import annotations

import argparse
import resource
import subprocess
import sys
from pathlib import Path


def peak_mb() -> float:
    """Пик памяти этого процесса и его детей, в мегабайтах.

    На macOS `ru_maxrss` в байтах, на Linux в килобайтах. Разница в
    тысячу раз — и её легко не заметить, если просто поделить на 1024.
    """
    scale = (1024 * 1024) if sys.platform == "darwin" else 1024
    own = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    kids = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return max(own, kids) / scale


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label", help="что измеряем: индекс, разбор, отчёт")
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="команда для замера, после `--`",
    )
    args = parser.parse_args()

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("нужна команда после `--`, например: "
                     "measure_rss.py индекс -- uv run kurator index /tmp/архив")

    code = subprocess.call(command)
    print(
        f"::memory:: {args.label}: {peak_mb():.0f} МБ (пик RSS)",
        file=sys.stderr,
    )
    raise SystemExit(code)


if __name__ == "__main__":
    main()