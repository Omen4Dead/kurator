"""Поиск дубликатов: точных, визуально похожих и burst-серий."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .config import (
    BURST_WINDOW_SECONDS,
    NEAR_DUP_COSINE_STRONG,
    NEAR_DUP_COSINE_WEAK,
    NEAR_DUP_PHASH_DISTANCE,
)
from .db import Photo, Store
from .hashing import hamming_many

SUFFIX_RE = re.compile(r"\((\d+)\)$")
PAIR_CHUNK = 2048

EXACT = "exact"
NEAR_STRONG = "near-strong"
NEAR_WEAK = "near-weak"
BURST = "burst"


@dataclass
class Group:
    kind: str
    members: list[str]
    keeper: str | None = None
    detail: dict = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def reclaimable(self) -> int:
        return max(0, self.size - 1)

    def victims(self) -> list[str]:
        if self.keeper is None or len(self.members) < 2:
            return []
        return [m for m in self.members if m != self.keeper]


def exact_groups(photos: list[Photo]) -> list[Group]:
    buckets: dict[str, list[str]] = {}
    for photo in photos:
        if photo.md5:
            buckets.setdefault(photo.md5, []).append(photo.rel_path)
    groups = []
    for digest, members in buckets.items():
        if len(members) < 2:
            continue
        group = Group(kind=EXACT, members=sorted(members))
        group.detail = {"md5": digest}
        groups.append(group)
    return groups


def _phash_array(photos: list[Photo]) -> np.ndarray:
    return np.array([photo.phash_int for photo in photos], dtype=np.uint64)


def near_groups(
    photos: list[Photo],
    distance: int = NEAR_DUP_PHASH_DISTANCE,
    store: Store | None = None,
    strong_cosine: float = NEAR_DUP_COSINE_STRONG,
    weak_cosine: float = NEAR_DUP_COSINE_WEAK,
) -> list[Group]:
    candidates = [p for p in photos if p.phash]
    if len(candidates) < 2:
        return []
    hashes = _phash_array(candidates)
    paths = [p.rel_path for p in candidates]
    index = {p.rel_path: p for p in candidates}

    pairs: list[tuple[int, int, int]] = []
    for start in range(0, len(candidates), PAIR_CHUNK):
        block = hashes[start : start + PAIR_CHUNK]
        dist = hamming_many(block[:, None], hashes[None, :])
        rows, cols = np.nonzero(dist <= distance)
        for r, c in zip(rows.tolist(), cols.tolist()):
            i = start + r
            j = c
            if i >= j:
                continue
            left, right = candidates[i], candidates[j]
            if left.md5 and left.md5 == right.md5:
                continue
            pairs.append((i, j, int(dist[r, c])))

    vectors = None
    positions: dict[str, int] = {}
    if store is not None:
        _, _, vectors = store.embeddings()
        if vectors.shape[0]:
            positions = {path: i for i, path in enumerate(store_paths(store))}
        else:
            vectors = None

    groups: list[Group] = []
    used: set[str] = set()
    for i, j, dist in sorted(pairs, key=lambda t: t[2]):
        a, b = paths[i], paths[j]
        if a in used and b in used:
            continue
        cosine = None
        if vectors is not None and a in positions and b in positions:
            cosine = float(np.dot(vectors[positions[a]], vectors[positions[b]]))
        if cosine is not None:
            kind = NEAR_STRONG if cosine >= strong_cosine else NEAR_WEAK
            if kind == NEAR_WEAK and cosine < weak_cosine:
                continue
        else:
            kind = NEAR_STRONG
        group = Group(
            kind=kind,
            members=sorted([a, b]),
            detail={"phash_distance": dist, "cosine": cosine},
        )
        groups.append(group)
        used.update([a, b])
    return groups


def store_paths(store: Store) -> list[str]:
    rows = store.query(
        "SELECT rel_path FROM photos WHERE embed IS NOT NULL "
        "ORDER BY COALESCE(taken_at, ''), rel_path"
    )
    return [r["rel_path"] for r in rows]


def _parse_stem(rel: str) -> tuple[str, int | None]:
    stem = Path(rel).stem
    match = SUFFIX_RE.search(stem)
    if match:
        return stem[: match.start()].strip(), int(match.group(1))
    return stem, None


def _drop_identical(members: list[str], by_path: dict[str, Photo]) -> list[str]:
    """Кадры с одинаковым md5 — это не burst, а побайтный дубль: убираем их."""
    counts: dict[str, int] = {}
    for rel in members:
        digest = by_path[rel].md5
        if digest:
            counts[digest] = counts.get(digest, 0) + 1
    top = max(counts.values(), default=0)
    if top < 2:
        return members
    primary = next(digest for digest, count in counts.items() if count == top)
    if counts[primary] == len(members):
        return []
    return [rel for rel in members if by_path[rel].md5 != primary]


def burst_groups(photos: list[Photo]) -> list[Group]:
    groups: list[Group] = []
    claimed: set[str] = set()
    by_path = {p.rel_path: p for p in photos}

    by_key: dict[tuple[str, str], list[Photo]] = {}
    for photo in photos:
        if photo.is_video:
            continue
        stem, index = _parse_stem(photo.rel_path)
        if index is None:
            continue
        key = (str(Path(photo.rel_path).parent), stem)
        by_key.setdefault(key, []).append(photo)

    for (folder, stem), items in sorted(by_key.items()):
        if len(items) < 2:
            continue
        members = _drop_identical(sorted(p.rel_path for p in items), by_path)
        if len(members) < 2:
            continue
        groups.append(
            Group(
                kind=BURST,
                members=members,
                detail={"reason": "серия с суффиксами (N)", "folder": folder, "stem": stem},
            )
        )
        claimed.update(members)

    by_folder: dict[str, list[Photo]] = {}
    for photo in photos:
        if photo.is_video or photo.rel_path in claimed:
            continue
        by_folder.setdefault(str(Path(photo.rel_path).parent), []).append(photo)

    for folder, items in sorted(by_folder.items()):
        stamped = []
        for photo in items:
            if not photo.taken_at:
                continue
            try:
                stamped.append((dt.datetime.fromisoformat(photo.taken_at), photo))
            except ValueError:
                continue
        stamped.sort(key=lambda pair: pair[0])

        used: set[str] = set()
        for i, (moment, photo) in enumerate(stamped):
            if photo.rel_path in used:
                continue
            window = []
            for other_moment, other in stamped[i + 1 :]:
                delta = (other_moment - moment).total_seconds()
                if delta > BURST_WINDOW_SECONDS:
                    break
                window.append(other.rel_path)
            if not window:
                continue
            members = _drop_identical(sorted([photo.rel_path, *window]), by_path)
            if len(members) < 2:
                continue
            groups.append(
                Group(
                    kind=BURST,
                    members=members,
                    detail={
                        "reason": f"EXIF в пределах {BURST_WINDOW_SECONDS:g} с",
                        "folder": folder,
                        "anchor": photo.rel_path,
                    },
                )
            )
            used.update(members)

    return groups


def burst_members(photos: list[Photo]) -> set[str]:
    found: set[str] = set()
    for group in burst_groups(photos):
        found.update(group.members)
    return found


def choose_keepers(groups: list[Group], photos: list[Photo]) -> None:
    index = {p.rel_path: p for p in photos}

    def rank(rel: str) -> tuple:
        photo = index[rel]
        pixels = (photo.width or 0) * (photo.height or 0)
        suffixed = 1 if SUFFIX_RE.search(Path(rel).stem) else 0
        return (suffixed, -pixels, -photo.size, rel)

    for group in groups:
        if group.kind == BURST:
            group.keeper = None
            continue
        ranked = sorted(group.members, key=rank)
        group.keeper = ranked[0] if ranked else None


def calibration(
    store: Store,
    photos: list[Photo],
    cosine_thresholds: tuple[float, ...] = (0.80, 0.85, 0.90, 0.95, 0.98, 0.99),
    phash_thresholds: tuple[int, ...] = (0, 2, 4, 6, 8, 10, 14),
) -> dict:
    hash_rows = [p for p in photos if p.phash]
    phash_counts: dict[int, int] = {}
    if len(hash_rows) > 1:
        hashes = _phash_array(hash_rows)
        counts: dict[int, int] = {}
        for start in range(0, len(hashes), PAIR_CHUNK):
            dist = hamming_many(hashes[start : start + PAIR_CHUNK, None], hashes[None, :])
            for t in phash_thresholds:
                counts[t] = counts.get(t, 0) + int((dist <= t).sum())
        phash_counts = {t: counts.get(t, 0) // 2 for t in phash_thresholds}

    _, paths, vectors = store.embeddings()
    cosine_counts: dict[float, int] = {}
    if len(paths) > 1 and vectors.shape[0] > 1:
        unit = vectors / np.clip(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-9, None)
        counts = {}
        for start in range(0, unit.shape[0], PAIR_CHUNK):
            sim = unit[start : start + PAIR_CHUNK] @ unit.T
            for t in cosine_thresholds:
                counts[t] = counts.get(t, 0) + int((sim >= t).sum())
        cosine_counts = {t: counts.get(t, 0) // 2 for t in cosine_thresholds}

    return {
        "photos": len(photos),
        "embedded": len(paths),
        "phash_pairs": phash_counts,
        "cosine_pairs": cosine_counts,
    }


def apply_burst_protection(
    groups: list[Group],
    photos: list[Photo],
    include_burst: bool = True,
) -> tuple[list[Group], set[str]]:
    """Убирает из групп-дублей всё, что относится к burst-сериям.

    Единая точка правды: и dups, и report, и apply, и веб-интерфейс
    обязаны считать кандидатов именно так, иначе они разойдутся.
    """
    protected = burst_members(photos)
    kept: list[Group] = []
    for group in groups:
        members = [rel for rel in group.members if rel not in protected]
        if len(members) < 2:
            continue
        kept.append(
            Group(kind=group.kind, members=sorted(members), keeper=group.keeper, detail=group.detail)
        )
    if include_burst:
        kept += burst_groups(photos)
    return kept, protected


def analyze(
    photos: list[Photo],
    store: Store | None = None,
    phash_distance: int = NEAR_DUP_PHASH_DISTANCE,
    strong_cosine: float = NEAR_DUP_COSINE_STRONG,
    include_burst: bool = True,
) -> tuple[list[Group], set[str]]:
    """Полный разбор фотопарки. Единая точка входа для всех команд."""
    groups = exact_groups(photos) + near_groups(
        photos,
        phash_distance,
        store,
        strong_cosine=strong_cosine,
        weak_cosine=max(0.0, strong_cosine - 0.15),
    )
    protected = burst_members(photos)
    kept: list[Group] = []
    for group in groups:
        members = [rel for rel in group.members if rel not in protected]
        if len(members) < 2:
            continue
        kept.append(
            Group(kind=group.kind, members=sorted(members), keeper=None, detail=group.detail)
        )
    if include_burst:
        kept += burst_groups(photos)
    choose_keepers(kept, photos)
    return kept, protected


def summarize(groups: list[Group]) -> dict[str, int]:
    out: dict[str, int] = {}
    for group in groups:
        out[group.kind] = out.get(group.kind, 0) + 1
    return out


def reclaimable_bytes(groups: list[Group], photos: list[Photo], kinds: set[str]) -> int:
    index = {p.rel_path: p for p in photos}
    total = 0
    for group in groups:
        if group.kind not in kinds:
            continue
        for rel in group.victims():
            total += index[rel].size
    return total