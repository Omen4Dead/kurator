#!/usr/bin/env python3
"""Проверить, что собранное приложение действительно работает.

Зачем это нужно. По `AGENTS.md`: «сборка проверяется не только тем, что
`.app` создался». Ошибки здесь уже случались, и выглядели они одинаково —
«приложение собралось, значит всё в порядке»:

- адрес модели при `sys.frozen` вёл внутрь бандла, а не в корень. И
  `onnxruntime` не уехал в бандл целиком — приложение открывалось и
  падало при первой индексации;
- libheif не уезжала в бандл: приложение запускалось, JPEG считались,
  а снимки с айфона молча уходили в «нечитаемые».

Приложение состоит из нативного интерфейса (Contents/MacOS/kurator-app)
и python-бэкенда (Contents/Resources/backend). Проверка запускает бэкенд
ровно так, как это делает приложение: процессом, с токеном в
KURATOR_TOKEN и аргументами `ui --port N`. Нативный бинарь в headless-CI
запустить нельзя, поэтому для него проверяется факт и архитектура.

Запуск (в каталоге, куда собрано приложение):

    uv run python packaging/verify_bundle.py dist/kurator.app
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TIMEOUT = 300.0

# Сколько ждать поднятия сервера. Первый запуск собранного приложения
# медленный: Gatekeeper проверяет двести мегабайт бандла, и короткий
# бюджет объявлял провалом то, что было медленным, а не сломанным.
STARTUP_BUDGET = 600.0

TOKEN = secrets.token_urlsafe(24)


def fail(message: str) -> None:
    print(f"ПРОВАЛ: {message}", file=sys.stderr)
    sys.exit(1)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def make_archive(root: Path) -> Path:
    """Архив с вложенной папкой, точным дубликатом и нестандартными форматами.

    Имена с пробелом и кириллицей — не украшение: они обязаны пройти через
    индекс и вернуться в ответах API в том виде, как на диске.

    HEIC и AVIF добавлены не для разнообразия. libheif — нативная
    библиотека, и ничто не мешает PyInstaller её не увезти: приложение
    запустится, эмбеддинги для JPEG посчитаются, а снимки с айфона молча
    уйдут в «нечитаемые», и проверка будет зелёной. Единственный способ
    это поймать — открыть такой файл собранным приложением.
    """
    album = root / "Photos from 2024" / "Отпуск"
    album.mkdir(parents=True, exist_ok=True)

    source = Path(__file__).resolve().parent.parent / "assets" / "icon-256.png"
    if not source.is_file():
        fail(f"нет исходной картинки: {source}")

    first = album / "IMG_0001.png"
    shutil.copyfile(source, first)
    # Точный дубликат: без него карантин нечего проверять.
    shutil.copyfile(source, album / "IMG_0001 (1).png")

    # Форматы, которые читает не сам Pillow, а подключённая библиотека.
    _write_sample(source, album / "IMG_0002.heic", "HEIF")
    _write_sample(source, album / "IMG_0003.avif", "AVIF")

    return first


def _write_sample(source: Path, target: Path, fmt: str) -> None:
    """Переписать исходную картинку в формат, который kurator обещает."""
    from PIL import Image, features

    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except ImportError:
        fail(
            "в проверяющем окружении нет pillow-heif, а проверка обязана "
            "создать настоящий файл этого формата"
        )

    if fmt == "AVIF" and not features.check("avif"):
        fail("Pillow в проверяющем окружении не умеет писать AVIF")

    with Image.open(source) as img:
        img.convert("RGB").save(target, format=fmt)


def _app_output(process: subprocess.Popen) -> str:
    """Что приложение успело написать в stdout и stderr.

    Трубу мы сами же перенаправляем в stderr — и обязаны читать её при
    любом исходе, а не только когда сервер не поднялся. Иначе в отчёт
    попадает голое «завершилось с кодом -1» без единого слова о причине.
    """
    try:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=30)
    except Exception as exc:
        return f"(не дождались конца процесса: {exc!r})"
    try:
        if process.stdout is None:
            return "(вывод приложения не перенаправлен)"
        text = process.stdout.read().strip()
    except Exception as exc:
        return f"(вывод приложения не прочитан: {exc!r})"
    return text or "(пусто)"


def _block(title: str, body: str) -> str:
    return f"\n\n{title}:\n" + "\n".join(f"      {ln}" for ln in body.splitlines())


def wait_for_server(process: subprocess.Popen, port: int) -> None:
    """Дождаться сервера, печатая ход ожидания.

    Бюджет ждать долго: первая проверка бандла системой не мгновенна, и
    на CI «долго» выглядело так, будто приложение зависло намертво, — а
    на деле оно ещё разворачивалось. Поэтому каждые полминуты говорим,
    где процесс находится: без этого «долго» и «зависло» неразличимы.
    """
    started = time.time()
    deadline = started + STARTUP_BUDGET
    last: object = None
    next_report = started + 30
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/state",
        headers={"X-Kurator-Token": TOKEN},
    )
    while time.time() < deadline:
        if process.poll() is not None:
            fail(
                f"бэкенд завершился с кодом {process.returncode} "
                "до поднятия сервера"
                + _block("бэкенд написал сам", _app_output(process))
                + _error_report()
            )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                if response.status == 200:
                    if time.time() - started > 20:
                        print(f"  сервер поднялся за {time.time() - started:.0f} с")
                    return
                last = f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            # Сервер ответил — значит сокет слушает и процесс жив. Даже
            # ошибка — это ответ; причину скажет проверка содержимого.
            last = f"HTTP {exc.code}"
            if exc.code == 403:
                fail("сервер отказал в токене: KURATOR_TOKEN не доехал до бэкенда")
            time.sleep(0.4)
        except Exception as exc:
            last = exc
            if time.time() >= next_report:
                next_report = time.time() + 30
                print(
                    f"  жду уже {time.time() - started:.0f} с, "
                    f"процесс жив: {process.poll() is None}, "
                    f"последняя причина: {exc!r}"
                )
            time.sleep(0.4)
    fail(
        f"сервер не поднялся за {STARTUP_BUDGET:.0f} секунд, "
        f"последняя причина: {last!r}\n\n{diagnostics(process, port)}" + _error_report()
    )


def diagnostics(process: subprocess.Popen, port: int) -> str:
    """Показать, где именно приложение остановилось.

    «Не поднялся» — не причина, а наблюдение, и оно одинаково выглядит
    при трёх совершенно разных поломках: приложение упало, приложение
    висит до сокета, или сокет слушает, а пакеты до него не доходят.
    Разбираться приходилось вслепую — по одному этому сообщению.

    Три прибора, каждый отвечает на свой вопрос:

    - след старта: приложение само пишет, на каком шаге находится, —
      единственный способ узнать, где оно застряло, если застряло;
    - список слушающих портов: видно, слушает ли вообще кто-то на этом
      порту, и не занят ли он чужим процессом;
    - контрольный сервер в самом проверяющем процессе: если и он не
      отвечает, то дело не в kurator, а в среде — например, в брандмауэре,
      молча роняющем пакеты.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    lines: list[str] = []

    # Жив ли процесс — надо узнать ДО того, как мы его убьём ниже.
    was_alive = process.poll() is None

    # Затем читаем то, что приложение успело написать: труба закрывается
    # только вместе с процессом, и до его убийства чтение не вернётся.
    said = _app_output(process)
    lines.append(
        "бэкенд написал сам:\n"
        + ("\n".join("      " + ln for ln in said.splitlines()) or "      (пусто)")
    )

    trace = Path.home() / ".kurator" / "startup-trace.log"
    if trace.is_file():
        try:
            body = trace.read_text(encoding="utf-8").rstrip()
            lines.append(
                "след старта (последняя строка — где застряло):\n"
                + "\n".join("      " + ln for ln in body.splitlines())
            )
        except OSError as exc:
            lines.append(f"след старта не прочитан: {exc!r}")
    else:
        lines.append("след старта НЕ создан — процесс не дошёл даже до входа в serve()")

    lines.append(f"процесс жив: {was_alive}, pid {process.pid}")

    try:
        with socket.socket() as probe:
            taken = probe.connect_ex(("127.0.0.1", port)) == 0
        lines.append(f"порт {port} слушается: {'да' if taken else 'нет'}")
    except Exception as exc:
        lines.append(f"порт проверить не удалось: {exc!r}")

    class _Quiet(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — имя задано BaseHTTPRequestHandler
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, fmt: str, *args) -> None:
            pass

    control = ThreadingHTTPServer(("127.0.0.1", 0), _Quiet)
    threading.Thread(target=control.serve_forever, daemon=True).start()
    try:
        with socket.create_connection(("127.0.0.1", control.server_address[1]), 5) as sock:
            sock.sendall(b"GET / HTTP/1.0\r\n\r\n")
            got = sock.recv(16)
        lines.append(
            "контрольный сервер в этой же среде: "
            + ("отвечает" if b"200" in got else f"ответил странно {got!r}")
        )
    except Exception as exc:
        lines.append(f"контрольный сервер НЕ отвечает: {exc!r} — дело в среде, не в kurator")
    finally:
        control.shutdown()
        control.server_close()

    return "\n".join(f"  {line}" for line in lines)


