"""Сервер гасится по запросу приложения.

Отдельный файл, потому что здесь поднимается настоящий HTTP-сервер
на свободном порту: мокать `shutdown()` нечего, нужно убедиться, что
порт действительно освобождается и поток обслуживания заканчивается.

Токен тест передаёт сам через параметр `serve()`: приложение делает то
же самое переменной окружения KURATOR_TOKEN.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from kurator.web.server import serve

TOKEN = "test-token"


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for(port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    last: BaseException | None = None
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1)
            return
        except urllib.error.HTTPError as exc:
            # 403 «нет токена» — тоже ответ: сервер поднялся и слушает.
            if exc.code == 403:
                return
            last = exc
            time.sleep(0.05)
        except Exception as exc:
            # Причина не отвечающего сервера — единственное, что здесь
            # полезно знать. «Сервер не поднялся» само по себе не говорит
            # ни отказал ли порт, ни запрещён ли соединение, ни жив ли он.
            last = exc
            time.sleep(0.05)
    # Сокет слушает, но не отвечает — accept() не вызывается, и виновата
    # строка где-то между созданием сервера и serve_forever(). Стек всех
    # потоков показывает её точно; угадывать по симптому бессмысленно.
    import faulthandler

    faulthandler.dump_traceback()
    raise AssertionError(f"сервер не поднялся: {last!r}")


def _get(port: int, token: str, path: str) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers={"X-Kurator-Token": token}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, {}


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
    # Исключение из потока обслуживания иначе пропадает: поток демон, его
    # трейсборк никто не печатает, и падение выглядит как «сервер не
    # поднялся» без единого слова о причине. Именно так выглядела поломка
    # на macOS-раннере до того, как её начали ловить здесь.
    crashed: list[BaseException] = []

    def run() -> None:
        try:
            serve(root, port=port, token=TOKEN)
        except BaseException as exc:  # noqa: BLE001 — причина нужна в тесте
            crashed.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        _wait_for(port)
    except AssertionError:
        if crashed:
            raise AssertionError(
                f"сервер упал при поднятии: {crashed[0]!r}"
            ) from crashed[0]
        raise
    return thread, finished, port


def test_quit_stops_serve_forever_and_releases_the_thread(tmp_path):
    """Главное свойство выхода: сервер гаснет сам и порт освобождается.

    Раньше завершить kurator, запущенный как приложение, было нечем:
    его оставалось убивать в мониторе системы.
    """
    thread, finished, port = _start(tmp_path, "album")

    status, payload = _post(port, TOKEN, "/api/quit")

    assert status == 200
    assert payload == {"quitting": True}
    assert finished.wait(10), "serve() должен вернуться после запроса о выходе"
    thread.join(timeout=5)
    assert thread.is_alive() is False

    # Порт должен быть снова пригоден: поднимаем на нём новый сервер.
    # Это ровно то, что делает приложение — запускает бэкенд заново.
    again = tmp_path / "album-again"
    again.mkdir()
    second_finished = threading.Event()

    def run_again() -> None:
        serve(again, port=port, token=TOKEN)
        second_finished.set()

    second = threading.Thread(target=run_again, daemon=True)
    second.start()
    _wait_for(port)

    assert _get(port, TOKEN, "/api/state")[0] == 200

    _post(port, TOKEN, "/api/quit")
    assert second_finished.wait(10)
    second.join(timeout=5)


def test_server_starts_without_reverse_dns_lookup(tmp_path, monkeypatch):
    """Сервер должен подниматься, даже если резолвер отвечает вечно.

    `http.server.HTTPServer.server_bind()` делает `socket.getfqdn(host)` —
    обратный DNS-запрос. На macOS-раннере GitHub он не возвращался за десять
    секунд: сокет слушал, но не отвечал, и kurator выглядел зависшим без
    единого слова об ошибке. Локальному интерфейсу имя хоста не нужно.

    Здесь `getfqdn` подменён на намеренно зависающий, поэтому старый код
    упал бы на таймауте, а текущий — нет.
    """
    import socket as socket_module

    def hanging_getfqdn(_host):
        # Сон длиннее любого разумного таймаута: если сервер полезет сюда,
        # тест упадёт по часам, а не случайно пройдёт на быстрой машине.
        time.sleep(30)
        return "unreachable"

    monkeypatch.setattr(socket_module, "getfqdn", hanging_getfqdn)

    thread, finished, port = _start(tmp_path, "album-nodns")

    assert _get(port, TOKEN, "/api/state")[0] == 200

    _post(port, TOKEN, "/api/quit")
    assert finished.wait(10)
    thread.join(timeout=5)


def test_quit_refuses_a_foreign_token(tmp_path):
    """Чужой токен не должен гасить сервер: иначе любой, кто знает адрес,
    мог бы остановить разбор чужой папки одним запросом."""
    thread, finished, port = _start(tmp_path, "album2")

    status, _ = _post(port, "wrong-token", "/api/quit")
    assert status == 403

    # Сервер обязан остаться в живых.
    assert _get(port, TOKEN, "/api/state")[0] == 200
    assert finished.wait(1.0) is False

    _post(port, TOKEN, "/api/quit")
    assert finished.wait(10)
    thread.join(timeout=5)


def test_token_comes_from_environment(tmp_path, monkeypatch):
    """Приложение передаёт токен через KURATOR_TOKEN, и бэкенд слушает его.

    Это единственный канал, которым собранное приложение говорит бэкенду,
    каким токеном пользоваться: файла экземпляра больше нет, и потерять
    эту переменную означает «приложение запустилось, но не отвечает».
    """
    monkeypatch.setenv("KURATOR_TOKEN", "env-token")

    root = tmp_path / "album-env"
    root.mkdir()
    port = _free_port()
    finished = threading.Event()

    def run() -> None:
        # Параметр token не передан нарочно: сервер обязан взять окружение.
        serve(root, port=port)
        finished.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    _wait_for(port)

    assert _get(port, "env-token", "/api/state")[0] == 200
    assert _get(port, TOKEN, "/api/state")[0] == 403

    _post(port, "env-token", "/api/quit")
    assert finished.wait(10)
    thread.join(timeout=5)
