"""HTTP-сервер и API локального интерфейса kurator.

Только стандартная библиотека. Слушает исключительно 127.0.0.1 и требует
сессионный токен в заголовке, чтобы случайная страница в браузере не могла
обратиться к локальному серверу.
"""

from __future__ import annotations

import io
import json
import mimetypes
import secrets
import threading
import time
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
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


class State:
    def __init__(self, root: Path, model_path: Path) -> None:
        self.root = root.resolve()
        self.model_path = model_path
        self.store = Store.open(self.root)
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.encoder: SiglipVision | None = None
        self.job: dict | None = None
        self.thumbs: OrderedDict[tuple[str, int], bytes] = OrderedDict()
        self._stop = threading.Event()

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
    from ..dedupe import analyze

    photos = state.store.photos()
    index = {p.rel_path: p for p in photos}
    groups, _ = analyze(
        photos,
        state.store,
        phash_distance=phash,
        strong_cosine=cosine,
        include_burst=True,
    )

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
                        "phash": NEAR_DUP_PHASH_DISTANCE,
                        "cosine": NEAR_DUP_COSINE_STRONG,
                        "cluster": CLUSTER_COSINE,
                        "batch": BATCH_SIZE,
                    },
                    "quarantine": QUARANTINE_DIRNAME,
                    "job": self.state.job,
                }
            )
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

        if route == "/api/index":
            job = _start_index(self.state, rehash=bool(body.get("rehash")))
            self._json(job)
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

        if route == "/api/undo":
            from ..quarantine import undo_last_run

            self._json(undo_last_run(self.state.store))
            return

        self._json({"error": "not found"}, 404)


def serve(root: Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    model_path = default_model_path()
    if model_path.is_dir():
        model_path = model_path / MODEL_FILENAME
    state = State(root, model_path)

    handler = type("BoundHandler", (Handler,), {"state": state})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True

    url = f"http://{host}:{port}/?token={state.token}"
    print(f"kurator {__version__}")
    print(f"корень : {state.root}")
    print(f"модель : {model_path} {'(на месте)' if state.embedder_available() else '(нет: только хеши)'}")
    print(f"открываю {url}")
    print("Ctrl+C — остановить")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nостанавливаюсь")
    finally:
        state.close()
        httpd.server_close()


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