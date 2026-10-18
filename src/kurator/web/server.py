"""HTTP-сервер и API локального интерфейса kurator.

Только стандартная библиотека. Слушает исключительно 127.0.0.1 и требует
сессионный токен в заголовке, чтобы случайная страница в браузере не могла
обратиться к локальному серверу.
"""

from __future__ import annotations

import io
import json
import mimetypes
import os
import re
import sqlite3
import secrets
import threading
import time
import urllib.request
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

from .. import __version__
from ..config import (
    BATCH_SIZE,
    CLUSTER_COSINE,
    IMAGE_EXTENSIONS,
    MODEL_FILENAME,
    MODEL_REPO,
    NEAR_DUP_COSINE_STRONG,
    NEAR_DUP_PHASH_DISTANCE,
    QUARANTINE_DIRNAME,
    default_model_path,
)
from ..db import Store
from ..encoder import SiglipVision, model_is_present

ASSETS = Path(__file__).resolve().parent / "assets"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}

THUMB_CACHE_LIMIT = 400
THUMB_WIDTHS = (128, 256, 512)

TRACE_FILENAME = "startup-trace.log"
_trace_lines: list[str] = []


def trace(message: str) -> None:
    """Отметить, где находится старт.

    Включается переменной окружения `KURATOR_TRACE=1` и пользователю не
    мешает. Нужен там, где окна нет: в CI и на чужой машине приложение
    молча не открывается, и «не открылось» ничем не отличается от
    «зависло на импорте библиотеки» — а это разные поломки с разными
    причинами.
    """
    if os.environ.get("KURATOR_TRACE") != "1":
        return
    _trace_lines.append(f"{time.time():9.1f}  {message}")
    try:
        (Path.home() / ".kurator" / TRACE_FILENAME).write_text(
            "\n".join(_trace_lines) + "\n", encoding="utf-8", errors="replace"
        )
    except OSError:
        pass


trace("модуль kurator.web.server загружен")


HISTORY_FILENAME = "roots.json"
INSTANCE_FILENAME = "instance.json"

DEFAULT_PORT = 8765
# Сколько ждать, пока вкладка не появится, и сколько после её закрытия.
# Приложение без вкладки — процесс без работы: он держит модель и индекс
# в памяти, но пользы от него нет. Ждать нужно не мгновенно: перезагрузка
# страницы и переключение между двумя вкладками тоже выглядят как закрытие.
IDLE_SHUTDOWN_SECONDS = 180


def state_dir() -> Path:
    return Path.home() / ".kurator"


def history_path() -> Path:
    return state_dir() / HISTORY_FILENAME


def instance_path() -> Path:
    return state_dir() / INSTANCE_FILENAME


def write_instance(payload: dict) -> None:
    """Оставить след живого экземпляра, чтобы второй запуск его нашёл.

    Токен из этого файла открывает интерфейс, поэтому права заужены: файл
    лежит в домашней папке и читать его должен только владелец.
    """
    path = instance_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        # Записать не смогли — работаем без следа. Приложение при этом
        # запускается, просто второй запуск его не найдёт и скажет об
        # этом прямо.
        pass


def clear_instance(pid: int | None = None) -> None:
    """Снять след, но только свой.

    Без проверки pid след стирал любой, кто первым остановится: два
    запущенных kurator — с разными папками — мешали бы друг другу, и
    следующий щелчок по значку решил бы, что приложение уже работает, а
    второго процесса нет. Заодно это делало тесты недетерминированными:
    сервер из прошлого теста мог снести след текущего.
    """
    path = instance_path()
    if pid is not None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict) or payload.get("pid") != pid:
            return
    try:
        path.unlink()
    except OSError:
        pass


