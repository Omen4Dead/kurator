"""Перемещение в карантин. Удаления нет — только перенос с манифестом."""

from __future__ import annotations

import datetime as dt
import json
import secrets
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .config import QUARANTINE_DIRNAME
from .db import Store


@dataclass
class Move:
    src: Path
    dst: Path

    def as_dict(self, root: Path) -> dict:
        return {
            "from": str(self.src.relative_to(root)),
            "to": str(self.dst.relative_to(root)),
        }


def quarantine_root(root: Path) -> Path:
    return root / QUARANTINE_DIRNAME


def plan_moves(root: Path, rel_paths: list[str]) -> list[Move]:
    base = quarantine_root(root)
    moves: list[Move] = []
    seen: set[str] = set()
    for rel in rel_paths:
        src = root / rel
        dst = base / rel
        if not src.is_file():
            continue
        if src in seen:
            continue
        seen.add(src)
        if dst.exists():
            continue
        moves.append(Move(src=src, dst=dst))
    return moves


def apply_moves(store: Store, moves: list[Move], kind: str, dry_run: bool) -> dict:
    root = store.root
    planned_bytes = sum(move.src.stat().st_size for move in moves if move.src.exists())

    if dry_run:
        return {
            "dry_run": True,
            "count": len(moves),
            "bytes": planned_bytes,
            "manifest": None,
            "run_id": None,
            "paths": [str(m.src.relative_to(root)) for m in moves],
            "sample": [
                {
                    "from": str(m.src.relative_to(root)),
                    "to": str(m.dst.relative_to(root)),
                }
                for m in moves
            ],
        }

    payload = {
        "kind": kind,
        "root": str(root),
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "count": len(moves),
        "bytes": planned_bytes,
        "moves": [m.as_dict(root) for m in moves],
    }
    unique = f"{int(time.time() * 1000)}_{secrets.token_hex(3)}"
    manifest = store.write_json(
        f"{QUARANTINE_DIRNAME}/manifest_{kind}_{unique}.json", payload
    )

    done: list[dict] = []
    failed: list[dict] = []
    for move in moves:
        move.dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(move.src), str(move.dst))
            done.append(move.as_dict(root))
        except OSError as exc:
            failed.append({**move.as_dict(root), "error": str(exc)})

    store.forget([m["from"] for m in done])
    run_id = store.start_run(kind, manifest)
    store.commit()
    return {"dry_run": False, "moved": len(done), "failed": failed,
            "manifest": str(manifest), "run_id": run_id, "bytes": payload["bytes"],
            "paths": [m["from"] for m in done]}


def _prune_empty_dirs(base: Path) -> None:
    """Убрать опустевшие каталоги карантина.

    Удалять файлы здесь нельзя: в карантине могут лежать снимки других
    переносов, и откат одной группы не имеет права их трогать. Раньше
    именно так и вышло — откат одной группы сносил все остальные.
    """
    if not base.exists():
        return
    for directory in sorted((p for p in base.rglob("*") if p.is_dir()),
                            key=lambda p: len(p.parts), reverse=True):
        try:
            next(directory.iterdir())
        except StopIteration:
            try:
                directory.rmdir()
            except OSError:
                pass
        except OSError:
            pass
    try:
        base.rmdir()
    except OSError:
        pass


def undo_run(store: Store, run_id: int) -> dict:
    row = store.query("SELECT * FROM runs WHERE id=?", (run_id,))
    if not row:
        return {"moved": 0, "message": "такой перенос не найден"}
    return _restore(store, row[0])


