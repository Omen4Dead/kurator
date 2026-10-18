"""Загрузка изображений, EXIF и препроцессинг под SigLIP."""

from __future__ import annotations

import datetime as dt
import io
from pathlib import Path

import numpy as np
from PIL import ExifTags, Image, ImageOps

from .config import INPUT_SIZE, SIGLIP_MEAN, SIGLIP_STD
from .hashing import DCT_SIZE, DHASH_H, DHASH_W, dhash, phash

Image.MAX_IMAGE_PIXELS = 400_000_000


class BadImage(Exception):
    pass


def _register_heif() -> None:
    """Включить чтение HEIF/AVIF.

    Регистрация делается один раз при импорте и молча пропускается, если
    библиотека не встала: kurator не должен отказываться запускаться
    из-за формата, которого у пользователя может и не быть. Ценой такой
    осторожности будет то, что снимки с айфона не читаются, — и ловит
    это тест в `tests/test_formats.py`, а не сам kurator.
    """
    try:
        import pillow_heif
    except ImportError:
        return
    pillow_heif.register_heif_opener()


_register_heif()


def open_rgb(path: Path) -> Image.Image:
    try:
        with Image.open(path) as img:
            img.load()
            return ImageOps.exif_transpose(img).convert("RGB")
    except Exception as exc:
        raise BadImage(str(exc)) from exc


def _exif_from(img: Image.Image) -> dict:
    exif = img.getexif()
    out = {
        "taken_at": None,
        "orientation": int(exif.get(274, 1) or 1),
    }
    # Дата съёмки лежит в под-IFD Exif, а Pillow заходит туда сам только
    # для JPEG. Для HEIF не заходит: у файла с айфона в верхнем уровне
    # есть Make и Model, а DateTimeOriginal — нет, и снимок получал дату
    # по времени копирования файла вместо времени съёмки. На
    # синхронизируемом архиве это время не значит ничего, а от него
    # зависят burst-серии и порядок в отчёте.
    try:
        sub = exif.get_ifd(ExifTags.IFD.Exif)
    except Exception:
        sub = {}
    dt_orig = exif.get(36867) or sub.get(36867)
    dt_dig = exif.get(36868) or sub.get(36868)
    # 306 — DateTime в главном IFD; часть камер пишет дату только туда.
    out["taken_at"] = (
        _parse_exif_date(dt_orig)
        or _parse_exif_date(dt_dig)
        or _parse_exif_date(exif.get(306))
    )
    return out


def load_for_analysis(path: Path) -> tuple[Image.Image, int, int, str | None]:
    """Одно чтение файла на всё: размеры, EXIF, кадр для эмбеддинга.

    Файл читается в память один раз: на внешнем диске повторное чтение
    10-мегабайтного снимка стоит дороже всего остального вместе взятого.

    Для JPEG вызывается draft: libjpeg декодирует сразу в уменьшенном
    размере. На 12-мегапиксельном снимке это 4000×3000 → 500×375 вместо
    полного кадра, а нужно всё равно 224×224.
    """
    try:
        # Файл отдаётся Pillow путём, а не читается целиком в память.
        # Раньше здесь был `Image.open(io.BytesIO(path.read_bytes()))` —
        # файл читался в байты и тут же копировался в BytesIO, то есть
        # дважды. На живом архиве это и давало рост памяти: пик индексации
        # доходил до 1507 МБ, из них 313 МБ давал один только JPEG на 250
        # файлов. Сейчас тот же замер — 43 МБ, и на треть быстрее.
        #
        # Повторного чтения при этом не появляется: JPEG декодируется
        # последовательно, и `draft` ниже всё равно читает файл один раз.
        with Image.open(path) as img:
            width, height = img.size
            exif = _exif_from(img)
            if 5 <= exif["orientation"] <= 8:
                width, height = height, width
            before_draft = max(img.size)
            img.draft("RGB", (INPUT_SIZE, INPUT_SIZE))
            # draft уменьшает кадр силами кодека, но не везде: JPEG умеет,
            # а TIFF — нет, и RAW остаётся полным. Тогда `convert` ниже
            # сделал бы вторую копию кадра: на CR2 это 240 МБ пика.
            #
            # Условие — «draft не сработал», а не «кадр большой», и это
            # важно: у JPEG путь остаётся побитово прежним, и эмбеддинги
            # не меняются вообще. Проверено на 80 снимках: расхождение
            # косинуса 0.028 возникало именно от лишней передискретизации
            # JPEG, а не от самого масштабирования.
            draft_worked = max(img.size) < before_draft
            img = ImageOps.exif_transpose(img)
            if not draft_worked and max(img.size) > INPUT_SIZE * 2:
                img.thumbnail((INPUT_SIZE * 2, INPUT_SIZE * 2), Image.BICUBIC)
            rgb = img.convert("RGB")
            return rgb, width, height, exif["taken_at"]
    except Exception as exc:
        raise BadImage(str(exc)) from exc


