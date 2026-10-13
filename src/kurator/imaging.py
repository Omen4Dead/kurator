"""Загрузка изображений, EXIF и препроцессинг под SigLIP."""

from __future__ import annotations

import datetime as dt
import io
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from .config import INPUT_SIZE, SIGLIP_MEAN, SIGLIP_STD
from .hashing import DCT_SIZE, DHASH_H, DHASH_W, dhash, phash

Image.MAX_IMAGE_PIXELS = 400_000_000


class BadImage(Exception):
    pass


def _register_heif() -> None:
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
    dt_orig = exif.get(36867)
    dt_dig = exif.get(36868)
    out["taken_at"] = _parse_exif_date(dt_orig) or _parse_exif_date(dt_dig)
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
        with Image.open(io.BytesIO(path.read_bytes())) as img:
            width, height = img.size
            exif = _exif_from(img)
            if 5 <= exif["orientation"] <= 8:
                width, height = height, width
            img.draft("RGB", (INPUT_SIZE, INPUT_SIZE))
            rgb = ImageOps.exif_transpose(img).convert("RGB")
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
    if out["taken_at"] is None:
        out["taken_at"] = _parse_exif_date(_sips_creation(path))
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


def _sips_creation(path: Path) -> str | None:
    import subprocess

    try:
        out = subprocess.run(
            ["sips", "-g", "creation", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if line.strip().startswith("creation:"):
            value = line.split(":", 1)[1].strip()
            if "<" in value or value in ("", "null"):
                return None
            return value
    return None


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