def live_instance() -> dict | None:
    """Есть ли уже работающий kurator и чем его открыть.

    Проверяется не pid, а живой ответ на порту: `os.kill(pid, 0)` на
    Windows означает `TerminateProcess`, и такая проверка убила бы чужой
    процесс. Заодно она ничего не скажет, если экземпляр погиб, не сняв
    след с диска.

    Тело страницы проверяется на `KURATOR_TOKEN`: порт мог занять кто-то
    ещё, и открывать пользователю чужую страницу нельзя.
    """
    try:
        payload = json.loads(instance_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    url = payload.get("url") if isinstance(payload, dict) else None
    if not isinstance(url, str) or not url:
        return None

    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            # Страница читается целиком: метка с токеном стоит в самом
            # конце index.html, и при чтении первых 4 КБ её не видно —
            # поиск молча проваливался, и второй запуск падал на занятом
            # порту точно так же, как до появления этой проверки.
            body = response.read(65536).decode("utf-8", "replace")
    except Exception:
        return None
    if "KURATOR_TOKEN" not in body:
        return None
    return payload


def read_history() -> list[str]:
    path = history_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [p for p in payload if isinstance(p, str)]


def remember_root(root: Path) -> None:
    target = history_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    key = str(root)
    history = [p for p in read_history() if p != key]
    history.insert(0, key)
    target.write_text(json.dumps(history[:20], ensure_ascii=False, indent=2), encoding="utf-8")


BLOCKED_ROOTS = {
    "/", "/bin", "/boot", "/dev", "/etc", "/lib", "/private", "/proc",
    "/sbin", "/System", "/sys", "/usr", "/var",
}

# На Windows корни томов — `C:\`, `D:\` и так далее. Их тут быть не может:
# список задан строками, и буквы дисков заранее неизвестны. Ловим на лету.
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]?$")


def too_broad(path: Path) -> bool:
    """Корень, где kurator бессмысленно полезет по всей системе."""
    raw = str(path)
    if raw in BLOCKED_ROOTS:
        return True
    if _WINDOWS_DRIVE_RE.match(raw):
        return True
    # На Windows `Path("/")` — это корень текущего диска, а не корень
    # файловой системы. Он не проходит ни по списку POSIX-путей, ни под
    # регулярку выше, и пользователь указал бы kurator на весь диск C:.
    # Голый разделитель без буквы диска блокируем на всех системах: на
    # POSIX это и так корень, а на Windows — корень тома.
    if raw in ("\\", "/", "\\/", "//"):
        return True
    # Пути, которых нет, `is_dir` не роняет, а вот `iterdir` на папке,
    # в которую нельзя войти, — может. Проверка «пустая ли папка» не должна
    # ронять выбор папки в интерфейсе.
    try:
        if not path.is_dir():
            return False
        next(iter(path.iterdir()), None)
        return False
    except OSError:
        return True


def index_status(path: Path) -> dict | None:
    """Что уже известно про папку. Только чтение, ничего не считаем."""
    db = path / ".kurator" / "index.db"
    if not db.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        try:
            files = int(conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0])
            embedded = int(
                conn.execute("SELECT COUNT(*) FROM photos WHERE embed IS NOT NULL").fetchone()[0]
            )
            skipped = int(
                conn.execute("SELECT COUNT(*) FROM decisions WHERE action='skip'").fetchone()[0]
            )
            row = conn.execute("SELECT v FROM meta WHERE k='last_index_at'").fetchone()
            last = int(row[0]) if row and str(row[0]).isdigit() else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return {"files": files, "embedded": embedded, "skipped": skipped, "last_index_at": last}


