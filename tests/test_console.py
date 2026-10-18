"""Русский текст в консоли на Windows."""

from __future__ import annotations

import io

import pytest

from kurator.console import utf8_stdio


def _cp1252_stream() -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252")


def test_russian_survives_windows_console():
    """Кириллица печатается в консоль cp1252, а не роняет программу.

    Раньше первая же строка вывода поднимала UnicodeEncodeError прямо
    из `print`, и приложение падало целиком с кодом -1. На macOS это не
    воспроизводится: там консоль utf-8, поэтому поломка жила только в
    Windows-сборке и была видна исключительно проверочным консольным
    вариантом.
    """
    stream = _cp1252_stream()
    utf8_stdio((stream,))
    print("корневая папка : Отпуск", file=stream)


def test_without_the_fix_it_really_does_raise():
    """Зафиксировать саму поломку, а не только починку.

    Тест выше проходит и у кода, который ничего не делает, если тот
    печатает по-русски в utf-8. Этот проверяет, что исходная ошибка
    действительно была и была такой: если она перестанет
    воспроизводиться, значит поменялось что-то ещё и починку надо
    перепроверить.
    """
    stream = _cp1252_stream()
    with pytest.raises(UnicodeEncodeError):
        print("корневая папка : Отпуск", file=stream)


def test_utf8_stdio_makes_it_printable():
    """Та же строка после перенастройки потока печатается и читается.

    Проверяет, что чинит именно `utf8_stdio`, а не что-нибудь другое:
    без неё первый тест падает, с ней — проходит.
    """
    stream = _cp1252_stream()
    utf8_stdio((stream,))
    print("корневая папка : Отпуск", file=stream)
    stream.flush()
    assert "Отпуск" in stream.buffer.getvalue().decode("utf-8")


def test_unprintable_symbol_does_not_kill_the_program():
    """Символ вне кодировки заменяется, а не обрывает вывод.

    Один непечатный символ посреди сообщения не должен превращать его в
    краш: пользователю полезнее увидеть текст с пропуском, чем ничего.
    """
    stream = _cp1252_stream()
    utf8_stdio((stream,))
    print("папка — 12 файлов", file=stream)
    stream.flush()
    assert "файлов" in stream.buffer.getvalue().decode("utf-8")


def test_absent_streams_are_not_an_error():
    """У оконного приложения потоков нет, и это нормальный случай.

    `sys.stdout` у собранного приложения равен `None`. Проверка не должна
    на этом падать: иначе починить кодировку нельзя было бы именно там,
    где она нужнее всего, — в приложении.
    """
    utf8_stdio((None,))  # type: ignore[arg-type]


def test_object_without_reconfigure_is_skipped():
    """Поток, который перенастраивать нечем, пропускается молча.

    Подменённый stdout в тестах и сторонние обёртки так себя ведут.
    Исключение здесь означало бы краш на старте — то есть ровно то, что
    эта функция чинит.
    """

    class Stub:
        pass

    utf8_stdio((Stub(),))  # type: ignore[arg-type]
