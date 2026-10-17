"""Запуск без аргументов — как у обычной программы.

Из приложения в Applications пользователь не передаёт путь к папке,
поэтому корнем становится последний использованный, а при первом
запуске его предлагает выбрать сам интерфейс.
"""

from __future__ import annotations

import traceback
from pathlib import Path

from .web.server import IDLE_SHUTDOWN_SECONDS, serve

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
    # Приложение выключается само, когда вкладок не осталось. Зекрыл
    # вкладку — значит закончил, и процесс с моделью и индексом в памяти
    # не должен висеть до следующего щелчка по значку.
    #
    # В консоли такого нет: там `kurator ui` запускают руками и пользуются
    # сервером без вкладки, поэтому режим ожидания там остаётся выключенным.
    try:
        serve(root=None, idle_timeout=IDLE_SHUTDOWN_SECONDS)
    except BaseException as exc:
        path = _write_error_report(exc)
        if path is not None:
            try:
                # Без консоли сообщение некуда деть. На macOS и Windows
                # приложение может быть запущено из файлового менеджера,
                # и без osascript / msg ни одна подсказка не дойдёт.
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