def candidate_roots() -> list[dict]:
    """Что имеет смысл предложить выбрать: история, типовые папки, диски."""
    seen: set[str] = set()
    out: list[dict] = []

    def add(path: Path, kind: str) -> None:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            return
        key = str(resolved)
        if key in seen or key in BLOCKED_ROOTS:
            return
        if not resolved.is_dir():
            return
        seen.add(key)
        entry = {"path": key, "kind": kind}
        status = index_status(resolved)
        if status:
            entry["index"] = status
        out.append(entry)

    for remembered in read_history():
        add(Path(remembered), "недавний")

    home = Path.home()
    for name in ("Pictures", "Фото", "Photos", "Downloads", "Desktop", "Documents"):
        add(home / name, "домашняя папка")
    add(home, "домашняя папка")

    for entry in sorted(home.glob("*[.]?*[Dd]isk*")) + sorted(home.glob("*Облак*")):
        add(entry, "облачный диск")

    volumes = Path("/Volumes")
    if volumes.is_dir():
        for entry in sorted(volumes.iterdir()):
            add(entry, "диск")

    if home.is_dir():
        try:
            images = 0
            for entry in home.iterdir():
                if entry.is_dir():
                    images += sum(1 for _ in entry.glob("*.jpg"))
            if images:
                add(home, "домашняя папка")
        except OSError:
            pass

    return out


class _Server(ThreadingHTTPServer):
    """Сервер без обратного DNS-поиска.

    `http.server.HTTPServer.server_bind()` зовёт `socket.getfqdn(host)`,
    чтобы записать `server_name`. Это обратный DNS-запрос, и на машине с
    медленным резолвером он висит секундами: сокет уже слушает, но
    `serve_forever()` ещё не вызван, поэтому приложение выглядит зависшим,
    а в терминале нет ни слова об ошибке.

    Локальному интерфейсу имя хоста не нужно: он слушает только
    `127.0.0.1`, и никто не обращается к нему по имени. Поэтому
    `server_bind` берём из `TCPServer` — без поиска.
    """

    daemon_threads = True

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


class State:
    def __init__(self, root: Path, model_path: Path) -> None:
        self.model_path = model_path
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.RLock()
        self.encoder: SiglipVision | None = None
        self.job: dict | None = None
        self.thumbs: OrderedDict[tuple[str, int], bytes] = OrderedDict()
        self._stop = threading.Event()
        # Сколько вкладок сейчас открыто. Каждая представляется при загрузке
        # и прощается при закрытии; по этому счётчику сторож понимает, что
        # приложением больше не пользуются.
        self._clients = 0
        self.root = root.resolve()
        self.store = Store.open(self.root)
        # Как погасить сервер. Заполняется в serve(): сам сервер туда
        # положить не может, он появляется позже.
        self.stop_server: Callable[[], None] | None = None

    @property
    def clients(self) -> int:
        with self.lock:
            return self._clients

    def add_client(self) -> int:
        """Вкладка открылась."""
        with self.lock:
            self._clients += 1
            return self._clients

    def drop_client(self) -> int:
        """Вкладка закрылась. Больше нуля не бывает — иначе счётчик уедет
        после перезагрузки страницы, где уход и приход могут не совпасть."""
        with self.lock:
            self._clients = max(0, self._clients - 1)
            return self._clients

    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        """Попросить сервер завершиться.

        Гонять `httpd.shutdown()` из потока обработчика нельзя: он
        ждёт окончания цикла обслуживания, а цикл ждёт обработчика.
        Поэтому остановку всегда откладывают отдельным потоком.
        """
        if self.stop_server is None:
            raise RuntimeError("сервер не запущен")
        threading.Timer(0.4, self.stop_server).start()

    def switch_root(self, path: Path) -> dict:
        """Смена папки: другое дерево, другая база, чистый кэш превью."""
        resolved = path.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"нет такой папки: {resolved}")
        if too_broad(resolved):
            raise ValueError(
                f"{resolved} — слишком широкая папка, kurator обошёл бы её целиком. "
                "Укажите папку с фотографиями."
            )
        with self.lock:
            if self.job and self.job.get("status") == "running":
                raise ValueError("сейчас идёт индексация, дождись окончания")
            previous = self.store
            try:
                store = Store.open(resolved)
            except Exception:
                previous.close()
                raise
            self.root = resolved
            self.store = store
            self.thumbs.clear()
            self.job = None
        try:
            previous.close()
        except Exception:
            pass
        remember_root(resolved)
        return {"root": str(resolved), "files": self.store.count(),
                "embedded": self.store.embedded_count()}

    def close(self) -> None:
        self._stop.set()
        self.store.close()

    def get_encoder(self) -> SiglipVision:
        if self.encoder is None:
            self.encoder = SiglipVision(self.model_path)
        return self.encoder

    def embedder_available(self) -> bool:
        return model_is_present(self.model_path)

    def thumb(self, rel: str, width: int) -> bytes | None:
        key = (rel, width)
        with self.lock:
            hit = self.thumbs.get(key)
            if hit is not None:
                self.thumbs.move_to_end(key)
                return hit
        path = self.safe_path(rel)
        if path is None:
            return None
        try:
            from PIL import Image

            with Image.open(path) as img:
                img = img.convert("RGB")
                img.thumbnail((width, width), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=80)
        except Exception:
            return None
        data = buf.getvalue()
        with self.lock:
            self.thumbs[key] = data
            while len(self.thumbs) > THUMB_CACHE_LIMIT:
                self.thumbs.popitem(last=False)
        return data

    def safe_path(self, rel: str) -> Path | None:
        if not rel:
            return None
        candidate = (self.root / rel).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return None
        if candidate.suffix.lower() not in IMAGE_EXTENSIONS:
            return None
        if not candidate.is_file():
            return None
        return candidate


