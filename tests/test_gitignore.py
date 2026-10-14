"""Приватные данные не должны попадать в git.

Индекс хранит относительные пути к снимкам, размеры, даты съёмки, md5,
перцептивные хеши и эмбеддинги. Если папка с фотографиями окажется внутри
git-репозитория, ни база, ни перенесённые в карантин снимки, ни отчёты
не должны уехать в коммит.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IGNORE = ROOT / ".gitignore"

MUST_IGNORE = [
    ".kurator/index.db",
    "Photos/.kurator/index.db",
    "some/deep/folder/.kurator/index.db",
    "models/vision_model_int8.onnx",
    "out/report.html",
    "out/sheets/exact_001.jpg",
    "report/report.md",
    ".kurator/quarantine/IMG_1.jpg",
    "Photos/.kurator/quarantine/IMG_1.jpg",
    "Photos/.kurator/quarantine/manifest_by-hand_1.json",
    "_kurator_quarantine/IMG_1.jpg",
]


def _ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", path],
        cwd=ROOT,
        capture_output=True,
    )
    return result.returncode == 0


def test_private_paths_are_ignored():
    leaked = [p for p in MUST_IGNORE if not _ignored(p)]
    assert not leaked, "не защищено в .gitignore: " + ", ".join(leaked)


def test_index_db_is_not_tracked():
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True
    ).stdout.splitlines()
    offenders = [
        f for f in tracked
        if f.endswith((".db", ".onnx"))
        or "/.kurator/" in f
        or "_kurator_quarantine/" in f
        or ".kurator/" in f
        or f.startswith(("out/", "report/"))
    ]
    assert not offenders, f"в репозитории уже лежит: {offenders}"


def test_user_photos_are_not_blanket_ignored():
    """Фотографии самого пользователя игнорировать нельзя: это не файлы kurator."""
    assert not _ignored("Photos/снимок.jpg")
    assert not _ignored("Pictures/2024/DSC_0001.JPG")
