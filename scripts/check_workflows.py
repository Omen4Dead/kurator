"""Проверить, что воркфлои вообще собираются.

Опечатка в `.github/workflows/ci.yml` не останавливает разработку: git
работает, тесты локально зелёные, а проверок просто нет — потому что
сломался рельс, на котором они ездят. GitHub при этом сообщает об ошибке
обрывочно, и заметить её легко только если смотреть.

YAML разбирается библиотекой, которой нет в зависимостях проекта: она
нужна один раз, в CI, и тащить `pyyaml` в рантайм ради проверки
собственных файлов сборки незачем. Устанавливается на лету через
`uv run --with`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


def main() -> int:
    try:
        import yaml
    except ImportError:
        print("нужен pyyaml: запускай через `uv run --with pyyaml python scripts/check_workflows.py`")
        return 2

    files = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    if not files:
        print("в .github/workflows нет ни одного файла")
        return 1

    problems: list[str] = []
    for path in files:
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            problems.append(f"{path.relative_to(ROOT)}: не разбирается — {error}")
            continue

        if not isinstance(document, dict):
            problems.append(f"{path.relative_to(ROOT)}: ожидался словарь в корне")
            continue

        # Ключ `on` YAML 1.1 превращает в True, поэтому берём оба.
        triggers = document.get("on", document.get(True))
        if triggers is None:
            problems.append(f"{path.relative_to(ROOT)}: нет секции on")
            continue

        jobs = document.get("jobs")
        if not isinstance(jobs, dict) or not jobs:
            problems.append(f"{path.relative_to(ROOT)}: нет ни одного джоба")
            continue

        names = list(jobs)
        print(f"{path.relative_to(ROOT)}: триггеры {list(triggers)}, джобы {names}")

        for name, job in jobs.items():
            if not isinstance(job, dict):
                problems.append(f"{path.relative_to(ROOT)}: джоб {name} не словарь")
                continue
            if "runs-on" not in job:
                problems.append(f"{path.relative_to(ROOT)}: у джоба {name} нет runs-on")
            steps = job.get("steps")
            if not isinstance(steps, list) or not steps:
                problems.append(f"{path.relative_to(ROOT)}: у джоба {name} нет шагов")

    if problems:
        print()
        for line in problems:
            print(f"ОШИБКА: {line}")
        return 1

    print()
    print(f"все {len(files)} воркфлоя в порядке")
    return 0


if __name__ == "__main__":
    sys.exit(main())