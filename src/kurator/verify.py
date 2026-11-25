"""Верификация содержимого пары снимков после грубого выравнивания.

Второй ярус near (1.13): эмбеддинг уверенно говорит «похожи», но не
различает «тот же снимок, деформирован» и «та же композиция, другое
содержимое» (ловушки эталона доходят по косинусу до 0.95). Здесь пара
сравнивается попиксельно: поле мелкозернистого шума, которое
генератор эталона кладёт детерминированно от seed и которое деформации
(поворот, кроп, рамка, пережатие) сохраняют, у чужих снимков своё.
Эмбеддинг такого сигнала не видит, глаз — тоже; скоррелированный
high-pass — видит.

Сигналы (пороги в config.py, разведка scripts/probe_hp.py):
  - corr: корреляция high-pass карт 64x64, максимум по 4 поворотам,
    плотным масштабам (0.55-1.0) и сдвигам (±4 px): кроп и рамка меняют
    масштаб и фазу сетки, без поиска корреляция разваливается;
  - color: пересечение RGB-гистограмм (8x8x8 бинов): ловушки с той же
    геометрией, но другой палитрой режутся здесь;
  - mean_color: евклидово расстояние средних RGB по центральному окну
    75% (рамка и полосы скриншота живут по краям и средний цвет
    полного кадра портят).

Читаемость файлов: верификатор работает только с парами-кандидатами
(сотни, не тысячи), чтение двух картинок на пару приемлемо.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from .config import (
    NEAR_TIER2_COLOR,
    NEAR_TIER2_CORR,
    NEAR_TIER2_MEAN_COLOR,
)

SIZE = 64
SCALES = (1.0, 0.95, 0.9, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6, 0.55)
SHIFTS = (-4, -2, 0, 2, 4)
_KERNEL = np.ones((5, 5), dtype=np.float32) / 25.0


def _box_blur(g: np.ndarray) -> np.ndarray:
    padded = np.pad(g, 2, mode="edge")
    out = np.zeros_like(g)
    for dy in range(5):
        for dx in range(5):
            out += _KERNEL[dy, dx] * padded[dy : dy + g.shape[0], dx : dx + g.shape[1]]
    return out


def _resize(a: np.ndarray, m: int) -> np.ndarray:
    return np.asarray(
        Image.fromarray(a.astype(np.float32)).resize((m, m), Image.BILINEAR)
    )


def _gray(path: Path) -> np.ndarray | None:
    try:
        with Image.open(path) as img:
            return np.asarray(
                img.convert("L").resize((SIZE, SIZE), Image.BILINEAR), dtype=np.float32
            )
    except Exception:
        return None


def _hist(path: Path) -> np.ndarray | None:
    try:
        with Image.open(path) as img:
            rgb = np.asarray(img.convert("RGB").resize((SIZE, SIZE), Image.BILINEAR))
    except Exception:
        return None
    q = (rgb // 32).reshape(-1, 3)
    h = np.zeros(512, dtype=np.float32)
    np.add.at(h, q[:, 0] * 64 + q[:, 1] * 8 + q[:, 2], 1.0)
    return h / max(1.0, h.sum())


def _mean_rgb_center(path: Path) -> np.ndarray | None:
    try:
        with Image.open(path) as img:
            rgb = np.asarray(
                img.convert("RGB").resize((SIZE, SIZE), Image.BILINEAR), dtype=np.float32
            )
    except Exception:
        return None
    off = SIZE // 8
    return rgb[off : SIZE - off, off : SIZE - off].reshape(-1, 3).mean(axis=0)


def best_corr(ha: np.ndarray, hb: np.ndarray) -> float:
    """Максимум high-pass корреляции по поворотам, масштабам и сдвигам."""
    ga, gb = ha.reshape(SIZE, SIZE), hb.reshape(SIZE, SIZE)
    best = -1.0
    for rot in range(4):
        b = np.rot90(gb, rot)
        bz = b.ravel() / (np.linalg.norm(b) or 1.0)
        az = ga.ravel() / (np.linalg.norm(ga) or 1.0)
        for sc in SCALES:
            m = max(8, int(round(SIZE * sc)))
            for sy in SHIFTS:
                for sx in SHIFTS:
                    y0 = (SIZE - m) // 2 + sy
                    x0 = (SIZE - m) // 2 + sx
                    if y0 < 0 or x0 < 0 or y0 + m > SIZE or x0 + m > SIZE:
                        continue
                    win = _resize(ga[y0 : y0 + m, x0 : x0 + m], SIZE)
                    wz = win.ravel() / (np.linalg.norm(win) or 1.0)
                    best = max(best, float(wz @ bz))
                    win = _resize(b[y0 : y0 + m, x0 : x0 + m], SIZE)
                    wz = win.ravel() / (np.linalg.norm(win) or 1.0)
                    best = max(best, float(wz @ az))
    return best


def pair_match(root: Path, rel_a: str, rel_b: str) -> bool:
    """True, если пара выдерживает верификацию содержимого.

    Порядок проверок от дешёвой к дорогой: средний цвет и гистограмма —
    миллисекунды, корреляция с поиском — только для их выживших.
    """
    path_a, path_b = root / rel_a, root / rel_b
    ha, hb = _gray(path_a), _gray(path_b)
    if ha is None or hb is None:
        return False
    ma, mb = _mean_rgb_center(path_a), _mean_rgb_center(path_b)
    if ma is None or mb is None:
        return False
    if float(np.linalg.norm(ma - mb)) > NEAR_TIER2_MEAN_COLOR:
        return False
    hista, histb = _hist(path_a), _hist(path_b)
    if hista is None or histb is None:
        return False
    if float(np.minimum(hista, histb).sum()) < NEAR_TIER2_COLOR:
        return False
    return best_corr(ha, hb) >= NEAR_TIER2_CORR