def list_runs(store: Store, limit: int = 20) -> list[dict]:
    out: list[dict] = []
    for row in store.query("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)):
        manifest = Path(row["manifest"])
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        root = Path(payload.get("root", ""))
        moves = payload.get("moves", [])
        alive = [m for m in moves if (root / m.get("to", "")).is_file()]
        if not alive:
            continue
        out.append({
            "run_id": row["id"],
            "kind": row["kind"],
            "count": len(alive),
            "bytes": payload.get("bytes", 0),
            "started_at": row["started_at"],
            "paths": [m["from"] for m in alive],
            "lost": len(moves) - len(alive),
        })
    return out


def undo_last_run(store: Store) -> dict:
    row = store.last_run()
    if row is None:
        return {"moved": 0, "message": "нечего откатывать — карантин пуст"}
    return _restore(store, row)


def _restore(store: Store, row: sqlite3.Row) -> dict:
    manifest = Path(row["manifest"])
    if not manifest.is_file():
        return {"moved": 0, "message": f"манифест потерян: {manifest}"}
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    root = store.root
    restored: list[str] = []
    missing: list[str] = []
    for entry in payload.get("moves", []):
        src = root / entry["to"]
        dst = root / entry["from"]
        if not src.is_file():
            missing.append(entry["to"])
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            missing.append(entry["to"])
            continue
        shutil.move(str(src), str(dst))
        restored.append(entry["from"])

    base = quarantine_root(root)
    try:
        manifest.unlink()
    except OSError:
        pass
    _prune_empty_dirs(base)
    store.delete_run(int(row["id"]))
    if restored:
        store.backfill_paths(root, restored)
    return {"moved": len(restored), "missing": missing}

def list_trash(store: Store) -> list[dict]:
    """Что сейчас лежит в карантине.

    Источник правды — манифесты: в них записано, откуда пришёл файл.
    Вдобавок подбираются файлы, о которых манифест потерялся: их всё
    равно видно в папке карантина, молча пропускать их нельзя.
    """
    root = store.root
    base = quarantine_root(root)
    items: list[dict] = []
    claimed: set[str] = set()

    rows = store.query("SELECT id, manifest FROM runs ORDER BY id DESC")
    for row in rows:
        manifest = Path(row["manifest"])
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for entry in payload.get("moves", []):
            rel_to = entry.get("to", "")
            path = root / rel_to
            claimed.add(rel_to)
            if not path.is_file():
                continue
            info = path.stat()
            items.append({
                "key": rel_to,
                "from_path": entry.get("from"),
                "to_path": rel_to,
                "size": info.st_size,
                "created_at": payload.get("created_at", ""),
                "run_id": int(row["id"]),
                "orphan": False,
            })

    if base.is_dir():
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix == ".json":
                continue
            rel_to = path.relative_to(root).as_posix()
            if rel_to in claimed:
                continue
            info = path.stat()
            items.append({
                "key": rel_to,
                "from_path": None,
                "to_path": rel_to,
                "size": info.st_size,
                "created_at": dt.datetime.fromtimestamp(info.st_mtime).isoformat(timespec="seconds"),
                "run_id": None,
                "orphan": True,
            })

    items.sort(key=lambda i: (i["created_at"], i["to_path"]), reverse=True)
    return items


def delete_trash(store: Store, keys: list[str]) -> dict:
    """Удалить из карантина навсегда.

    Возврат здесь один и необратимый, поэтому проверки жёсткие: ключ
    обязан лежать внутри папки карантина и совпадать с тем, что
    реально лежит на диске. Манифест, из которого вынули все
    переносы, удаляется вместе с записью в базе — иначе в истории
    останется перенос, откатывать который уже нечего.
    """
    root = store.root
    base = quarantine_root(root)
    wanted = {k for k in keys if isinstance(k, str) and k}
    deleted: list[str] = []
    failed: list[dict] = []
    freed = 0

    for key in sorted(wanted):
        path = root / key
        try:
            resolved = path.resolve()
        except OSError:
            failed.append({"key": key, "error": "путь не читается"})
            continue
        if base.resolve() not in resolved.parents:
            failed.append({"key": key, "error": "это не карантин, отказ"})
            continue
        if not path.is_file():
            failed.append({"key": key, "error": "файла уже нет"})
            continue
        try:
            freed += path.stat().st_size
            path.unlink()
        except OSError as exc:
            failed.append({"key": key, "error": str(exc)})
            continue
        deleted.append(key)

    gone = set(deleted)
    for row in store.query("SELECT id, manifest FROM runs"):
        manifest = Path(row["manifest"])
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        moves = payload.get("moves", [])
        left = [m for m in moves if m.get("to") not in gone]
        if len(left) == len(moves):
            continue
        if not left:
            try:
                manifest.unlink()
            except OSError:
                pass
            store.delete_run(int(row["id"]))
            continue
        payload["moves"] = left
        payload["count"] = len(left)
        payload["bytes"] = sum(
            (root / m["from"]).stat().st_size
            for m in left
            if (root / m["from"]).is_file()
        )
        try:
            manifest.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    _prune_empty_dirs(base)
    store.commit()
    return {"deleted": len(deleted), "bytes": freed, "failed": failed}
