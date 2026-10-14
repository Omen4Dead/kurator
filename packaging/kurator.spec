# -*- mode: python ; coding: utf-8 -*-
"""Сборка kurator.app для macOS.

Запускается через packaging/build.sh, который и собирает, и подписывает,
и делает DMG. Отдельно запускать не нужно.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs

ROOT = Path(SPECPATH).parent
MODELS_DIR = ROOT / "models"
if not (MODELS_DIR / "vision_model_int8.onnx").is_file():
    raise SystemExit(
        "нет весов: models/vision_model_int8.onnx. Скачай их ./scripts/pull-model.sh"
    )

# onnxruntime подгружает свои .dylib уже во время вывода, поэтому их
# нужно положить в бандл заранее — иначе приложение падает на первой
# же индексации.
dylibs = collect_dynamic_libs("onnxruntime")
binaries = dylibs

# Модель на 90 МБ кладём рядом с интерпретатором: `config.default_model_path`
# ищет её именно там, когда приложение собрано.
# Веб-интерфейс — это три файла, которые сервер читает с диска по
# пути. PyInstaller про импорты их не видит, и без явного объявления
# приложение собирается, а отдаёт 404 на всё подряд.
ASSETS_DIR = ROOT / "src" / "kurator" / "web" / "assets"
if not (ASSETS_DIR / "index.html").is_file():
    raise SystemExit(f"нет веб-интерфейса: {ASSETS_DIR}")

datas = [(str(MODELS_DIR), "models"), (str(ASSETS_DIR), "kurator/web/assets")]

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
    excludes=["tkinter", "unittest", "pydoc_data", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="kurator",
    console=False,
    disable_windowed_traceback=False,
    icon=str(ROOT / "assets" / "kurator.icns"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="kurator",
)

app = BUNDLE(
    coll,
    name="kurator.app",
    icon=str(ROOT / "assets" / "kurator.icns"),
    bundle_identifier="ru.omen4dead.kurator",
    info_plist={
        "CFBundleName": "kurator",
        "CFBundleDisplayName": "kurator",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        # Приложение только слушает локальный порт и ходит в интернет
        # не умеет вовсе: разрешать ему сеть незачем.
        "NSAppTransportSecurity": {"NSAllowsArbitraryLoads": False},
    },
)