def _groups_for(state: State, phash: int, cosine: float):
    from ..cluster import cluster_paths
    from ..dedupe import analyze, group_signature

    photos = state.store.photos()
    index = {p.rel_path: p for p in photos}
    groups, _ = analyze(
        photos,
        state.store,
        phash_distance=phash,
        strong_cosine=cosine,
        include_burst=True,
    )
    for group in groups:
        group.detail["sig"] = group_signature(group.kind, group.members)

    _, paths, vectors = state.store.embeddings()
    clusters: list[list[str]] = []
    if len(paths) >= 3:
        clusters, _ = cluster_paths(paths, vectors, threshold=CLUSTER_COSINE, min_size=3)

    return photos, groups, clusters


def _group_payload(group) -> dict:
    return {
        "kind": group.kind,
        "size": group.size,
        "keeper": group.keeper,
        "members": group.members,
        "detail": {k: v for k, v in group.detail.items() if v is not None},
        "victims": group.victims(),
    }


def _start_index(state: State, rehash: bool) -> dict:
    with state.lock:
        if state.job and state.job.get("status") == "running":
            return state.job

    job = {
        "id": secrets.token_hex(8),
        "status": "running",
        "started_at": time.time(),
        "progress": "",
        "stats": None,
        "error": None,
    }
    state.job = job

    def work() -> None:
        from ..indexer import index_root

        try:
            encoder = state.get_encoder() if state.embedder_available() else None
            stats = index_root(
                state.root,
                state.store,
                encoder=encoder,
                rehash=rehash,
                progress=lambda message: job.update({"progress": message.strip()}),
            )
            job["stats"] = stats
            job["status"] = "done"
        except Exception as exc:
            job["error"] = f"{type(exc).__name__}: {exc}"
            job["status"] = "error"

    threading.Thread(target=work, daemon=True).start()
    return job


