# -*- mode: python ; coding: utf-8 -*-
"""Сборка kurator.app для macOS (Apple Silicon).

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
# Веб-интерфейс — это три файла, которые сервер читает с диска по
# пути. PyInstaller про импорты их не видит, и без явного объявления
# приложение собирается, а отдаёт 404 на всё подряд.
ASSETS_DIR = ROOT / "src" / "kurator" / "web" / "assets"
if not (ASSETS_DIR / "index.html").is_file():
    raise SystemExit(f"нет веб-интерфейса: {ASSETS_DIR}")

datas = [(str(MODELS_DIR), "models"), (str(ASSETS_DIR), "kurator/web/assets")]

# Значок. macOS принимает и .icns, и .png — но .icns лучше, потому что
# система сама подберёт размер, а не растянет картинку.
ICON = ROOT / "assets" / "kurator.icns"
if not ICON.is_file():
    raise SystemExit(f"нет значка: {ICON}")

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
    # Приложение не консольное: работает через браузер, а stdout нужен
    # только при диагностике сборки, и там его читает verify_bundle.py
    # из трубы.
    console=False,
    disable_windowed_traceback=False,
    icon=str(ICON),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    # `strip -S` снимает только отладочные символы: поставка от этого
    # меньше, а работа не меняется.
    strip=True,
    upx=False,
    name="kurator",
)

app = BUNDLE(
    coll,
    name="kurator.app",
    icon=str(ICON),
    bundle_identifier="ru.omen4dead.kurator",
    info_plist={
        "CFBundleName": "kurator",
        "CFBundleDisplayName": "kurator",
        "CFBundleShortVersionString": "0.4.1",
        "CFBundleVersion": "0.4.1",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        # Приложение только слушает локальный порт и ходит в интернет
        # не умеет вовсе: разрешать ему сеть незачем.
        "NSAppTransportSecurity": {"NSAllowsArbitraryLoads": False},
    },
)