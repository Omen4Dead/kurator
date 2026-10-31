"""Упаковка: то, что должно остаться верным всегда.

Всё здесь ловит рассинхронизацию, которая иначе обнаружилась бы на
чужой машине и молча. Истории уже случались, и выглядели одинаково —
«сборка прошла, значит всё в порядке»:

- веб-ассеты не попали в бандл, и приложение отдавало 404;
- адрес модели при `sys.frozen` вёл внутрь бандла, а веса туда не
  положили.

Проверки дешёвые, но ловят именно то, до чего не доходит ручной прогон.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "kurator.spec"
BUILD_MAC = ROOT / "packaging" / "build.sh"
ICON = ROOT / "assets" / "kurator.icns"


def test_spec_targets_macos_only():
    """Спек собирает macOS-бандл и не ветвится по платформам.

    Платформенная развилка в spec — это код, который на машине
    разработчика не исполняется никогда: ветка для другой системы
    ломается молча, и узнаёт об этом тот, кто собирает там. Целевая
    платформа одна — macOS, значит и ветвления быть не должно.
    """
    text = SPEC.read_text(encoding="utf-8")

    assert "sys.platform" not in text, "в spec появилась ветка по платформе"
    assert "kurator.icns" in text, "в spec не упомянут значок .icns"
    assert "kurator.ico" not in text, "в spec остался значок для Windows"
    assert "app = BUNDLE" in text, "в spec нет сборки .app"


def test_icon_exists_for_the_bundle():
    """Значок, который требует spec, обязан лежать в репозитории."""
    assert ICON.is_file(), f"нет {ICON.name} — сборка упадёт в spec"


def test_build_script_pulls_weights_and_verifies_bundle():
    """Скрипт сборки не должен ссылаться на несуществующее.

    Опечатка в пути не останавливает сборку там, где этот путь не
    проверяется, и обнаруживается в лучшем случае в CI.
    """
    mac_text = BUILD_MAC.read_text(encoding="utf-8")

    assert "scripts/pull-model.sh" in mac_text, "build.sh не тянет веса"
    assert "packaging/verify_bundle.py" in mac_text, "build.sh не проверяет бандл"


def test_bundle_is_verified_by_running_it():
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

    workflow = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
    assert "verify_bundle.py" in workflow, "сборка в CI не проверяет бандл"


def test_runtime_stays_without_web_frameworks():
    """Правило «число зависимостей не ограничено, смысл — да» с границами.

    Отмена правила трёх зависимостей не означает «добавляйте что хочется».
    Границы объявлены в `AGENTS.md`: нельзя то, что делается стандартной
    библиотекой, и нельзя то, что тянет веб-сервер, ORM, CLI-фреймворк
    или свой формат данных. Веб-интерфейс остаётся тремя статическими
    файлами и `ThreadingHTTPServer`.

    Проверка дешёвая и ловит именно то, что тихо протаскивают: в
    зависимости попадает фреймворк «на будущее».
    """
    import tomllib

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    runtime = {
        d.split(">")[0].split("=")[0].split("[")[0].strip().lower()
        for d in pyproject["project"]["dependencies"]
    }

    forbidden = {
        "flask", "fastapi", "django", "starlette", "uvicorn", "gunicorn",
        "tornado", "quart", "litestar", "aiohttp", "requests", "httpx",
        "sqlalchemy", "alembic", "pydantic",
        "click", "typer", "rich",
    }
    smuggled = sorted(runtime & forbidden)
    assert not smuggled, (
        "в рантайм-зависимостях появились пакеты, которые правило запрещает: "
        f"{', '.join(smuggled)}. Читать форматы — да, тянуть фреймворк — нет."
    )

    # Заявленная причина отсутствия фреймворка должна оставаться правдой:
    # сервер слушает только петлю, и интерфейс — статические файлы.
    server = (ROOT / "src" / "kurator" / "web" / "server.py").read_text(encoding="utf-8")
    assert "ThreadingHTTPServer" in server, "веб-сервер перестал быть стандартным"


def test_runtime_has_no_subprocess_after_all():
    """Сетевого и внешнего кода в рантайме быть не должно.

    `sips` как источник даты был таким местом: подстановка команды,
    которая вне macOS молча возвращала пустоту. Проверка смотрит по AST,
    а не по тексту.
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