class Handler(BaseHTTPRequestHandler):
    server_version = f"kurator/{__version__}"
    state: State

    def log_message(self, fmt: str, *args) -> None:
        pass

    def _send(self, code: int, body: bytes, content_type: str, cache: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json")

    def _asset(self, name: str) -> None:
        path = (ASSETS / name).resolve()
        try:
            path.relative_to(ASSETS.resolve())
        except ValueError:
            self._send(403, b"forbidden", "text/plain")
            return
        if not path.is_file():
            self._send(404, b"not found", "text/plain")
            return
        body = path.read_bytes()
        if name == "index.html":
            body = body.replace(b"__KURATOR_TOKEN__", self.state.token.encode())
        suffix = path.suffix.lower()
        self._send(200, body, CONTENT_TYPES.get(suffix, "application/octet-stream"))

    def _authorized(self, query: dict) -> bool:
        token = self.headers.get("X-Kurator-Token") or (query.get("token") or [""])[0]
        return secrets.compare_digest(token or "", self.state.token)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            self._asset("index.html")
            return
        if route in ("/app.js", "/style.css"):
            self._asset(route.lstrip("/"))
            return
        if not self._authorized(query):
            self._json({"error": "нет токена"}, 403)
            return

        if route == "/api/state":
            store = self.state.store
            photos = store.photos()
            self._json(
                {
                    "root": str(self.state.root),
                    "version": __version__,
                    "files": len(photos),
                    "bytes": sum(p.size for p in photos),
                    "embedded": store.embedded_count(),
                    "model": {
                        "repo": MODEL_REPO,
                        "path": str(self.state.model_path),
                        "present": self.state.embedder_available(),
                    },
                    "defaults": {
                        "phash": float(store.get_meta("tuning.phash", NEAR_DUP_PHASH_DISTANCE)),
                        "cosine": float(store.get_meta("tuning.cosine", NEAR_DUP_COSINE_STRONG)),
                        "cluster": CLUSTER_COSINE,
                        "batch": BATCH_SIZE,
                    },
                    "factory": {
                        "phash": float(NEAR_DUP_PHASH_DISTANCE),
                        "cosine": float(NEAR_DUP_COSINE_STRONG),
                    },
                    "quarantine": QUARANTINE_DIRNAME,
                    "skipped": len(store.skipped()),
                    "skipped_groups": store.skipped_groups(),
                    "last_index_at": store.get_meta("last_index_at"),
                    "job": self.state.job,
                }
            )
            return

        if route == "/api/runs":
            from ..quarantine import list_runs

            self._json({"runs": list_runs(self.state.store)})
            return

        if route == "/api/trash":
            from ..quarantine import list_trash

            items = list_trash(self.state.store)
            self._json({
                "items": items,
                "count": len(items),
                "bytes": sum(i["size"] for i in items),
            })
            return

        if route == "/api/roots":
            self._json({"suggestions": candidate_roots(), "current": str(self.state.root)})
            return

        if route == "/api/job":
            job = self.state.job or {"status": "idle"}
            self._json(
                {
                    "status": job.get("status"),
                    "progress": job.get("progress", ""),
                    "stats": job.get("stats"),
                    "error": job.get("error"),
                }
            )
            return

        if route == "/api/findings":
            phash = int(query.get("phash", [NEAR_DUP_PHASH_DISTANCE])[0])
            cosine = float(query.get("cosine", [NEAR_DUP_COSINE_STRONG])[0])
            photos, groups, clusters = _groups_for(self.state, phash, cosine)
            index = {p.rel_path: p for p in photos}
            payload_groups = []
            for group in groups:
                item = _group_payload(group)
                item["files"] = [
                    {
                        "path": rel,
                        "size": index[rel].size,
                        "taken_at": index[rel].taken_at,
                        "width": index[rel].width,
                        "height": index[rel].height,
                        "role": "keeper" if rel == group.keeper else "victim",
                    }
                    for rel in group.members
                    if rel in index
                ]
                payload_groups.append(item)
            self._json(
                {
                    "groups": payload_groups,
                    "clusters": [{"size": len(c), "members": c} for c in clusters],
                }
            )
            return

        if route == "/api/thumb":
            rel = unquote((query.get("path") or [""])[0])
            width = int((query.get("w") or ["256"])[0])
            width = min(width, max(THUMB_WIDTHS))
            data = self.state.thumb(rel, width)
            if data is None:
                self._send(404, b"no thumb", "text/plain")
                return
            self._send(200, data, "image/jpeg", cache=True)
            return

        self._json({"error": "not found"}, 404)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorized(query):
            self._json({"error": "нет токена"}, 403)
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "плохой json"}, 400)
            return

        route = parsed.path

        if route == "/api/root":
            target = (body.get("path") or "").strip()
            if not target:
                self._json({"error": "не указан путь"}, 400)
                return
            try:
                info = self.state.switch_root(Path(target))
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
                return
            job = _start_index(self.state, rehash=bool(body.get("rehash")))
            self._json({**info, "job": job})
            return

        if route == "/api/index":
            job = _start_index(self.state, rehash=bool(body.get("rehash")))
            self._json(job)
            return

        if route == "/api/restore":
            from ..quarantine import undo_run

            run_id = body.get("run_id")
            if run_id is None:
                self._json({"error": "не указан перенос"}, 400)
                return
            result = undo_run(self.state.store, int(run_id))
            self._json(result)
            return

        if route == "/api/trash/delete":
            from ..quarantine import delete_trash

            keys = body.get("keys") or []
            if not isinstance(keys, list) or not keys:
                self._json({"error": "не выбрано ни одного файла"}, 400)
                return
            if not body.get("confirm"):
                self._json({"error": "нужно явное подтверждение"}, 400)
                return
            result = delete_trash(self.state.store, [str(k) for k in keys])
            self._json(result)
            return

        if route == "/api/trash/restore":
            # Возврат ничего не разрушает: файл просто едет обратно, и если
            # что-то пойдёт не так, он останется в корзине. Поэтому
            # подтверждения здесь, в отличие от удаления, не требуется.
            from ..quarantine import restore_paths

            keys = body.get("keys") or []
            if not isinstance(keys, list) or not keys:
                self._json({"error": "не выбрано ни одного файла"}, 400)
                return
            result = restore_paths(self.state.store, [str(k) for k in keys])
            self._json(result)
            return

        if route == "/api/hello":
            # Вкладка открылась. Без этого счётчика приложение не знает,
            # что им пользуются, и по простою выключится под открытой
            # страницей.
            self._json({"clients": self.state.add_client()})
            return

        if route == "/api/bye":
            # Вкладка закрылась. Ставится из браузера через sendBeacon
            # при уходе со страницы, поэтому ответ никому не нужен.
            self._json({"clients": self.state.drop_client()})
            return

        if route == "/api/quit":
            # Отвечаем сразу, а гасим сервер чуть позже: иначе ответ
            # не успеет уйти и браузер увидит оборванное соединение
            # вместо подтверждения.
            self._json({"quitting": True})
            try:
                self.state.request_stop()
            except RuntimeError:
                pass
            return

        if route == "/api/tuning":
            store = self.state.store
            if body.get("phash") is not None:
                store.set_meta("tuning.phash", str(float(body["phash"])))
            if body.get("cosine") is not None:
                store.set_meta("tuning.cosine", str(float(body["cosine"])))
            store.commit()
            self._json({"saved": True})
            return

        if route == "/api/report":
            from ..report import build_report

            out_dir = self.state.root / "out"
            photos, groups, _ = _groups_for(
                self.state,
                int(body.get("phash", NEAR_DUP_PHASH_DISTANCE)),
                float(body.get("cosine", NEAR_DUP_COSINE_STRONG)),
            )
            info = build_report(
                self.state.root,
                photos,
                groups,
                out_dir,
                sheets=bool(body.get("sheets", True)),
                max_groups=int(body.get("max_groups", 40)),
            )
            self._json(info)
            return

        if route == "/api/apply":
            from ..quarantine import apply_moves, plan_moves

            photos, groups, _ = _groups_for(
                self.state,
                int(body.get("phash", NEAR_DUP_PHASH_DISTANCE)),
                float(body.get("cosine", NEAR_DUP_COSINE_STRONG)),
            )
            kinds = set(body.get("kinds") or ["exact"])
            victims = sorted({m for g in groups if g.kind in kinds for m in g.victims()})
            moves = plan_moves(self.state.root, victims)
            result = apply_moves(self.state.store, moves, kind=",".join(sorted(kinds)), dry_run=True)
            self._json(
                {
                    "dry_run": True,
                    "count": len(moves),
                    "bytes": sum(p.size for p in photos if p.rel_path in {m.src.relative_to(self.state.root).as_posix() for m in moves}),
                    "sample": [
                        {"from": m.src.relative_to(self.state.root).as_posix(),
                         "to": m.dst.relative_to(self.state.root).as_posix()}
                        for m in moves[:200]
                    ],
                    "manifest": result["manifest"],
                }
            )
            return

        if route == "/api/apply/confirm":
            from ..quarantine import apply_moves, plan_moves

            photos, groups, _ = _groups_for(
                self.state,
                int(body.get("phash", NEAR_DUP_PHASH_DISTANCE)),
                float(body.get("cosine", NEAR_DUP_COSINE_STRONG)),
            )
            kinds = set(body.get("kinds") or ["exact"])
            victims = sorted({m for g in groups if g.kind in kinds for m in g.victims()})
            moves = plan_moves(self.state.root, victims)
            result = apply_moves(self.state.store, moves, kind=",".join(sorted(kinds)), dry_run=False)
            self._json(result)
            return

        if route == "/api/decide":
            from ..dedupe import burst_members
            from ..quarantine import apply_moves, plan_moves

            paths = [p for p in (body.get("paths") or []) if p]
            action = body.get("action")
            if action not in ("unskip", "clear") and not paths:
                self._json({"error": "нечего решать"}, 400)
                return
            root = self.state.root
            photos = self.state.store.photos()
            protected = burst_members(photos)

            if action == "remove":
                selected = [p for p in paths if p not in protected]
                blocked = [p for p in paths if p in protected]
                if not selected:
                    self._json(
                        {"error": "всё выбранное — burst-кадры, их не трогаем", "blocked": blocked},
                        400,
                    )
                    return
                moves = plan_moves(root, selected)
                result = apply_moves(self.state.store, moves, kind="by-hand", dry_run=False)
                if body.get("sig"):
                    self.state.store.drop_decisions([body["sig"]])
                self._json({**result, "blocked": blocked})
                return

            if action == "skip":
                signature = body.get("sig") or ""
                if not signature:
                    self._json({"error": "не указан отпечаток группы"}, 400)
                    return
                self.state.store.record_decision(signature, paths, "skip")
                self.state.store.commit()
                self._json({"skipped": len(paths), "sig": signature})
                return

            if action == "unskip":
                signature = body.get("sig") or ""
                if not signature:
                    self._json({"error": "не указан отпечаток группы"}, 400)
                    return
                removed = self.state.store.unskip(signature)
                self.state.store.commit()
                self._json({"cleared": removed})
                return

            if action == "clear":
                removed = self.state.store.clear_decisions(body.get("scope"))
                self.state.store.commit()
                self._json({"cleared": removed})
                return

            self._json({"error": f"неизвестное действие: {action}"}, 400)
            return

        if route == "/api/undo":
            from ..quarantine import undo_all_runs

            self._json(undo_all_runs(self.state.store))
            return

        self._json({"error": "not found"}, 404)


