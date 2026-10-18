#!/usr/bin/env python3
"""Проверить, что собранное приложение действительно работает.

Зачем это нужно. По `AGENTS.md`: «сборка проверяется не только тем, что
`.app` создался». Уже было две ошибки, и обе выглядели как «приложение
собралось, значит всё в порядке»:

- веб-интерфейс — это три файла, которые сервер читает с диска. Про
  импорты их PyInstaller не видит, и без явного `datas` в spec
  приложение запускается и отдаёт 404 на всё подряд;
- адрес модели при `sys.frozen` вёл внутрь бандла, а не в корень. И
  `onnxruntime` не уехал в бандл целиком — приложение открывалось и
  падало при первой индексации.

Поэтому проверка состоит из четырёх пунктов, и пропуск любого означает
провал: запустилось, отдало главную страницу, посчитало эмбеддинги,
показало имена файлов.

Запуск (в каталоге, куда собрано приложение):

    uv run python packaging/verify_bundle.py dist/kurator
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Порт, который выдаёт обычный запуск. Проверять надо именно его: если
# сборка поднимется только на нестандартном порту, пользователь всё равно
# увидит пустую страницу.
PORT = 8765
TIMEOUT = 300.0

# Сколько ждать поднятия сервера. На Windows первый запуск собранного
# приложения занимает минуты: защитник проверяет двести мегабайт бандла, и
# каждое открытие файла ждёт очереди. 120 секунд на это не хватало, и
# проверка объявляла провалом то, что было медленным, а не сломанным.
STARTUP_BUDGET = 900.0


def fail(message: str) -> None:
    print(f"ПРОВАЛ: {message}", file=sys.stderr)
    sys.exit(1)


def make_archive(root: Path) -> Path:
    """Архив с вложенной папкой, точным дубликатом и нестандартными форматами.

    Имена с пробелом и кириллицей — не украшение: разделитель пути в
    собранном приложении на Windows обязан оставаться прямым, а имена
    показываться как на диске. Проверяется именно этим деревом.

    HEIC и AVIF добавлены не для разнообразия. libheif — нативная
    библиотека, и ничто не мешает PyInstaller её не увезти: приложение
    запустится, страница отдастся, эмбеддинги для JPEG посчитаются, а
    снимки с айфона молча уйдут в «нечитаемые», и проверка будет
    зелёной. Единственный способ это поймать — открыть такой файл
    собранным приложением.
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
    любом исходе, а не только когда сервер не поднялся. Консольный
    проверочный вариант именно так и объясняет поломку: печатает её и
    выходит с кодом -1, а текст мы выбрасывали и получали голое
    «завершилось с кодом 4294967295».
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


def wait_for_server(process: subprocess.Popen) -> None:
    """Дождаться сервера, печатая ход ожидания.

    Бюджет в 120 секунд оказался мал: на Windows сборка стартует минуты,
    и это не поломка. На CI это выглядело так, будто приложение зависло
    намертво, — а на деле оно ещё разворачивало и сканировалось
    защитником. Поэтому ждём дольше и каждые полминуты говорим, где
    процесс находится: без этого «долго» и «зависло» неразличимы.
    """
    started = time.time()
    deadline = started + STARTUP_BUDGET
    last: object = None
    next_report = started + 30
    while time.time() < deadline:
        if process.poll() is not None:
            fail(
                f"приложение завершилось с кодом {process.returncode} "
                "до поднятия сервера"
                + _block("приложение написало само", _app_output(process))
                + _error_report()
            )
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=2)
            if time.time() - started > 20:
                print(f"  сервер поднялся за {time.time() - started:.0f} с")
            return
        except urllib.error.HTTPError as exc:
            # Сервер ответил — значит сокет слушает и процесс жив. Что он
            # отдал, скажет проверка содержимого. Раньше такой ответ
            # считался «сервер не поднялся», и настоящая причина пряталась
            # за выдуманной.
            last = f"HTTP {exc.code}"
            return
        except Exception as exc:
            last = exc
            if time.time() >= next_report:
                next_report = time.time() + 30
                print(
                    f"  жду уже {time.time() - started:.0f} с, "
                    f"процесс жив: {process.poll() is None}, "
                    f"{_instance_note()}, последняя причина: {exc!r}"
                )
            time.sleep(0.4)
    fail(
        f"сервер не поднялся за {STARTUP_BUDGET:.0f} секунд, "
        f"последняя причина: {last!r}\n\n{diagnostics(process)}" + _error_report()
    )


def _instance_note() -> str:
    path = Path.home() / ".kurator" / "instance.json"
    return "экземпляр записан" if path.is_file() else "до сокета не дошёл"