def _error_report() -> str:
    """Показать след падения, если приложение его оставило.

    Оконное приложение не пишет в stderr — `sys.stderr` у него равен
    `None`, и исключение просто исчезает. Поэтому `desktop.py`
    оставляет файл, и без его чтения падение выглядит как «сервер не
    поднялся» без единого слова о причине.
    """
    path = Path.home() / ".kurator" / "last-error.log"
    if not path.is_file():
        return ""
    try:
        return "\n\nприложение оставило след:\n" + path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _utf8_stdout() -> None:
    """Научить консоль печатать по-русски независимо от окружения."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main() -> None:
    _utf8_stdout()

    if len(sys.argv) < 2:
        fail("укажи собранное приложение: verify_bundle.py dist/kurator.app")
    bundle = Path(sys.argv[1])
    if not bundle.is_dir():
        fail(f"нет каталога {bundle}")

    contents = bundle / "Contents"
    frontend = contents / "MacOS" / "kurator-app"
    backend = contents / "Resources" / "backend" / "kurator"

    # 0. Структура: обе половины на своих местах.
    if not frontend.is_file():
        fail(f"в бандле нет нативного интерфейса: {frontend}")
    if not backend.is_file():
        fail(f"в бандле нет python-бэкенда: {backend}")
    info = subprocess.run(
        ["file", str(frontend)], capture_output=True, text=True, timeout=30
    ).stdout
    if "arm64" not in info:
        fail(f"интерфейс собран не под Apple Silicon: {info.strip()}")
    print("  интерфейс и бэкенд на месте, архитектура arm64")

    # Каталог НЕ начинается с точки: `walk_media` пропускает скрытые
    # каталоги, и архив с точкой оказался бы для приложения невидимым —
    # проверка падала бы с «не увидело архив», а причина была бы в имени.
    archive = make_archive(bundle / "verify-archive")
    print(f"архив для проверки: {archive.parent}")

    port = free_port()
    # Бэкенд запускается ровно так, как его запускает приложение:
    # токен в переменной окружения, порт аргументом.
    process = subprocess.Popen(
        [str(backend), "ui", "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "KURATOR_TRACE": "1", "KURATOR_TOKEN": TOKEN},
    )

    def api(path: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=body,
            headers={"X-Kurator-Token": TOKEN, "Content-Type": "application/json"},
            method="POST" if payload is not None else "GET",
        )
        return json.loads(urllib.request.urlopen(request, timeout=600).read().decode())

    try:
        wait_for_server(process, port)

        # 1. Без токена API молчит: это и защита, и проверка, что
        # авторизация не отвалилась при сборке.
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5)
            fail("API ответил без токена — защиты нет")
        except urllib.error.HTTPError as exc:
            if exc.code != 403:
                fail(f"без токена ждали 403, получили {exc.code}")
        print("  без токена API не отвечает")

        # Приложение стартует без папки: при первом запуске её предлагают
        # выбрать, а дальше берётся последняя. Поэтому корень надо задать
        # явно, иначе проверка смотрела бы на чужую папку пользователя.
        info = api("/api/root", {"path": str(archive.parent)})
        print(f"  корень задан: {info.get('root')}")

        state = api("/api/state")
        if not state.get("model", {}).get("present"):
            fail("модель не найдена: весы не уехали в бандл")

        # 2. Индексация и эмбеддинги. Единственная проверка, которая
        # отличает рабочую сборку от той, что падает на первом кадре:
        # onnxruntime должен реально посчитать векторы.
        #
        # Смена корня запускает индексацию сама, и она асинхронная: счётчик
        # файлов в ответе пока ноль, потому что индекс ещё пуст. Проверять
        # надо после окончания job, а не сразу.
        deadline = time.time() + TIMEOUT
        job: dict = {}
        while time.time() < deadline:
            job = api("/api/job")
            if job.get("status") == "done":
                break
            if job.get("status") == "error":
                fail(f"индексация упала: {job.get('error')}")
            time.sleep(1.0)
        else:
            fail(f"индексация не закончилась за {TIMEOUT:.0f} с: {job}")

        snapshot = api("/api/state")
        files = snapshot.get("files", 0)
        embedded = snapshot.get("embedded", 0)
        print(f"  проиндексировано файлов: {files}, эмбеддингов: {embedded}")

        if files < 4:
            fail(f"приложение увидело {files} файлов вместо четырёх — обход папок сломан")
        if embedded != files:
            # Ровно этот случай тут и ловится: файлы HEIC и AVIF будут
            # посчитаны, но останутся без эмбеддинга, если libheif не
            # уехала в бандл. Приложение при этом выглядит исправным.
            fail(
                f"эмбеддингов {embedded} при {files} файлах — не все форматы прочитаны. "
                "Почти всегда это нативная библиотека, которая не уехала в бандл: "
                "без неё снимки молча попадают в «нечитаемые». "
                f"Подробности: {snapshot.get('skipped')}"
            )

        # 3. Имена файлов: обратные слэши в ответах означают, что
        # rel_path уехал в бандл в сыром виде.
        findings = api("/api/findings")
        blob = str(findings)
        if "IMG_0001" not in blob:
            fail(f"в находках нет имени файла — интерфейс покажет пустые заголовки: {blob[:300]}")
        if "\\" in blob:
            fail(f"в находках остались обратные слэши: {blob[:300]}")
        print("  имена файлов корректны, разделители прямые")

        print()
        print("ВСЁ ПРОВЕРЕНО: запустилось, ответило по API, посчитало эмбеддинги,")
        print("показало верные имена файлов.")
    except BaseException as exc:  # noqa: BLE001 — причина нужна в отчёте
        # След падения читался только если сервер не поднялся. Но приложение
        # может умереть и позже — посреди проверки, на середине запроса к API —
        # и тогда след не читался никем, а в лог падало голое
        # ConnectionAbortedError без единого слова о причине.
        print()
        print(f"ПРОВАЛ: {type(exc).__name__}: {exc}")
        report = _error_report()
        print(report if report else "\nприложение не оставило след падения")
        tail = _app_output(process)
        if tail:
            print("\nвывод бэкенда:\n" + tail)
        raise
    finally:
        # Гасим бэкенд. Если не вышло — снимаем процесс, иначе джоб
        # в CI повиснет на нём.
        try:
            api("/api/quit", {})
            process.wait(timeout=20)
        except Exception:
            process.kill()
            process.wait(timeout=10)

        shutil.rmtree(bundle / "verify-archive", ignore_errors=True)


if __name__ == "__main__":
    main()