def last_root() -> Path | None:
    """Папка из прошлой сессии, если она ещё на месте."""
    for remembered in read_history():
        path = Path(remembered)
        if path.is_dir() and not too_broad(path):
            return path
    return None


def default_root() -> tuple[Path, str]:
    """Кем заменить папку, если её не сказали и прошлой сессии не было."""
    remembered = last_root()
    if remembered is not None:
        return remembered, "из прошлой сессии"
    pictures = Path.home() / "Pictures"
    if pictures.is_dir():
        return pictures, "домашняя папка с фотографиями"
    return Path.home(), "домашняя папка"


def _idle_watchdog(state: "State", httpd, timeout: float) -> None:
    """Выключить сервер, если вкладок не осталось.

    Приложение без открытой вкладки — это процесс, который держит модель
    и индекс в памяти, но ничего не делает. Пользователь закрыл вкладку и
    ушёл, а процесс остался висеть до следующего запуска, который к тому
    моменту уже не мог пройти.

    Проверка идёт каждые 5 секунд, само выключение — через `timeout`
    секунд после последнего ухода вкладки. Пауза нужна для перезагрузки
    страницы и для переключения между двумя вкладками: обе выглядят как
    «вкладок нет», но ни одну из них закрывать не хочется.
    """
    deadline = None
    while not state.stopping():
        if state.clients > 0:
            deadline = None
        elif deadline is None:
            deadline = time.monotonic() + timeout
        elif time.monotonic() >= deadline:
            print("\nвкладок не осталось — останавливаюсь")
            httpd.shutdown()
            return
        time.sleep(5)


