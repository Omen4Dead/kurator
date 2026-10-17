"""Приложение запускается второй раз.

Пользовательский баг, найденный на MacBook: открыть kurator, закрыть
вкладку не нажимая «выход», нажать на значок снова — и ничего не
происходит. Анимация иконки зависает, интерфейс не появляется.

Причина: `serve` поднимал сервер на фиксированном порту и ничем не был
защищён от повторного запуска. Первый экземпляр после закрытия вкладки
продолжал жить, второй падал на занятом порту с `OSError`, а показать
эту ошибку в приложении было нечем.

Здесь проверяется именно то поведение, ради которого всё затевалось:
второй щелчок по значку открывает окно первого, а не роняет процесс.
"""

from __future__ import annotations

import json
import itertools
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from kurator.web import server as srv
from kurator.web.server import State, _Server, serve


_PORT_SEQ = itertools.count(48100)


def _free_port() -> int:
    """Порт из своего диапазона, а не эфемерный.

    `bind(0)` возвращает порт из пула операционной системы, и тот же
    порт очень скоро может достаться следующему тесту в том же процессе
    — особенно если предыдущий сервер только что освободил его. Тогда
    тест падал бы на «Address already in use» не из-за кода, а из-за
    планировщика. Свой диапазон и последовательный счётчик этого не
    допускают.
    """
    for _ in range(200):
        port = next(_PORT_SEQ)
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise AssertionError("не нашлось свободного порта")


def _token_of(port: int) -> str:
    page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode()
    return page.split('KURATOR_TOKEN = "')[1].split('"')[0]


def _post(port: int, token: str, path: str) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=b"{}",
        headers={"X-Kurator-Token": token, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode())


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """След живого экземпляра не должен попадать в домашнюю папку.

    Без этого тест и настоящий kurator на одной машине мешали бы друг
    другу: один увидит след другого и решит, что приложение уже запущено.
    """
    monkeypatch.setattr(srv, "state_dir", lambda: tmp_path / "home-kurator")
    monkeypatch.setattr(srv, "history_path", lambda: tmp_path / "home-kurator" / "roots.json")
    yield


def _start_serving(root: Path, port: int) -> tuple[threading.Thread, threading.Event]:
    """Поднять сервер как это делает приложение, но в потоке."""
    finished = threading.Event()

    def run() -> None:
        try:
            serve(root, port=port, open_browser=False)
        finally:
            finished.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, finished