def read_exif(path: Path) -> dict:
    out: dict = {
        "taken_at": None,
        "make": None,
        "model": None,
        "orientation": 1,
    }
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            out["orientation"] = int(exif.get(274, 1) or 1)
            out["make"] = _clean(exif.get(271))
            out["model"] = _clean(exif.get(272))
            dt_orig = exif.get(36867)
            dt_dig = exif.get(36868)
            out["taken_at"] = _parse_exif_date(dt_orig) or _parse_exif_date(dt_dig)
    except Exception:
        return out
    return out


def _clean(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    value = str(value).strip().strip("\x00").strip()
    return value or None


def _parse_exif_date(value) -> str | None:
    if not value:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = str(value).strip().rstrip("\x00")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d"):
        try:
            parsed = dt.datetime.strptime(text[: len(fmt) + 2].strip(), fmt)
            return parsed.isoformat(timespec="seconds")
        except ValueError:
            continue
    return None


def file_times(path: Path) -> dt.datetime:
    """Когда файл создан и когда изменён — по тому, что знает система.

    Раньше последним источником даты служила макосовская `sips`: она
    сообщает дату импорта, которая на других системах недоступна вовсе,
    и там функция молча возвращала `None`. Файл без EXIF терял дату целиком,
    индекс не жаловался, а отчёт в колонке «дата» врал.

    `st_birthtime` есть на macOS и BSD, на Linux — с Python 3.12, на Windows
    — с 3.13. Где его нет, остаётся `st_mtime`, и это честнее, чем ничего.
    """
    try:
        info = path.stat()
    except OSError:
        return dt.datetime.fromtimestamp(0)
    birth = getattr(info, "st_birthtime", None)
    return dt.datetime.fromtimestamp(birth if birth is not None else info.st_mtime)


def to_siglip_batch(img: Image.Image) -> np.ndarray:
    side = INPUT_SIZE
    w, h = img.size
    scale = side / min(w, h)
    resized = img.resize((max(side, round(w * scale)), max(side, round(h * scale))), Image.BICUBIC)
    w, h = resized.size
    left = (w - side) // 2
    top = (h - side) // 2
    crop = resized.crop((left, top, left + side, top + side))
    arr = np.asarray(crop, dtype=np.float32) / 255.0
    arr = (arr - SIGLIP_MEAN) / SIGLIP_STD
    return np.transpose(arr, (2, 0, 1))


def hash_views(img: Image.Image) -> tuple[int, int]:
    gray = img.convert("L")
    dct = np.asarray(gray.resize((DCT_SIZE, DCT_SIZE), Image.LANCZOS))
    grad = np.asarray(gray.resize((DHASH_W, DHASH_H), Image.LANCZOS))
    return phash(dct), dhash(grad)


def l2_normalize(matrix: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(matrix, axis=1, keepdims=True)
    norm[norm == 0] = 1.0
    return matrix / norm