def serve(
    root: Path | None,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    idle_timeout: float | None = None,
) -> None:
    """Поднять локальный интерфейс и держать его, пока он нужен.

    `idle_timeout` — через сколько секунд без вкладки выключаться. Для
    приложения это осмысленно: закрыл вкладку — значит закончил. Для
    консоли нет, там сервер запускают руками и им пользуются без вкладки,
    поэтому по умолчанию значение None.

    Повторный запуск приложения, когда экземпляр уже жив, не поднимает
    второй сервер, а открывает окно первого. Иначе вышло бы два процесса
    с двумя копиями модели в памяти — ровно то, на что жаловался
    пользователь, только сдвоенное.
    """
    trace(f"serve(): начало, корень задан: {root is not None}")
    if root is None:
        # Запуск без папки — это клик по иконке. Папку не передавали,
        # значит и второй экземпляр пользователю не нужен: он хотел
        # посмотреть тот, который уже открыт.
        running = live_instance()
        trace(f"живой экземпляр: {running}")
        if running is not None:
            print(f"kurator {__version__}")
            print("уже запущен — открываю его окно")
            if open_browser:
                webbrowser.open(running["url"])
            return
        root, source = default_root()
    else:
        source = "из командной строки"
    trace(f"корень выбран: {root} ({source})")
    model_path = default_model_path()
    if model_path.is_dir():
        model_path = model_path / MODEL_FILENAME
    trace(f"адрес модели: {model_path}")
    state = State(root, model_path)
    trace("состояние создано, база открыта")
    remember_root(state.root)
    trace("корень запомнен")

    handler = type("BoundHandler", (Handler,), {"state": state})
    httpd = _Server((host, port), handler)
    state.stop_server = httpd.shutdown
    trace(f"сокет привязан: {host}:{port}")

    url = f"http://{host}:{port}/?token={state.token}"
    write_instance({"url": url, "pid": os.getpid(), "port": port, "root": str(state.root)})
    trace("экземпляр записан")
    print(f"kurator {__version__}")
    print(f"корень : {state.root}  ({source})")
    print(f"модель : {model_path} {'(на месте)' if state.embedder_available() else '(нет: только хеши)'}")
    print(f"открываю {url}")
    print("Ctrl+C — остановить")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    watchdog = None
    if idle_timeout:
        watchdog = threading.Thread(
            target=_idle_watchdog,
            args=(state, httpd, float(idle_timeout)),
            daemon=True,
        )
        watchdog.start()

    try:
        trace("слушаю")
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nостанавливаюсь")
    finally:
        trace("останавливаюсь")
        clear_instance(os.getpid())
        state.close()
        httpd.server_close()
        print("остановлено")


def resolve_port(preferred: int) -> int:
    import socket

    for port in range(preferred, preferred + 20):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise SystemExit("не нашёл свободный порт")


mimetypes.add_type("text/javascript", ".js")