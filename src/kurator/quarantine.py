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
            count = len(payload.get("moves", []))
        except (OSError, json.JSONDecodeError):
            count = 0
        out.append({
            "run_id": row["id"],
            "kind": row["kind"],
            "count": count,
            "bytes": payload.get("bytes", 0),
            "started_at": row["started_at"],
            "paths": [m["from"] for m in payload.get("moves", [])] if count else [],
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