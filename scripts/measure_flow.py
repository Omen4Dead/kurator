#!/usr/bin/env python3
"""Следить за процессом во время работы и записать, сколько он съел.

Зачем: `evaluate` печатает метрики и молчит про расход памяти. Из-за
этого рост не замечали годами — на синтетике, где архив на 268 файлов,
матрицы расстояний съедают полмегабайта, и на живом архиве те же строки
съедают полтора гигабайта.

Этот скрипт нужен там, где одной итоговой цифры мало: нужен ход
индексации во времени, потому что пик может держаться полсекунды или
через полчаса. Среднее показывает одно, максимум — другое.

Запуск:

    uv run python scripts/measure_flow.py частичная -- .venv/bin/python -m kurator index /путь
    uv run python scripts/measure_flow.py полная -- \
        .venv/bin/python -m kurator index /путь --rehash

Пишет `out/measure-<метка>.json` с полным рядом наблюдений, а в конце
печатает сводку. Команда запускается как есть, её код возврата
пробрасывается.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "out"

INTERVAL = 2.0

# Проценты из ps приходят шумными и иногда с минусом, поэтому округляем
# и прижимаем снизу: отрицательная память в отчёте выглядит как ошибка.
def _sample(pid: int) -> tuple[int, float] | None:
    """RSS в килобайтах и загрузка в процентах. None — процесс умер."""
    try:
        result = subprocess.run(
            ["ps", "-o", "rss=,%cpu=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = result.stdout.strip()
    if not line:
        return None
    parts = line.split()
    if len(parts) < 2:
        return None
    try:
        rss = max(0, int(float(parts[0])))
        # Запятая, а не точка: ps печатает проценты в локали системы, и в
        # русской macOS это «12,5». Наивный float() это отвергал, и замер
        # молча не снимал ни одного образца.
        cpu = max(0.0, float(parts[1].replace(",", ".")))
    except ValueError:
        return None
    return rss, cpu


def _fmt(mb: float) -> str:
    return f"{mb:8.0f} МБ"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label", help="что измеряем: частичная, полная")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="команда после `--`")
    args = parser.parse_args()

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("нужна команда после `--`")

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    print(f"измеряю: {args.label}", flush=True)
    print(f"команда: {' '.join(command)}", flush=True)

    started = time.time()
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )

    # Вывод читается отдельным потоком, а замер идёт по своему таймеру.
    # Иначе образцы берутся только в моменты появления строк, и процесс,
    # который печатает редко, в измерение не попадает вовсе: первая версия
    # этого скрипта так и не сняла ни одного наблюдения.
    pending_lines: list[str] = []
    assert process.stdout is not None

    def reader() -> None:
        for line in process.stdout:
            pending_lines.append(line.rstrip())

    pump = threading.Thread(target=reader, daemon=True)
    pump.start()

    samples: list[dict] = []
    shown = 0

    def drain() -> None:
        nonlocal shown
        while shown < len(pending_lines):
            print(f"  | {pending_lines[shown][:70]}", flush=True)
            shown += 1

    while process.poll() is None:
        sample = _sample(process.pid)
        if sample is not None:
            rss, cpu = sample
            stamp = time.time() - started
            samples.append(
                {"t": round(stamp, 1), "rss_mb": round(rss / 1024, 1), "cpu": cpu}
            )
            print(
                f"  {stamp:6.0f} с  {_fmt(rss / 1024)}  cpu {cpu:5.1f}%",
                flush=True,
            )
        drain()
        time.sleep(INTERVAL)

    drain()
    code = process.wait()
    elapsed = time.time() - started

    if not samples:
        print(f"не удалось снять ни одного наблюдения (код возврата {code})")
        raise SystemExit(code)

    peak = max(s["rss_mb"] for s in samples)
    mean = sum(s["rss_mb"] for s in samples) / len(samples)
    final = samples[-1]["rss_mb"]
    peak_cpu = max(s["cpu"] for s in samples)

    OUT_DIR.mkdir(exist_ok=True)
    payload = {
        "label": args.label,
        "command": command,
        "returncode": code,
        "elapsed_s": round(elapsed, 1),
        "peak_rss_mb": peak,
        "mean_rss_mb": round(mean, 1),
        "final_rss_mb": final,
        "peak_cpu_percent": peak_cpu,
        "samples": len(samples),
        "series": samples,
    }
    path = OUT_DIR / f"measure-{args.label}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(f"=== {args.label} ===")
    print(f"  время         {elapsed:7.0f} с")
    print(f"  наблюдений    {len(samples):7d}")
    print(f"  пик памяти    {_fmt(peak)}")
    print(f"  среднее       {_fmt(mean)}")
    print(f"  в конце       {_fmt(final)}")
    print(f"  пик cpu       {peak_cpu:7.1f}%")
    print(f"  ряд           {path.relative_to(ROOT)}")
    print(f"  код возврата  {code}")

    # Хвост ряда печатаем отдельным списком: по нему видно, память утекает
    # или держится. Одна последняя цифра этого не показывает.
    print()
    print("  ход памяти, конец ряда:")
    for sample in samples[-(len(samples) // 8 or 1):]:
        print(f"    {sample['t']:6.0f} с  {_fmt(sample['rss_mb'])}  cpu {sample['cpu']:5.1f}%")

    raise SystemExit(code)


if __name__ == "__main__":
    main()