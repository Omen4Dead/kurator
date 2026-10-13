"""Обход папки и построение индекса."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Iterator

from .config import (
    BATCH_SIZE,
    IMAGE_EXTENSIONS,
    SKIP_DIRNAMES,
    VIDEO_EXTENSIONS,
)
from .db import Photo, Store
from .encoder import SiglipVision, model_is_present
from .hashing import md5_file, to_hex
from .imaging import BadImage, hash_views, open_rgb, read_exif


def walk_media(root: Path) -> Iterator[tuple[str, Path]]:
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRNAMES and not d.startswith("."))
        here = Path(dirpath)
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            path = here / name
            suffix = path.suffix.lower()
            if suffix not in IMAGE_EXTENSIONS and suffix not in VIDEO_EXTENSIONS:
                continue
            yield str(path.relative_to(root)), path


def index_root(
    root: Path,
    store: Store,
    encoder: SiglipVision | None = None,
    rehash: bool = False,
    progress: Callable[[str], None] | None = None,
    known_override: dict | None = None,
    only_paths: set[str] | None = None,
) -> dict[str, int]:
    root = root.resolve()
    stats = {"seen": 0, "indexed": 0, "skipped": 0, "embedded": 0, "unreadable": 0, "removed": 0}
    known = {} if rehash else store.known_state()
    if known_override:
        known = known_override

    present: set[str] = set()
    pending: list[tuple[str, Path]] = []

    for rel, path in walk_media(root):
        if only_paths is not None and rel not in only_paths:
            continue
        stats["seen"] += 1
        present.add(rel)
        try:
            info = path.stat()
        except OSError:
            continue
        previous = known.get(rel)
        if previous and previous[0] == info.st_size and previous[1] == info.st_mtime:
            stats["skipped"] += 1
            continue
        pending.append((rel, path))

    if only_paths is None:
        gone = [rel for rel in known if rel not in present]
        store.forget(gone)
        stats["removed"] = len(gone)

    def report(message: str) -> None:
        if progress:
            progress(message)

    for offset in range(0, len(pending), BATCH_SIZE):
        chunk = pending[offset : offset + BATCH_SIZE]
        metas: list[Photo] = []
        for rel, path in chunk:
            try:
                info = path.stat()
            except OSError:
                continue
            photo = Photo(
                rel_path=rel,
                size=info.st_size,
                mtime=info.st_mtime,
                is_video=path.suffix.lower() in VIDEO_EXTENSIONS,
            )
            photo.md5 = md5_file(path)
            photo.taken_at = read_exif(path).get("taken_at")
            if not photo.is_video:
                try:
                    with open_rgb(path) as img:
                        photo.width, photo.height = img.size
                        photo.phash, photo.dhash = (to_hex(v) for v in hash_views(img))
                except BadImage:
                    stats["unreadable"] += 1
            metas.append(photo)

        if encoder is not None:
            embeddable = [p for p in metas if not p.is_video]
            if embeddable:
                used, vectors = encoder.encode_from_paths([root / p.rel_path for p in embeddable])
                lookup = {p.rel_path: p for p in embeddable}
                for path, vector in zip(used, vectors):
                    photo = lookup[str(path.relative_to(root))]
                    photo.embed = vector
                    photo.embed_model = encoder.output_name

        for photo in metas:
            store.upsert(photo)
            stats["indexed"] += 1
            if photo.embed is not None:
                stats["embedded"] += 1
        store.commit()
        report(
            f"  {stats['indexed'] + stats['skipped']}/{stats['seen']} "
            f"(новых {stats['indexed']}, пропущено {stats['skipped']})"
        )

    store.set_meta("root", str(root))
    store.set_meta("last_index_at", str(int(time.time())))
    if encoder is not None:
        store.set_meta("encoder", encoder.output_name)
    store.commit()
    return stats


def backfill_embeddings(
    root: Path,
    store: Store,
    encoder: SiglipVision,
    progress: Callable[[str], None] | None = None,
) -> int:
    root = root.resolve()
    rows = [r for r in store.iter_rows() if r["embed"] is None and not r["is_video"]]
    if not rows:
        return 0
    paths = [root / r["rel_path"] for r in rows]
    done = 0
    for start in range(0, len(rows), BATCH_SIZE):
        batch_rows = rows[start : start + BATCH_SIZE]
        batch_paths = paths[start : start + BATCH_SIZE]
        used, vectors = encoder.encode_from_paths(batch_paths)
        lookup = {str(p.relative_to(root)): r for p, r in zip(batch_paths, batch_rows)}
        for path, vector in zip(used, vectors):
            row = lookup[str(path)]
            photo = Photo(
                rel_path=row["rel_path"],
                size=row["size"],
                mtime=row["mtime"],
                embed=vector,
                embed_model=encoder.output_name,
            )
            store.upsert(photo)
        store.commit()
        done += len(batch_rows)
        if progress:
            progress(f"  эмбеддинги {done}/{len(rows)}")
    return done


def index_paths(root: Path, store: Store, rel_paths: list[str]) -> int:
    """Доиндексировать конкретные файлы: хеши, EXIF и эмбеддинги."""
    root = root.resolve()
    store.forget(rel_paths)
    known = store.known_state()
    present = {rel: root / rel for rel in rel_paths if (root / rel).is_file()}
    stats = index_root(
        root,
        store,
        encoder=None,
        rehash=False,
        known_override={rel: (known[rel][0], known[rel][1]) for rel in present if rel in known},
        only_paths=set(present),
    )
    return stats["indexed"]


def encoder_or_none(model_path: Path, allow_download: bool) -> SiglipVision | None:
    if not model_is_present(model_path):
        if not allow_download:
            return None
        print(f"весов нет, качаю {model_path}…")
        from .encoder import download_model

        download_model(model_path)
    return SiglipVision(model_path)