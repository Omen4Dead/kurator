"""Пороги в интерфейсе: откуда берутся и что при смене меняется.

Две разные вещи, которые легко спутать.

Первая — откуда интерфейс знает заводские значения. Приложение читает их
из `/api/state`, и там обязаны стоять пороги из конфига, а не число,
застывшее в клиенте: иначе кнопка «сбросить» вернёт прежнее значение
после смены порога в коде.

Вторая — что происходит с индексом при смене порога. Пороги не
участвуют в индексации, поэтому строки снимков не должны меняться
ни байтом. Это и есть то, что 1.11 обещает пользователю, и обещание
без проверки рано или поздно перестанет быть правдой: появится
параметр, влияющий на препроцессинг, и пересчёт станет нужен, а
интерфейс по-прежнему будет молчать об этом.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from kurator.config import NEAR_DUP_COSINE_STRONG, NEAR_DUP_PHASH_DISTANCE
from kurator.db import Store

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
        except Exception as exc:  # noqa: BLE001 — причина нужна в тесте
            last = exc
            time.sleep(0.05)
    raise AssertionError(f"сервер не поднялся: {last!r}")


def _get(port: int, token: str, path: str) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers={"X-Kurator-Token": token}
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode())


def _post(port: int, token: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body or {}).encode(),
        headers={"X-Kurator-Token": token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return {"_status": exc.code, "_body": exc.read().decode()}


@pytest.fixture()
def server(tmp_path: Path):
    root = tmp_path / "archive"
    root.mkdir()
    port = _free_port()
    finished = threading.Event()

    def run() -> None:
        from kurator.web.server import serve

        try:
            serve(root, port=port, token=TOKEN)
        finally:
            finished.set()

    threading.Thread(target=run, daemon=True).start()
    _wait_for(port)
    yield port, root
    _post(port, TOKEN, "/api/quit")
    assert finished.wait(10), "сервер не остановился после /api/quit"


def test_status_factory_matches_config(server):
    """`factory` в статусе совпадает с порогами из кода."""
    port, _ = server
    status = _get(port, TOKEN, "/api/state")
    assert status["factory"]["phash"] == pytest.approx(NEAR_DUP_PHASH_DISTANCE)
    assert status["factory"]["cosine"] == pytest.approx(NEAR_DUP_COSINE_STRONG)


def test_defaults_reflect_stored_override(server):
    """Сохранённый порог виден в `defaults` и не выдаётся за заводской.

    Именно эта разница объясняет пользователю, что он настроил своё.
    """
    port, _ = server
    _post(port, TOKEN, "/api/tuning", {"phash": 7, "cosine": 0.9})

    status = _get(port, TOKEN, "/api/state")
    assert status["defaults"]["phash"] == pytest.approx(7)
    assert status["factory"]["phash"] == pytest.approx(NEAR_DUP_PHASH_DISTANCE)


def _rows(root: Path) -> list[tuple]:
    store = Store.open(root)
    try:
        return [
            (p.rel_path, p.size, p.md5, p.phash, p.dhash, p.taken_at)
            for p in store.photos()
        ]
    finally:
        store.close()


def test_tuning_change_leaves_index_untouched(server):
    """Смена порога не трогает индекс — это и обещание 1.11.

    Проверяется на настоящих строках: после `POST /api/tuning` с другим
    порогом содержимое снимков в базе должно совпасть. Если когда-нибудь
    появится параметр, требующий пересчёта, этот тест упадёт первым —
    и это правильно: молчание интерфейса о переиндексации перестанет
    быть правдой незаметно для разработчика.
    """
    port, archive = server

    before = _rows(archive)
    _post(port, TOKEN, "/api/tuning", {"phash": 2, "cosine": 0.99})
    _post(port, TOKEN, "/api/tuning", {"phash": 10, "cosine": 0.8})
    after = _rows(archive)

    assert before == after, "смена порога изменила строки индекса"
    assert _get(port, TOKEN, "/api/state")["defaults"]["phash"] == 10
