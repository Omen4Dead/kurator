"""Сервер гасится по запросу интерфейса.

Отдельный файл, потому что здесь поднимается настоящий HTTP-сервер
на свободном порту: мокать `shutdown()` нечего, нужно убедиться, что
порт действительно освобождается и поток обслуживания заканчивается.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from kurator.web.server import serve


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for(port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
            return
        except Exception:
            time.sleep(0.05)
    raise AssertionError("сервер не поднялся")


def _token(port: int) -> str:
    page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode()
    return page.split('KURATOR_TOKEN = "')[1].split('"')[0]


def _post(port: int, token: str, path: str) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=b"{}",
        headers={"X-Kurator-Token": token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def _start(tmp_path: Path, name: str) -> tuple[threading.Thread, threading.Event, int]:
    root = tmp_path / name
    root.mkdir()
    port = _free_port()
    finished = threading.Event()

    def run() -> None:
        serve(root, port=port, open_browser=False)
        finished.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    _wait_for(port)
    return thread, finished, port


def test_quit_stops_serve_forever_and_releases_the_thread(tmp_path):
    """Главное свойство выхода: сервер гаснет сам и порт освобождается.

    Раньше завершить kurator, запущенный как приложение, было нечем:
    его оставалось убивать в мониторе системы.
    """
    thread, finished, port = _start(tmp_path, "album")

    status, payload = _post(port, _token(port), "/api/quit")

    assert status == 200
    assert payload == {"quitting": True}
    assert finished.wait(10), "serve() должен вернуться после запроса о выходе"
    thread.join(timeout=5)
    assert thread.is_alive() is False

    # Порт должен быть снова пригоден: поднимаем на нём новый сервер.
    # Это ровно то, что делает человек — открывает kurator заново.
    again = tmp_path / "album-again"
    again.mkdir()
    second_finished = threading.Event()

    def run_again() -> None:
        serve(again, port=port, open_browser=False)
        second_finished.set()

    second = threading.Thread(target=run_again, daemon=True)
    second.start()
    _wait_for(port)

    assert urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).status == 200

    _post(port, _token(port), "/api/quit")
    assert second_finished.wait(10)
    second.join(timeout=5)


def test_quit_refuses_a_foreign_token(tmp_path):
    """Чужой токен не должен гасить сервер: иначе любой, кто знает адрес,
    мог бы остановить разбор чужой папки одной ссылкой."""
    thread, finished, port = _start(tmp_path, "album2")

    status, _ = _post(port, "wrong-token", "/api/quit")
    assert status == 403

    # Сервер обязан остаться в живых.
    urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read()
    assert finished.wait(1.0) is False

    _post(port, _token(port), "/api/quit")
    assert finished.wait(10)
    thread.join(timeout=5)