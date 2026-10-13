"""Кластеризация эмбеддингов по косинусному порогу (leader clustering).

Single-link по рёбрам склеивает всё в один ком, если A похож на B, а B на C.
Здесь снимок попадает в группу только если похож на её центроид, поэтому
цепочки не возникают: получаются компактные тематические группы.
"""

from __future__ import annotations

import numpy as np

from .config import CLUSTER_COSINE

DEFAULT_MAX_CENTROIDS = 6000
PROGRESS_EVERY = 2000


def _normalized(matrix: np.ndarray) -> np.ndarray:
    if matrix.size == 0:
        return matrix
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def _order_key(path: str) -> tuple:
    return (path.casefold(), path)


def leader_cluster(
    vectors: np.ndarray,
    threshold: float = CLUSTER_COSINE,
    max_centroids: int = DEFAULT_MAX_CENTROIDS,
    progress=None,
) -> np.ndarray:
    """Возвращает массив номеров кластеров длины n."""
    n = vectors.shape[0]
    if n == 0:
        return np.zeros(0, dtype=np.int64)

    unit = _normalized(np.asarray(vectors, dtype=np.float32))
    labels = np.full(n, -1, dtype=np.int64)

    centroid_sums: list[np.ndarray] = []
    sizes: list[int] = []

    for i in range(n):
        if not centroid_sums:
            labels[i] = 0
            centroid_sums.append(unit[i].copy())
            sizes.append(1)
            continue
        bank = np.vstack(centroid_sums)
        sims = bank @ unit[i]
        best = int(np.argmax(sims))
        if sims[best] >= threshold and len(centroid_sums) < max_centroids:
            labels[i] = best
            centroid_sums[best] = centroid_sums[best] + unit[i]
            size = sizes[best] + 1
            sizes[best] = size
            centroid_sums[best] /= np.linalg.norm(centroid_sums[best]) or 1.0
        else:
            labels[i] = len(centroid_sums)
            centroid_sums.append(unit[i].copy())
            sizes.append(1)
        if progress and (i + 1) % PROGRESS_EVERY == 0:
            progress(i + 1, n, len(centroid_sums))

    return labels


def cluster_paths(
    paths: list[str],
    vectors: np.ndarray,
    threshold: float = CLUSTER_COSINE,
    min_size: int = 3,
    max_centroids: int = DEFAULT_MAX_CENTROIDS,
    progress=None,
) -> tuple[list[list[str]], int]:
    if len(paths) < min_size:
        return [], 0
    order = sorted(range(len(paths)), key=lambda i: _order_key(paths[i]))
    sorted_paths = [paths[i] for i in order]
    sorted_vectors = np.asarray(vectors, dtype=np.float32)[order]

    labels = leader_cluster(
        sorted_vectors, threshold=threshold, max_centroids=max_centroids, progress=progress
    )
    total_centroids = int(labels.max()) + 1 if labels.size else 0

    buckets: dict[int, list[str]] = {}
    for path, label in zip(sorted_paths, labels.tolist()):
        buckets.setdefault(label, []).append(path)

    groups = [sorted(members) for members in buckets.values() if len(members) >= min_size]
    groups.sort(key=lambda members: (-len(members), members[0]))
    return groups, total_centroids


def coherence(vectors: np.ndarray, members: list[str], path_index: dict[str, int]) -> dict:
    """Насколько группа однородна: средний и минимальный косинус к центроиду."""
    idx = [path_index[m] for m in members if m in path_index]
    if len(idx) < 2:
        return {"mean": None, "min": None}
    unit = _normalized(np.asarray(vectors, dtype=np.float32)[idx])
    centroid = unit.mean(axis=0)
    norm = np.linalg.norm(centroid)
    if norm == 0:
        return {"mean": None, "min": None}
    centroid = centroid / norm
    sims = unit @ centroid
    return {"mean": float(sims.mean()), "min": float(sims.min())}