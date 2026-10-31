# -*- mode: python ; coding: utf-8 -*-
"""Сборка python-бэкенда kurator для macOS (Apple Silicon).

PyInstaller здесь собирает ТОЛЬКО бэкенд: onedir-каталог `dist/backend`
с исполняемым `kurator`. Итоговое kurator.app собирает build.sh руками:
главный исполняемый файл — нативный SwiftUI-бинарь из app/, а этот
каталог уезжает в `Contents/Resources/backend/`.

Запускается через packaging/build.sh, который и собирает, и подписывает,
и делает установщик. Отдельно запускать не нужно.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs

ROOT = Path(SPECPATH).parent
MODELS_DIR = ROOT / "models"
if not (MODELS_DIR / "vision_model_int8.onnx").is_file():
    raise SystemExit(
        "нет весов: models/vision_model_int8.onnx. "
        "Скачай их ./scripts/pull-model.sh"
    )

# onnxruntime подгружает свои библиотеки уже во время вывода, поэтому их
# нужно положить в бандл заранее — иначе приложение падает на первой же
# индексации.
binaries = collect_dynamic_libs("onnxruntime")

# Модель на 90 МБ кладём рядом с интерпретатором: `config.default_model_path`
# ищет её именно там, когда приложение собрано.
datas = [(str(MODELS_DIR), "models")]

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        "onnxruntime",
        "onnxruntime.capi._pybind_state",
        "PIL.Image",
        "PIL.JpegImagePlugin",
    ],
    hookspath=[],
    runtime_hooks=[],
    # Стандартная библиотека целиком весит больше самой программы.
    # Всё это в поставке не нужно, а места занимает прилично: тесты
    # numpy тянут за собой половину стандартной библиотеки.
    excludes=[
        "tkinter", "unittest", "pydoc_data", "test", "tests",
        "numpy.testing", "numpy.f2py", "numpy.distutils",
        "PIL.ImageQt", "PIL.ImageShow", "PIL.ImageTk",
        "lib2to3", "distutils", "idlelib", "turtledemo", "sqlite3.test",
        "email.mime.image",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="kurator",
    # Бэкенд — консольный процесс: приложение запускает его само и читает
    # его вывод, а окна у него нет.
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    # `strip -S` снимает только отладочные символы: поставка от этого
    # меньше, а работа не меняется.
    strip=True,
    upx=False,
    name="backend",
)
