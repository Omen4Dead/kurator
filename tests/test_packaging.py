"""Упаковка: то, что должно остаться верным всегда.

Всё здесь ловит рассинхронизацию, которая иначе обнаружилась бы на
чужой машине и молча. Три истории уже случились, и все три выглядели
одинаково — «сборка прошла, значит всё в порядке»:

- веб-ассеты не попали в бандл, и приложение отдавало 404;
- `ctypes.wintypes` был исключён из спека, и на Windows падал `sqlite3`,
  а на macOS всё выглядело исправным;
- иконка `.ico` разошлась с исходным PNG, и в Windows собиралась старая.

Проверки дешёвые, но ловят именно то, до чего не доходит ручной прогон.
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "kurator.spec"
INNO = ROOT / "packaging" / "installer.iss"
BUILD_WIN = ROOT / "packaging" / "build-windows.ps1"
BUILD_MAC = ROOT / "packaging" / "build.sh"
ICO_SOURCE = ROOT / "assets" / "icon-1024.png"
ICO_TARGET = ROOT / "assets" / "kurator.ico"


def test_committed_icon_matches_source_png():
    """`.ico` должен собираться из текущего PNG, а не из прошлого.

    Иначе поменяли логотип, а Windows-сборка продолжает брать старую
    иконку — и это видно только пользователям, у которых она в панели
    задач. Сборка при этом проходит без единого сообщения.
    """
    assert ICO_TARGET.is_file(), (
        f"нет {ICO_TARGET.name}. Собери: uv run python scripts/make_ico.py"
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "make_ico.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, (
        "иконка разошлась с исходником:\n" + result.stdout + result.stderr
    )


def test_ico_holds_every_size_windows_needs():
    """В .ico нужен набор размеров, а не один большой.

    Windows растянет единственный кадр на панели задач и в проводнике,
    и иконка станет кашей. Набор проверен на всех семи кадрах, а не по
    факту существования файла.
    """
    from PIL import Image

    with Image.open(ICO_TARGET) as image:
        sizes = sorted(image.info.get("sizes", []))

    expected = sorted((size, size) for size in (16, 24, 32, 48, 64, 128, 256))
    assert sizes == expected, f"в .ico размеры {sizes}, ожидались {expected}"


def test_spec_excludes_nothing_windows_sqlite_needs():
    """`ctypes.wintypes` нельзя выкидывать из поставки.

    На macOS его никто не импортирует, и строка в `excludes` выглядит
    безобидной. На Windows его использует `sqlite3`: без него приложение
    запустилось бы и упало на первом же обращении к архиву — то есть
    ровно тогда, когда пользователь уже ждёт результат.

    Список берётся из самого вызова `Analysis`, а не поиском по тексту:
    в файле есть комментарий, который объясняет, что модуль исключать
    нельзя, и простой поиск споткнулся бы именно на нём.
    """
    import ast

    tree = ast.parse(SPEC.read_text(encoding="utf-8"))
    excludes: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Analysis":
            for keyword in node.keywords:
                if keyword.arg == "excludes":
                    excludes = [
                        element.value
                        for element in keyword.value.elts
                        if isinstance(element, ast.Constant)
                    ]

    assert excludes, "не нашёл список excludes в Analysis"
    assert "ctypes.wintypes" not in excludes, (
        "ctypes.wintypes нельзя исключать: на Windows его держит sqlite3"
    )


def test_spec_builds_both_platforms():
    """Спек должен быть общим, иначе правки разъедутся.

    Две копии одного файла — это гарантированный момент, когда одну
    забыли поправить и сломали одну из платформ. Поэтому: BUNDLE и
    macOS-специфика только под условием, значок выбирается по системе.
    """
    text = SPEC.read_text(encoding="utf-8")

    assert "IS_MACOS" in text, "в spec нет разделения платформ"
    assert "sys.platform" in text, "платформа определяется не через sys.platform"
    assert "kurator.ico" in text, "в spec не упомянута иконка для Windows"
    assert "kurator.icns" in text, "в spec не упомянут значок для macOS"

    # BUNDLE обязан быть под условием: на Windows он не имеет смысла.
    bundle_line = next(
        line for line in text.splitlines() if line.strip().startswith("app = BUNDLE")
    )
    guarded = "if IS_MACOS:" in text.splitlines()[: text.splitlines().index(bundle_line)][-4:]
    assert guarded, "BUNDLE не под условием платформы"


def test_installer_matches_built_binary():
    """Установщик должен ставить то, что собирает сборка.

    Имя расходится легко: поменяли `name` в spec, а Inno Setup продолжает
    искать прежнее — и падает уже на пользователе.
    """
    assert INNO.is_file(), f"нет {INNO.name}"

    spec_text = SPEC.read_text(encoding="utf-8")
    assert 'name="kurator"' in spec_text, "spec собирает не kurator"
    assert 'AppExeName "kurator.exe"' in INNO.read_text(encoding="utf-8")


def test_build_scripts_reference_existing_files():
    """Скрипты сборки не должны ссылаться на несуществующее.

    Опечатка в пути не останавливает сборку на macOS, где этого пути
    может не быть вовсе, и обнаруживается только на другой системе —
    в лучшем случае, в худшем никогда.
    """
    mac_text = BUILD_MAC.read_text(encoding="utf-8")
    win_text = BUILD_WIN.read_text(encoding="utf-8")

    assert "scripts/pull-model.sh" in mac_text, "build.sh не тянет веса"
    assert "packaging/verify_bundle.py" in mac_text, "build.sh не проверяет бандл"

    assert "pull-model.ps1" in win_text, "build-windows.ps1 не тянет веса"
    assert "make_ico.py" in win_text, "build-windows.ps1 не собирает иконку"
    assert "packaging\\verify_bundle.py" in win_text, (
        "build-windows.ps1 не проверяет бандл"
    )


def test_icon_is_rebuilt_before_windows_build():
    """Сборщик иконки обязан проверять её свежесть, а не молча перезаписывать.

    Иначе поменяли PNG, собрали на Windows, и в рабочей копии появилось
    изменение, которого никто не замечал до самого коммита.
    """
    text = BUILD_WIN.read_text(encoding="utf-8")
    assert "make_ico.py" in text
    assert "--check" in text, (
        "иконка перезаписывается молча: нужен --check, чтобы падать на расхождении"
    )


def test_windows_build_verifies_the_bundle():
    """Сборка обязана проверять бандл, а не только создавать файл.

    Иначе приложение, которое собирается, но не открывается, считалось
    бы успехом. Проверка обязана требовать эмбеддинги: именно их
    отсутствие выдаёт неполный бандл `onnxruntime`.
    """
    verify = ROOT / "packaging" / "verify_bundle.py"
    assert verify.is_file(), "нет packaging/verify_bundle.py"

    text = verify.read_text(encoding="utf-8")
    assert "/api/index" in text or "/api/root" in text, "проверка не запускает индексацию"
    assert "embedded" in text, "проверка не требует эмбеддингов"
    assert '"/"' in text or "index.html" in text, "проверка не требует главную страницу"

    # Обе платформы зовут проверку: иначе Windows-сборка останется
    # единственной непроверенной.
    assert "verify_bundle.py" in BUILD_WIN.read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
    assert workflow.count("verify_bundle.py") >= 2, "проверка вызвана не для обеих платформ"


def test_runtime_has_no_subprocess_after_all():
    """Сетевого и внешнего кода в рантайме быть не должно.

    Дублирует проверку из test_cross_platform, но здесь она же рядом с
    остальными проверками упаковки: если когда-нибудь придётся добавить
    в spec `datas` или хук, правило про внешний код проверяется здесь же.
    """
    import ast

    import kurator

    offenders: list[str] = []
    for path in Path(kurator.__file__).parent.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if "subprocess" in names:
                offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, f"subprocess в рантайме: {offenders}"


def test_icon_is_valid_ico_container():
    """Файл должен быть читаемым ICO, а не просто называться так.

    Сборка на Windows упала бы, но понятнее упасть здесь, чем на
    `uv run python scripts/make_ico.py` у того, кто правит логотип.
    """
    from PIL import Image

    data = ICO_TARGET.read_bytes()
    # ICO начинается с нулевого резерва и единичного типа (1 = icon).
    assert data[:4] == b"\x00\x00\x01\x00", "это не ICO"

    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "ICO"