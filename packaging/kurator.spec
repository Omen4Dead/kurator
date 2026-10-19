# -*- mode: python ; coding: utf-8 -*-
"""Сборка kurator для macOS и Windows.

Запускается через packaging/build.sh на macOS и packaging/build-windows.ps1
на Windows, которые и собирают, и подписывают, и делают установщик.
Отдельно запускать не нужно.

Один spec на обе платформы, потому что расходятся только три вещи: значок
(.icns против .ico), сборка в бандл (есть только у macOS) и подпись. Всё
остальное — анализ, бинарники, данные — у PyInstaller общее, и держать
вторую копию файла значит гарантированно забыть перенести правку.
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs

IS_MACOS = sys.platform == "darwin"

# Консольный вариант собирается только для проверки сборки в CI.
#
# Оконное приложение при падении показывает диалог Windows, а в раннере
# без рабочего стола закрывать этот диалог некому: процесс живёт вечно,
# молчит и не пишет ни байта в stderr. Выглядит как зависание, и
# диагностика упирается в стену. Консольный вариант ту же ошибку печатает
# текстом, и падение становится видимым.
CONSOLE = os.environ.get("KURATOR_CONSOLE") == "1"

ROOT = Path(SPECPATH).parent
MODELS_DIR = ROOT / "models"
if not (MODELS_DIR / "vision_model_int8.onnx").is_file():
    raise SystemExit(
        "нет весов: models/vision_model_int8.onnx. "
        "Скачай их ./scripts/pull-model.sh или .\\scripts\\pull-model.ps1"
    )

# onnxruntime подгружает свои библиотеки уже во время вывода, поэтому их
# нужно положить в бандл заранее — иначе приложение падает на первой же
# индексации. На macOS это .dylib, на Windows .dll, а имя функции
# одинаковое.
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

# Значок. PyInstaller на Windows .icns не понимает вовсе, а на macOS
# принимает и .icns, и .png — но .icns там лучше, потому что система сама
# подберёт размер, а не растянет картинку.
if IS_MACOS:
    ICON = ROOT / "assets" / "kurator.icns"
    if not ICON.is_file():
        raise SystemExit(f"нет значка: {ICON}")
else:
    ICON = ROOT / "assets" / "kurator.ico"
    if not ICON.is_file():
        raise SystemExit(
            f"нет значка: {ICON}. Собери его: uv run python scripts/make_ico.py"
        )

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
    # numpy тянут за собой половину стандартной библиотерии.
    excludes=[
        "tkinter", "unittest", "pydoc_data", "test", "tests",
        "numpy.testing", "numpy.f2py", "numpy.distutils",
        "PIL.ImageQt", "PIL.ImageShow", "PIL.ImageTk",
        "lib2to3", "distutils", "idlelib", "turtledemo", "sqlite3.test",
        "email.mime.image",
        # `ctypes.wintypes` здесь НЕ исключается, хотя на macOS его никто
        # не импортирует и строка выглядит безобидной. На Windows его
        # использует sqlite3: без него приложение запустилось бы и упало
        # на первом же обращении к архиву. Список поэтому одинаков для
        # обеих систем — молчаливая поломка дороже сотен килобайт.
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
    # На Windows это собирает .exe с окном. `console=False` обязателен
    # ещё и потому, что приложение всё равно не консольное: работает
    # оно через браузер, а stdout нужен только для диагностики сборки.
    # Исключение — проверочная сборка, см. CONSOLE выше.
    console=CONSOLE,
    disable_windowed_traceback=False,
    icon=str(ICON),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    # `strip` включается только на macOS, и это не украшение, а требование
    # работоспособности на Windows.
    #
    # Там PyInstaller зовёт `strip` из PATH — а нашёлся там GNU strip — и
    # применяет его к каждому бинарнику в `_internal`, включая сам
    # `python313.dll` и системные DLL вроде
    # `api-ms-win-core-errorhandling-l1-1-0.dll`. Приложение падало с
    # кодом -1 и сообщением
    #
    #     Failed to load Python DLL ...\python313.dll
    #     LoadLibrary: Invalid access to memory location
    #
    # то есть бинарник оказывался испорченным ещё до запуска Python.
    # Оконная сборка делала то же самое, но показывала текст в диалоге,
    # который в CI закрыть некому, — и выглядела как зависшая.
    #
    # На macOS вызов правильный (`strip -S`, только отладочные символы) и
    # там он остаётся: поставка от этого меньше.
    strip=IS_MACOS,
    upx=False,
    name="kurator",
)

# Бандл .app есть только у macOS. На Windows `BUNDLE` не имеет смысла и
# только добавил бы в spec обходной путь, который нечем проверять.
if IS_MACOS:
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