"""Бэкенд внутри собранного приложения — как обычная программа.

SwiftUI-приложение запускает бэкенд процессом с аргументами
`ui --port N` (их разбирает обычный командный интерфейс через
entry.py), передаёт токен через KURATOR_TOKEN и гасит процесс при
выходе. Сюда попадает только запуск без аргументов: тогда корень
берётся из прошлой сессии.
"""

from __future__ import annotations

import traceback
from pathlib import Path

from .web.server import serve

ERROR_LOG = Path.home() / ".kurator" / "last-error.log"


def _write_error_report(exc: BaseException) -> Path | None:
    """Оставить след, если приложение упало при старте.

    Собранное приложение — оконное, и в нём `sys.stderr` равен `None`:
    исключение просто исчезает, а пользователь видит значок, который не
    открывается, и никаких объяснений. Именно так выглядел баг с
    зависшим запуском, и разобрать его без следа было нечем.

    Файл маленький, перезаписывается и содержит только текст ошибки.
    """
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        ERROR_LOG.write_text(
            "kurator не смог запуститься\n\n" + "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ),
            encoding="utf-8",
        )
        return ERROR_LOG
    except OSError:
        return None


def main() -> None:
    # Жизнью бэкенда управляет приложение: оно же его и погасит. Своих
    # сторожей здесь нет — процесс без приложения бессмысленен, но и
    # висеть он не будет, приложение завершает его вместе с собой.
    try:
        serve(root=None)
    except BaseException as exc:
        path = _write_error_report(exc)
        if path is not None:
            try:
                # Без консоли сообщение некуда деть: приложение запускается
                # из Finder, и без osascript ни одна подсказка не дойдёт.
                __import__("subprocess").run(
                    [
                        "osascript",
                        "-e",
                        f'display notification "kurator не смог запуститься. '
                        f'Подробности в {path}" with title "kurator"',
                    ],
                    check=False,
                    timeout=10,
                )
            except Exception:
                pass
        raise
