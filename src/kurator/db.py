"""Хранилище индекса: один SQLite-файл рядом с фотками."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .config import db_path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=15000;

CREATE TABLE IF NOT EXISTS photos (
    id           INTEGER PRIMARY KEY,
    rel_path     TEXT    NOT NULL UNIQUE,
    size         INTEGER NOT NULL,
    mtime        REAL    NOT NULL,
    md5          TEXT,
    phash        TEXT,
    dhash        TEXT,
    taken_at     TEXT,
    width        INTEGER,
    height       INTEGER,
    embed        BLOB,
    embed_model  TEXT,
    embed_dim    INTEGER,
    is_video     INTEGER NOT NULL DEFAULT 0,
    indexed_at   REAL
);

CREATE INDEX IF NOT EXISTS idx_photos_md5   ON photos(md5);
CREATE INDEX IF NOT EXISTS idx_photos_phash ON photos(phash);
CREATE INDEX IF NOT EXISTS idx_photos_embed ON photos(embed_model);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id       INTEGER PRIMARY KEY,
    started_at REAL NOT NULL,
    root     TEXT NOT NULL,
    kind     TEXT NOT NULL,
    manifest TEXT NOT NULL
);
"""

PHOTO_FIELDS = (
    "id",
    "rel_path",
    "size",
    "mtime",
    "md5",
    "phash",
    "dhash",
    "taken_at",
    "width",
    "height",
    "is_video",
    "indexed_at",
)


@dataclass
class Photo:
    rel_path: str
    size: int
    mtime: float
    md5: str | None = None
    phash: str | None = None
    dhash: str | None = None
    taken_at: str | None = None
    width: int | None = None
    height: int | None = None
    embed: Any = None
    embed_model: str | None = None
    is_video: bool = False

    @property
    def phash_int(self) -> int:
        return int(self.phash, 16) if self.phash else 0

    @property
    def dhash_int(self) -> int:
        return int(self.dhash, 16) if self.dhash else 0

    @property
    def sort_key(self) -> tuple:
        return (self.taken_at or "", self.rel_path)


@dataclass
class Store:
    conn: sqlite3.Connection
    root: Path
    path: Path
    lock: threading.RLock

    @classmethod
    def open(cls, root: Path) -> "Store":
        path = db_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        conn.commit()
        return cls(conn=conn, root=root, path=path, lock=threading.RLock())

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def set_meta(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, value),
            )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.lock:
            row = self.conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
            return row["v"] if row else default

    def known_state(self) -> dict[str, tuple[int, float]]:
        with self.lock:
            rows = self.conn.execute("SELECT rel_path, size, mtime FROM photos").fetchall()
        return {r["rel_path"]: (r["size"], r["mtime"]) for r in rows}

    def forget(self, rel_paths: list[str]) -> None:
        if not rel_paths:
            return
        with self.lock:
            self.conn.executemany(
                "DELETE FROM photos WHERE rel_path=?", ((p,) for p in rel_paths)
            )

    def upsert(self, photo: Photo) -> None:
        embed = photo.embed
        if embed is None:
            blob, dim = None, None
        else:
            blob = np.asarray(embed, dtype=np.float32).tobytes()
            dim = int(np.asarray(embed).shape[0])
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO photos
                    (rel_path, size, mtime, md5, phash, dhash, taken_at, width, height,
                     embed, embed_model, embed_dim, is_video, indexed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(rel_path) DO UPDATE SET
                    size=excluded.size, mtime=excluded.mtime, md5=excluded.md5,
                    phash=excluded.phash, dhash=excluded.dhash, taken_at=excluded.taken_at,
                    width=excluded.width, height=excluded.height,
                    embed=COALESCE(excluded.embed, photos.embed),
                    embed_model=COALESCE(excluded.embed_model, photos.embed_model),
                    embed_dim=COALESCE(excluded.embed_dim, photos.embed_dim),
                    is_video=excluded.is_video, indexed_at=excluded.indexed_at
                """,
                (
                    photo.rel_path,
                    photo.size,
                    photo.mtime,
                    photo.md5,
                    photo.phash,
                    photo.dhash,
                    photo.taken_at,
                    photo.width,
                    photo.height,
                    blob,
                    photo.embed_model,
                    dim,
                    int(photo.is_video),
                    time.time(),
                ),
            )

    def commit(self) -> None:
        with self.lock:
            self.conn.commit()

    def count(self) -> int:
        with self.lock:
            row = self.conn.execute("SELECT COUNT(*) AS c FROM photos").fetchone()
        return int(row["c"])

    def embedded_count(self) -> int:
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS c FROM photos WHERE embed IS NOT NULL"
            ).fetchone()
        return int(row["c"])

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def iter_rows(self, only_embedded: bool = False):
        sql = "SELECT * FROM photos"
        if only_embedded:
            sql += " WHERE embed IS NOT NULL"
        sql += " ORDER BY COALESCE(taken_at, ''), rel_path"
        with self.lock:
            rows = self.conn.execute(sql).fetchall()
        for row in rows:
            yield row

    def photos(self, only_embedded: bool = False) -> list[Photo]:
        return [
            Photo(
                rel_path=row["rel_path"],
                size=row["size"],
                mtime=row["mtime"],
                md5=row["md5"],
                phash=row["phash"],
                dhash=row["dhash"],
                taken_at=row["taken_at"],
                width=row["width"],
                height=row["height"],
                embed_model=row["embed_model"],
                is_video=bool(row["is_video"]),
            )
            for row in self.iter_rows(only_embedded=only_embedded)
        ]

    def embeddings(self) -> tuple[list[int], list[str], Any]:
        rows = self.query(
            "SELECT id, rel_path, embed, embed_dim FROM photos "
            "WHERE embed IS NOT NULL ORDER BY COALESCE(taken_at, ''), rel_path"
        )
        ids: list[int] = []
        paths: list[str] = []
        vectors: list[np.ndarray] = []
        for row in rows:
            dim = int(row["embed_dim"] or 0)
            if dim == 0:
                continue
            ids.append(int(row["id"]))
            paths.append(row["rel_path"])
            vectors.append(np.frombuffer(row["embed"], dtype=np.float32, count=dim))
        if not vectors:
            return ids, paths, np.zeros((0, 1), dtype=np.float32)
        return ids, paths, np.vstack(vectors)

    def start_run(self, kind: str, manifest: Path) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO runs(started_at, root, kind, manifest) VALUES(?,?,?,?)",
                (time.time(), str(self.root), kind, str(manifest)),
            )
            self.conn.commit()
        return int(cur.lastrowid)

    def last_run(self) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    def delete_run(self, run_id: int) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
            self.conn.commit()

    def write_json(self, rel: str, payload: Any) -> Path:
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return target