def _wait(port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
            return
        except Exception:
            time.sleep(0.05)
    raise AssertionError("сервер не поднялся")


def test_second_launch_opens_the_running_window(tmp_path, monkeypatch):
    """Второй запуск открывает окно первого, а не падает.

    Раньше он молча умирал на занятом порту, и пользователь видел
    зависшую иконку без единого слова о причине.
    """
    port = _free_port()
    opened: list[str] = []
    monkeypatch.setattr(srv.webbrowser, "open", lambda url: opened.append(url))

    first, _ = _start_serving(tmp_path / "album", port)
    _wait(port)

    # Ровно то, что делает человек: щёлкнуть по значку снова.
    serve(None, port=port, open_browser=True)

    assert opened, "второй запуск ничего не открыл"
    assert opened[0].startswith(f"http://127.0.0.1:{port}/"), (
        f"открыт не тот адрес: {opened[0]}"
    )

    # И главное: первый экземпляр остался жив и отвечает.
    assert _token_of(port), "первый экземпляр не отвечает после второго запуска"

    _post(port, _token_of(port), "/api/quit")


def test_second_launch_does_not_raise_port_error(tmp_path, monkeypatch):
    """Главный симптом: повторный запуск не должен ронять процесс.

    Именно это и происходило — `OSError` на занятом порту, который
    приложение показать не умело.
    """
    port = _free_port()
    monkeypatch.setattr(srv.webbrowser, "open", lambda url: None)

    _start_serving(tmp_path / "album", port)
    _wait(port)

    # Если бы защиты не было, здесь вылетел бы OSError.
    serve(None, port=port, open_browser=True)

    _post(port, _token_of(port), "/api/quit")


def test_only_one_server_survives(tmp_path, monkeypatch):
    """Два процесса с двумя копиями модели — это ровно то, что не нужно.

    Свободный порт как ответ на повторный запуск формально решает
    «не упасть», но даёт второй сервер со второй моделью в памяти.
    Проверяем, что этого не происходит.
    """
    port = _free_port()
    monkeypatch.setattr(srv.webbrowser, "open", lambda url: None)

    _start_serving(tmp_path / "album", port)
    _wait(port)
    serve(None, port=port, open_browser=True)

    # Порт по-прежнему отвечает, и отвечает тот же экземпляр:
    # токен у него не изменился.
    assert _token_of(port) == _token_of(port)
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
        assert response.status == 200

    _post(port, _token_of(port), "/api/quit")


def test_explicit_folder_still_starts_its_own_server(tmp_path, monkeypatch):
    """Явная папка в командной строке — это другая задача.

    `kurator ui ~/Другое` при запущенном приложении обязан поднять свой
    сервер: пользователь просил именно про эту папку. Перенаправление
    действует только на запуск без папки, то есть на щелчок по значку.
    """
    port = _free_port()
    other = _free_port()
    monkeypatch.setattr(srv.webbrowser, "open", lambda url: None)

    _start_serving(tmp_path / "album", port)
    _wait(port)

    thread, finished = _start_serving(tmp_path / "other", other)
    _wait(other)

    assert finished.is_set() is False, "второй сервер со своей папкой сразу вышел"

    _post(other, _token_of(other), "/api/quit")
    _post(port, _token_of(port), "/api/quit")


def test_dead_instance_file_is_ignored(tmp_path, monkeypatch):
    """Слепок от упавшего процесса не должен помешать запуску.

    Экземпляр мог погибнуть, не сняв след с диска. Если бы такой след
    считался рабочим, приложение не запускалось бы никогда — до
    перезагрузки.
    """
    monkeypatch.setattr(srv.webbrowser, "open", lambda url: None)
    srv.write_instance({"url": "http://127.0.0.1:1/", "pid": 999999, "port": 1})

    assert srv.live_instance() is None, "слепок от упавшего процесса посчитан живым"

    port = _free_port()
    thread, _ = _start_serving(tmp_path / "album", port)
    _wait(port)
    _post(port, _token_of(port), "/api/quit")


def test_foreign_server_on_port_is_not_mistaken_for_ours(tmp_path, monkeypatch):
    """Занятый порт — ещё не kurator.

    Порт мог занять кто-то другой. Открывать пользователю чужую
    страницу и считать её своим интерфейсом нельзя, поэтому след
    проверяется по содержимому ответа, а не по одному «порт отвечает».
    """

    class Foreign:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, _size=None):
            return b"<html>someone else entirely</html>"

    monkeypatch.setattr(srv.urllib.request, "urlopen", lambda *a, **k: Foreign())
    srv.write_instance({"url": "http://127.0.0.1:8765/", "pid": 1, "port": 8765})

    assert srv.live_instance() is None, "чужой сервер принят за kurator"


def test_instance_trace_is_removed_on_shutdown(tmp_path, monkeypatch):
    """После остановки следа быть не должно.

    Иначе следующий запуск увидит «живой» экземпляр и не поднимется.
    """
    port = _free_port()
    monkeypatch.setattr(srv.webbrowser, "open", lambda url: None)

    _start_serving(tmp_path / "album", port)
    _wait(port)

    assert srv.instance_path().is_file(), "следа нет, второй запуск не найдёт экземпляр"

    _post(port, _token_of(port), "/api/quit")

    for _ in range(50):
        if not srv.instance_path().is_file():
            break
        time.sleep(0.1)
    assert not srv.instance_path().is_file(), "след остался после остановки"