"""Верификация содержимого пары снимков после грубого выравнивания.

Второй ярус near (1.13/1.14): эмбеддинг уверенно говорит «похожи», но
не различает «тот же снимок, деформирован» и «та же композиция, другое
содержимое» (ловушки эталона доходят по косинусу до 0.965). Здесь пара
сравнивается попиксельно: у деформированного снимка совпадает сама
сцена, у чужого — только композиция.

Сигналы (пороги в config.py, разведка scripts/explore_color_diff.py на
эталоне 2026-11-26):
  - color: пересечение RGB-гистограмм (8x8x8 бинов). Дешёвый фильтр,
    убивает ловушки с другой палитрой; проверяется первым;
  - corr: корреляция яркостных карт 64x64, максимум по 4 поворотам,
    плотным масштабам (0.55-1.0) и ВСЕМ позициям окна (FFT-скользящая
    корреляция). Кроп и рамка меняют масштаб и сдвиг содержимого:
    поиск только вокруг центра (±4 px) терял кропы генератора,
    снимающие 10-30% с краёв. High-pass СЮДА НЕ ВОЗВРАЩАТЬ: по нему
    корреляция реальных пар падает до 0.33-0.71, и рамки с белой каймой
    не выживают;
  - color_diff: среднее |ΔRGB| в точке ЛУЧШЕГО выравнивания. Матчер
    находит, где кадры совпадают геометрически; если там не совпадает
    цвет — это чужая сцена с похожей композицией. Глобальный средний
    цвет тут не работал: кроп меняет доли площадей и сдвигал среднее
    у настоящих пар (измерено: 3 кропа из 8 умирали на нём).

Производительность: на живом архиве ярус 2 получает тысячи пар
(на эталоне 5916 при cosine ≥ 0.91), поэтому файлы декодируются один
раз и кешируются по (mtime, size) — тот же контракт инкрементальности,
что у индексатора; гистограмма проверяется до запуска матчера и
срезает ~94% кандидатов.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from .config import (
    NEAR_TIER2_COLOR,
    NEAR_TIER2_COLOR_DIFF,
    NEAR_TIER2_CORR,
)

SIZE = 64
SCALES = (1.0, 0.95, 0.9, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6, 0.55)

# (side, rot, y0, x0, m) — как окно одной стороны совмещено с полным
# кадром другой: side чьё окно, rot поворот ПОЛНОГО кадра.
Alignment = tuple[str, int, int, int, int]

# path -> ((mtime, size), hist, gray64, rgb64). Кеш живёт процесс —
# analyze и тесты декодируют каждый файл один раз, не на каждую пару.
_FEATURES: dict[str, tuple[tuple[float, int], np.ndarray, np.ndarray, np.ndarray]] = {}
#            path    (mtime, size)      hist       gray64    rgb64


def _resize(a: np.ndarray, m: int) -> np.ndarray:
    return np.asarray(
        Image.fromarray(a.astype(np.float32)).resize((m, m), Image.BILINEAR)
    )


def _resize_rgb(a: np.ndarray, m: int) -> np.ndarray:
    # PIL не принимает (H, W, 3) float32 — ресайзим каналы как 2D 'F'.
    return np.stack(
        [
            np.asarray(
                Image.fromarray(a[..., c].astype(np.float32)).resize(
                    (m, m), Image.BILINEAR
                )
            )
            for c in range(a.shape[2])
        ],
        axis=-1,
    )


def _features(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """(hist, gray64, rgb64) — с кешем по (mtime, size).

    None, если файл не читается. Правило совпадения ключа — как у
    индексатора: неизменные size+mtime означают неизменный файл.
    """
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        return None
    hit = _FEATURES.get(key)
    if hit is not None and hit[0] == (st.st_mtime, st.st_size):
        return hit[1], hit[2], hit[3]
    try:
        with Image.open(path) as img:
            rgb64 = img.convert("RGB").resize((SIZE, SIZE), Image.BILINEAR)
            rgb = np.asarray(rgb64, dtype=np.float32)
            gray = np.asarray(rgb64.convert("L"), dtype=np.float32)
    except Exception:
        return None
    q = (rgb.astype(np.uint8) // 32).reshape(-1, 3)
    hist = np.zeros(512, dtype=np.float32)
    np.add.at(hist, q[:, 0] * 64 + q[:, 1] * 8 + q[:, 2], 1.0)
    hist /= max(1.0, hist.sum())
    if len(_FEATURES) > 8192:
        _FEATURES.clear()
    _FEATURES[key] = ((st.st_mtime, st.st_size), hist, gray, rgb)
    return hist, gray, rgb


def _integral(img: np.ndarray) -> np.ndarray:
    # Интегральное изображение img² с нулевой первой строкой/столбцом.
    return np.pad(img * img, ((1, 0), (1, 0))).cumsum(0).cumsum(1)


def _win_sumsq(integ: np.ndarray, m: int) -> np.ndarray:
    # Сумма квадратов в каждом окне m×m: результат (SIZE-m+1)².
    return integ[m:, m:] - integ[:-m, m:] - integ[m:, :-m] + integ[:-m, :-m]


def _sliding_corr(
    img_fft: np.ndarray, integ: np.ndarray, ker: np.ndarray, m: int
) -> np.ndarray:
    """Нормированная корреляция окна m×м со всеми позициями img.

    scores[y, x] = <img[y:y+m, x:x+m], ker> / (||окно||·||ker||),
    y, x ∈ [0, SIZE-m]. Считается одним FFT-произведением, поэтому
    полный перебор позиций стоит столько же, сколько одна сверка.
    """
    ker_pad = np.zeros((SIZE, SIZE), dtype=np.float32)
    ker_pad[:m, :m] = ker
    scores = np.fft.ifft2(img_fft * np.conj(np.fft.fft2(ker_pad))).real[
        : SIZE - m + 1, : SIZE - m + 1
    ]
    energy = _win_sumsq(integ, m) * max(float((ker * ker).sum()), 1e-12)
    return scores / np.sqrt(np.maximum(energy, 1e-12))


def best_align(ga: np.ndarray, gb: np.ndarray) -> tuple[float, Alignment]:
    """Максимум корреляции по поворотам, масштабам и позициям.

    Позиция окна ищется по ВСЕМ допустимым смещениям: кроп генератора
    снимает случайные 10-30% с краёв, и окно может лежать на ±10 px от
    центра. Возвращает корреляцию и выравнивание, на котором она
    достигнута: color_diff проверяет цвет именно там.
    """
    best, align = -1.0, ("Awin", 0, 0, 0, SIZE)
    fft_a = np.fft.fft2(ga)
    int_a = _integral(ga)
    for rot in range(4):
        b = np.rot90(gb, rot)
        fft_b = np.fft.fft2(b)
        int_b = _integral(b)
        for sc in SCALES:
            m = max(8, int(round(SIZE * sc)))
            # Awin: окно из A против уменьшенного B; Bwin — наоборот.
            scores = _sliding_corr(fft_a, int_a, _resize(b, m), m)
            idx = int(np.argmax(scores))
            c = float(scores.ravel()[idx])
            if c > best:
                best = c
                y0, x0 = divmod(idx, scores.shape[1])
                align = ("Awin", rot, int(y0), int(x0), m)
            scores = _sliding_corr(fft_b, int_b, _resize(ga, m), m)
            idx = int(np.argmax(scores))
            c = float(scores.ravel()[idx])
            if c > best:
                best = c
                y0, x0 = divmod(idx, scores.shape[1])
                align = ("Bwin", rot, int(y0), int(x0), m)
    return best, align


def color_diff(ra: np.ndarray, rb: np.ndarray, align: Alignment) -> float:
    """Среднее |ΔRGB| в точке выравнивания (окно одной стороны против
    полного кадра другой)."""
    side, rot, y0, x0, m = align
    # Окно — из той стороны, которую назвал матчер (Awin: окно из A,
    # полный кадр — повёрнутый B; Bwin — зеркально).
    if side == "Awin":
        win_src, full = ra, np.rot90(rb, rot)
    else:
        win_src, full = np.rot90(rb, rot), ra
    win = _resize_rgb(win_src[y0 : y0 + m, x0 : x0 + m], SIZE)
    return float(np.abs(win - full).mean())


def pair_signals(root: Path, rel_a: str, rel_b: str) -> tuple[float, float, float] | None:
    """(corr, color_hist, color_diff) — для разведки и тестов.

    None, если хотя бы один файл не читается.
    """
    fa = _features(root / rel_a)
    fb = _features(root / rel_b)
    if fa is None or fb is None:
        return None
    hista, ga, ra = fa
    histb, gb, rb = fb
    corr, align = best_align(ga, gb)
    hist = float(np.minimum(hista, histb).sum())
    cdiff = color_diff(ra, rb, align)
    return corr, hist, cdiff


def pair_match(root: Path, rel_a: str, rel_b: str) -> bool:
    """True, если пара выдерживает верификацию содержимого.

    Порядок проверок от дешёвой к дорогой: гистограмма — по кешу
    фич миллисекунды, матчер с поиском — только для её выживших.
    """
    fa = _features(root / rel_a)
    fb = _features(root / rel_b)
    if fa is None or fb is None:
        return False
    hista, ga, ra = fa
    histb, gb, rb = fb
    if float(np.minimum(hista, histb).sum()) < NEAR_TIER2_COLOR:
        return False
    corr, align = best_align(ga, gb)
    if corr < NEAR_TIER2_CORR:
        return False
    return color_diff(ra, rb, align) <= NEAR_TIER2_COLOR_DIFF
