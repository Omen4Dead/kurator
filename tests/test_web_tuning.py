"""Пороги в интерфейсе: откуда берутся и что при смене меняется.

Две разные вещи, которые легко спутать.

Первая — откуда интерфейс знает заводские значения. Должен знать с
сервера: страницу читает человек, у которого через полгода будет
другая версия kurator с другими порогами, и зашитое в JavaScript
число станет ложью. Проверяется статикой по app.js плюс запросом
`/api/status` на живом сервере.

Вторая — что происходит с индексом при смене порога. Пороги не
участвуют в индексации, поэтому строки снимков не должны меняться
ни байтом. Это и есть то, что 1.11 обещает пользователю, и обещание
без проверки рано или поздно перестанет быть правдой: появится
параметр, влияющий на препроцессинг, и пересчёт станет нужен, а
интерфейс по-прежнему будет молчать об этом.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from kurator.config import NEAR_DUP_COSINE_STRONG, NEAR_DUP_PHASH_DISTANCE
from kurator.db import Store
from kurator.web.server import ASSETS

APP_JS = ASSETS / "app.js"


def test_assets_carry_no_hardcoded_factory():
    """Заводские пороги приходят с сервера, а не зашиты в страницу.

    Ловит конкретную поломку: `factory` был инициализирован числами
    прямо в JavaScript. Пока страница не ответила, кнопка «сбросить»
    писала в индекс эти числа, и после первой правки порога в
    конфиге сброс молча возвращал прежнее значение.
    """
    source = APP_JS.read_text(encoding="utf-8")
    literals = re.findall(r"factory:\s*\{\s*phash:\s*([0-9.]+)", source)
    assert not literals, (
        "заводские пороги зашиты в app.js: "
        f"factory: {{ phash: {literals[0]} }}. "
        "Их надо брать из /api/state, иначе кнопка «сбросить» вернёт "
        "прежние значения после смены порога в конфиге."
    )


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
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
            return
        except Exception as exc:  # noqa: BLE001 — причина нужна в тесте
            last = exc
            time.sleep(0.05)
    raise AssertionError(f"сервер не поднялся: {last!r}")


def _token(port: int) -> str:
    page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode()
    return page.split('KURATOR_TOKEN = "')[1].split('"')[0]


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
            serve(root, port=port, open_browser=False)
        finally:
            finished.set()

    threading.Thread(target=run, daemon=True).start()
    _wait_for(port)
    yield port, root
    _post(port, _token(port), "/api/quit")
    assert finished.wait(10), "сервер не остановился после /api/quit"


def test_status_factory_matches_config(server):
    """`factory` в статусе совпадает с порогами из кода."""
    port, _ = server
    status = _get(port, _token(port), "/api/state")
    assert status["factory"]["phash"] == pytest.approx(NEAR_DUP_PHASH_DISTANCE)
    assert status["factory"]["cosine"] == pytest.approx(NEAR_DUP_COSINE_STRONG)


def test_defaults_reflect_stored_override(server):
    """Сохранённый порог виден в `defaults` и не выдаётся за заводской.

    Именно эта разница объясняет пользователю, что он настроил своё.
    """
    port, _ = server
    token = _token(port)
    _post(port, token, "/api/tuning", {"phash": 7, "cosine": 0.9})

    status = _get(port, token, "/api/state")
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
    token = _token(port)

    before = _rows(archive)
    _post(port, token, "/api/tuning", {"phash": 2, "cosine": 0.99})
    _post(port, token, "/api/tuning", {"phash": 10, "cosine": 0.8})
    after = _rows(archive)

    assert before == after, "смена порога изменила строки индекса"
    assert _get(port, token, "/api/state")["defaults"]["phash"] == 10