def diagnostics(process: subprocess.Popen) -> str:
    """Показать, где именно приложение остановилось.

    «Не поднялся» — не причина, а наблюдение, и оно одинаково выглядит
    при трёх совершенно разных поломках: приложение упало, приложение
    висит до сокета, или сокет слушает, а пакеты до него не доходят.
    Разбираться приходилось вслепую — по одному этому сообщению.

    Три прибора, каждый отвечает на свой вопрос:

    - файл экземпляра: пишется сразу после привязки сокета и до
      `serve_forever`. Есть — значит процесс жив и сервер уже слушает,
      и дело не в приложении;
    - след старта: приложение само пишет, на каком шаге находится, —
      единственный способ узнать, где оно застряло, если застряло;
    - список слушающих портов: видно, слушает ли вообще кто-то на этом
      порту, и не занят ли он чужим процессом;
    - контрольный сервер в самом проверяющем процессе: если и он не
      отвечает, то дело не в kurator, а в среде — например, в брандмауэре,
      молча роняющем пакеты.
    """
    import json
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    lines: list[str] = []

    # Жив ли процесс — надо узнать ДО того, как мы его убьём ниже.
    was_alive = process.poll() is None

    # Смотрим на процесс, пока он ещё жив: после убийства Windows уже не
    # ответит ни на что.
    lines.extend(_windows_reports(process))

    # Затем читаем то, что приложение успело написать: труба закрывается
    # только вместе с процессом, и до его убийства чтение не вернётся.
    said = _app_output(process)
    lines.append(
        "приложение написало само:\n"
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

    instance = Path.home() / ".kurator" / "instance.json"
    lines.append(f"процесс жив: {was_alive}, pid {process.pid}")
    if instance.is_file():
        try:
            data = json.loads(instance.read_text(encoding="utf-8"))
            lines.append(
                f"файл экземпляра есть: pid {data.get('pid')}, порт {data.get('port')} "
                f"— значит сервер уже привязан и дело не в запуске"
            )
        except (OSError, ValueError):
            lines.append("файл экземпляра есть, но прочитать его не удалось")
    else:
        lines.append("файл экземпляра НЕ создан — до привязки сокета не дошли")

    try:
        netstat = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=30
        ).stdout
        listening = [ln.strip() for ln in netstat.splitlines() if f":{PORT}" in ln]
        lines.append(
            "слушает на порту: " + ("; ".join(listening[:4]) or "никто не слушает")
        )
    except Exception as exc:
        lines.append(f"netstat не отработал: {exc!r}")

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


def _windows_reports(process: subprocess.Popen) -> list[str]:
    """Что Windows думает о зависшем процессе.

    На Windows процесс умеет висеть молча: загрузчик ждёт окна, которого
    в сессии без рабочего стола нет, и сообщения об этом не печатает ни
    один. Три прибора отвечают на разные вопросы: съел ли защитник файл,
    было ли что-то в журнале событий и — главное — работает ли процесс
    вообще, что видно по времени CPU и по `Responding`.
    """
    if os.name != "nt":
        return []

    def ps(command: str) -> str:
        try:
            done = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                timeout=60,
            )
            return (done.stdout or done.stderr).strip()
        except Exception as exc:
            return f"не отработало: {exc!r}"

    return [
        "процесс глазами Windows: "
        + ps(
            f"$p = Get-Process -Id {process.pid} -ErrorAction SilentlyContinue; "
            "if (-not $p) { 'процесса нет' } else { "
            # `Responding` есть только у процесса с окном; спрашивать его
            # у консольного варианта значит поймать ошибку вместо ответа.
            "$r = try { $p.Responding } catch { 'нет окна' }; "
            "'CPU=' + [math]::Round($p.CPU, 1) + ' с, потоков=' + $p.Threads.Count "
            "+ ', отвечает=' + $r "
            "+ ', памяти=' + [math]::Round($p.WorkingSet64 / 1MB) + ' МБ' }"
        ),
        "съел ли защитник что-нибудь: "
        + ps(
            "$d = Get-MpThreatDetection -ErrorAction SilentlyContinue "
            "| Select-Object -First 3 "
            "| ForEach-Object { $_.Resources -join ',' }; "
            "if ($d) { $d -join '; ' } else { 'нет' }"
        ),
        "журнал событий: "
        + ps(
            "Get-WinEvent -FilterHashtable @{LogName='Application'; Level=2} -MaxEvents 5 "
            "-ErrorAction SilentlyContinue "
            "| ForEach-Object { $_.Message.Split([Environment]::NewLine)[0] }"
        ).replace("\n", " | "),
    ]


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


def get(path: str) -> str:
    return urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=30).read().decode()


def _utf8_stdout() -> None:
    """Научить консоль печатать по-русски.

    На Windows stdout по умолчанию cp1252, и русский текст в нём не
    представим: скрипт падал с UnicodeEncodeError на первой же строке
    вывода. Проверка собра��ного приложения на Windows не прошла бы
    именно из-за этого — то есть проверяла бы не то.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main() -> None:
    _utf8_stdout()

    if len(sys.argv) < 2:
        fail("укажи каталог собранного приложения: verify_bundle.py dist/kurator")
    bundle = Path(sys.argv[1])
    if not bundle.is_dir():
        fail(f"нет каталога {bundle}")

    # Ищем исполняемый файл. Раскладка разная: на Windows это kurator.exe
    # рядом с каталогом сборки, на macOS — внутри .app, и передать можно
    # как сам .app, так и каталог, в котором он лежит. onedir-сборка —
    # это kurator/kurator внутри dist.
    candidates = [
        # передан сам .app
        bundle / "Contents" / "MacOS" / "kurator",
        # передан каталог сборки
        bundle / "kurator" / "Contents" / "MacOS" / "kurator",
        bundle / "kurator.exe",
        bundle / "kurator",
        bundle / "kurator" / "kurator",
    ]
    exe = next((path for path in candidates if path.is_file()), None)
    if exe is None:
        found = sorted(str(p.relative_to(bundle)) for p in bundle.glob("**/*") if p.is_file())
        fail(
            f"в {bundle} нет исполняемого файла kurator. Что там есть: "
            f"{found[:12]}"
        )

    print(f"проверяю {exe}")

    # Каталог НЕ начинается с точки: `walk_media` пропускает скрытые
    # каталоги, и архив с точкой оказался бы для приложения невидимым —
    # проверка падала бы с «не увидело архив», а причина была бы в имени.
    archive = make_archive(bundle / "verify-archive")
    print(f"архив для проверки: {archive.parent}")

    process = subprocess.Popen(
        [str(exe)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "KURATOR_TRACE": "1"},
    )

    try:
        wait_for_server(process)

        # 1. Главная страница. Именно здесь ловится забытый `datas`:
        # приложение отвечает, но отдаёт 404 на всё подряд.
        page = get("/")
        if "<html" not in page.lower():
            fail("главная страница не похожа на HTML — веб-ассеты не попали в бандл")
        if "kurator" not in page.lower():
            fail("на главной странице нет ни слова о kurator")
        print("  главная страница отдаётся")

        for asset in ("/app.js", "/style.css"):
            body = get(asset)
            if len(body) < 100:
                fail(f"{asset} пустой или обрезан — ассет не уехал в бандл")
        print("  app.js и style.css на месте")

        # 2. Токен из страницы: без него API не отвечает, и это не поломка.
        token = page.split('KURATOR_TOKEN = "')[1].split('"')[0]

        def api(path: str, payload: dict | None = None) -> dict:
            import json

            body = json.dumps(payload or {}).encode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{PORT}{path}",
                data=body,
                headers={
                    "X-Kurator-Token": token,
                    "Content-Type": "application/json",
                },
                method="POST" if payload is not None else "GET",
            )
            return json.loads(urllib.request.urlopen(request, timeout=600).read().decode())

        # Приложение стартует без папки: при первом запуске её предлагают
        # выбрать, а дальше берётся последняя. Поэтому корень надо задать
        # явно, иначе проверка смотрела бы на чужую папку пользователя.
        info = api("/api/root", {"path": str(archive.parent)})
        print(f"  корень задан: {info.get('root')}")

        state = api("/api/state")
        if not state.get("model", {}).get("present"):
            fail("модель не найдена: весы не уехали в бандл")

        # 3. Индексация и эмбеддинги. Единственная проверка, которая
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

        # 4. Имена файлов: обратные слэши в интерфейсе означают, что
        # rel_path уехал в бандл в сыром виде.
        findings = api("/api/findings")
        blob = str(findings)
        if "IMG_0001" not in blob:
            fail(f"в находках нет имени файла — интерфейс покажет пустые заголовки: {blob[:300]}")
        if "\\" in blob:
            fail(f"в находках остались обратные слэши: {blob[:300]}")
        print("  имена файлов корректны, разделители прямые")

        print()
        print("ВСЁ ПРОВЕРЕНО: запустилось, отдало страницу, посчитало эмбеддинги,")
        print("показало верные имена файлов.")
    finally:
        # Гасим приложение. Если не вышло — снимаем процесс, иначе джоб
        # в CI повиснет на нём.
        try:
            import json

            import urllib.request as ur

            page = ur.urlopen(f"http://127.0.0.1:{PORT}/", timeout=5).read().decode()
            token = page.split('KURATOR_TOKEN = "')[1].split('"')[0]
            req = ur.Request(
                f"http://127.0.0.1:{PORT}/api/quit",
                data=b"{}",
                headers={"X-Kurator-Token": token, "Content-Type": "application/json"},
                method="POST",
            )
            ur.urlopen(req, timeout=5)
            process.wait(timeout=20)
        except Exception:
            process.kill()
            process.wait(timeout=10)

        shutil.rmtree(bundle / "verify-archive", ignore_errors=True)


if __name__ == "__main__":
    main()