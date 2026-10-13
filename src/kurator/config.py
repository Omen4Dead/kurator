"""Константы и пути."""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".jpe",
    ".png",
    ".webp",
    ".heic",
    ".heif",
    ".tif",
    ".tiff",
    ".bmp",
    ".gif",
}

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".heic-mov"}

STATE_DIRNAME = ".kurator"
QUARANTINE_DIRNAME = "_kurator_quarantine"
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
    return PROJECT_ROOT / "models" / MODEL_FILENAME


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS