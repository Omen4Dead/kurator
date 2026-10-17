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


def fail(message: str) -> None:
    print(f"ПРОВАЛ: {message}", file=sys.stderr)
    sys.exit(1)


def make_archive(root: Path) -> Path:
    """Архив с вложенной папкой и точным дубликатом.

    Имена с пробелом и кириллицей — не украшение: разделитель пути в
    собранном приложении на Windows обязан оставаться прямым, а имена
    показываться как на диске. Проверяется именно этим деревом.
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

    return first


def wait_for_server(process: subprocess.Popen) -> None:
    deadline = time.time() + 120
    while time.time() < deadline:
        if process.poll() is not None:
            fail(f"приложение завершилось с кодом {process.returncode} до поднятия сервера")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=2)
            return
        except Exception:
            time.sleep(0.4)
    fail("сервер не поднялся за 120 секунд" + _error_report())


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

        if files < 2:
            fail(f"приложение увидело {files} файлов вместо двух — обход папок сломан")
        if embedded < 2:
            fail(
                f"эмбеддингов {embedded}, ожидалось 2 — модель отработала не полностью. "
                "Почти всегда это onnxruntime, который не уехал в бандл целиком."
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