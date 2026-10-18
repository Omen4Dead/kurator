"""Константы и пути."""

from __future__ import annotations

import os
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent

# Сырые файлы камер.
#
# Список не из фантазий, а из того, как устроен контейнер: Pillow читает
# RAW как обычный TIFF, потому что это TIFF-контейнеры — CR2 (Canon),
# NEF (Nikon), ARW (Sony), ORF (Olympus), RW2 (Panasonic), DNG (Adobe),
# PEF (Pentax), SRW (Samsung), ERF (Epson).
#
# Чего здесь нет и почему: RAF (Fujifilm) и X3F (Sigma) устроены иначе,
# Pillow их не открывает. Если добавить их, каждый такой файл будет
# падать в счётчик «нечитаемых», и поддержка RAW окажется поддержкой
# с ошибками в каждом втором архиве. Лучше не видеть их совсем, чем видеть
# и врать.
RAW_EXTENSIONS = {
    ".cr2",   # Canon
    ".nef",   # Nikon
    ".arw",   # Sony
    ".orf",   # Olympus, Panasonic- Lumix
    ".rw2",   # Panasonic
    ".dng",   # Adobe, в том числе пересобранные из других
    ".pef",   # Pentax
    ".srw",   # Samsung
    ".erf",   # Epson
}

# HEIF-семейство: то, чем снимает iPhone и дешёвые Android. Pillow эти
# файлы не открывает вовсе — их читает libheif через pillow-heif.
#
# На айфоновском архиве это треть снимков, и до подключения они были не
# просто не видны: файлы попадали в индекс, не получали ни эмбеддинга,
# ни pHash и уходили в счётчик «нечитаемых». Поддержка, которая
# выглядит включённой и молча не работает, хуже её отсутствия.
HEIF_EXTENSIONS = {
    ".heic",
    ".heif",
    ".heics",
    ".heifs",
    ".hif",
}

# Всё остальное читает Pillow сам. Список собран проверкой, а не по
# документации: каждый формат записывается настоящим файлом и читается
# обратно, см. tests/test_formats.py.
IMAGE_EXTENSIONS = {
    # JPEG
    ".jpg",
    ".jpeg",
    ".jpe",
    ".jfif",
    # Обычное
    ".png",
    ".webp",
    ".tif",
    ".tiff",
    ".bmp",
    ".dib",
    ".gif",
    # JPEG 2000: сканеры и часть архивов
    ".jp2",
    ".j2k",
    ".jpf",
    ".j2c",
    ".jpc",
    ".jpx",
    # AVIF: Pillow 12 умеет сам, отдельная библиотека не нужна
    ".avif",
    ".avifs",
    # Старые сканеры
    ".pcx",
    ".tga",
    *HEIF_EXTENSIONS,
    *RAW_EXTENSIONS,
}

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".heic-mov"}

STATE_DIRNAME = ".kurator"
# Карантин живёт внутри служебной папки: одна скрытая директория вместо
# двух, и индекс не нужно отдельно исключать из обхода — индексатор
# пропускает всё, что начинается с точки.
QUARANTINE_DIRNAME = f"{STATE_DIRNAME}/quarantine"
# Старое расположение: карантин был отдельной папкой в корне. Нужно,
# чтобы переехать на него можно было не спрашивая, иначе файлы, уже
# перенесённые в старый карантин, потеряли бы связь с манифестами.
LEGACY_QUARANTINE_DIRNAME = "_kurator_quarantine"
DB_FILENAME = "index.db"

MODEL_FILENAME = "vision_model_int8.onnx"
MODEL_REPO = "onnx-community/siglip2-base-patch16-224-ONNX"
MODEL_URL = f"https://huggingface.co/{MODEL_REPO}/resolve/main/onnx/{MODEL_FILENAME}"
MODEL_SHA256 = "0dd31785a2713f1113ef2272472165c69d580473dae38d7b47568ac587795e70"
MODEL_LICENSE = "Apache-2.0"
MODEL_SIZE_HINT = "90 МБ"

INPUT_SIZE = 224
BATCH_SIZE = 16

SIGLIP_MEAN = 0.5
SIGLIP_STD = 0.5

NEAR_DUP_PHASH_DISTANCE = 10
NEAR_DUP_COSINE_STRONG = 0.90
NEAR_DUP_COSINE_WEAK = 0.75
BURST_WINDOW_SECONDS = 2.0
BURST_PHASH_DISTANCE = 6

CLUSTER_COSINE = 0.86

SKIP_DIRNAMES = {
    STATE_DIRNAME,
    QUARANTINE_DIRNAME,
    LEGACY_QUARANTINE_DIRNAME,
    "node_modules",
    ".git",
    ".Trash",
    ".Trashes",
}


def state_dir(root: Path) -> Path:
    return Path(root).resolve() / STATE_DIRNAME


def db_path(root: Path) -> Path:
    return state_dir(root) / DB_FILENAME


def default_model_path() -> Path:
    env = os.environ.get("KURATOR_MODEL")
    if env:
        return Path(env).expanduser()
    if getattr(sys, "frozen", False):
        # В собранном приложении исходников нет, и PROJECT_ROOT указывает
        # внутрь бандла. Весы кладутся рядом с интерпретатором, поэтому
        # ищем их там, где PyInstaller действительно распаковал данные.
        base = Path(getattr(sys, "_MEIPASS", "") or Path(sys.executable).resolve().parent)
        return base / "models" / MODEL_FILENAME
    return PROJECT_ROOT / "models" / MODEL_FILENAME


def rel_posix(path: Path, root: Path) -> str:
    """Путь относительно корня всегда через «/».

    Индекс, манифесты и веб-интерфейс сравнивают и режут пути через
    `split("/")`. `str(path.relative_to(root))` на Windows даёт обратные
    слэши, и дальше это расходится: `list_trash` читает из манифеста
    `.kurator\\quarantine\\a.jpg`, а при обходе диска получает
    `.kurator/quarantine/a.jpg`. Один файл — два разных ключа, он
    показывается в корзине дважды, а удаление сбрасывает только один.

    Поэтому любой относительный путь, который попадает в индекс, манифест
    или ответ API, проходит здесь. Обратно разворачивается через
    `Path(*rel.split("/"))`.
    """
    return path.relative_to(root).as_posix()


def unflatten(rel: str) -> Path:
    """Развернуть путь из индекса обратно в `Path` текущей системы."""
    return Path(*rel.split("/"))


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS