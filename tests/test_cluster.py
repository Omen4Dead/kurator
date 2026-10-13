import numpy as np

from kurator.cluster import cluster_paths, coherence, leader_cluster


def vec(*coords) -> np.ndarray:
    return np.array([coords], dtype=np.float32)


def unit(*coords) -> np.ndarray:
    v = vec(*coords)
    return v / np.linalg.norm(v)


def test_leader_cluster_keeps_apart_chained_points():
    a = unit(1, 0, 0)
    b = unit(0.97, 0.24, 0)
    c = unit(0, 1, 0)
    vectors = np.vstack([a, b, c])
    labels = leader_cluster(vectors, threshold=0.95)
    assert labels[0] == labels[1], "похожие должны попасть вместе"
    assert labels[2] != labels[0], "далёкий не должен сливаться по цепочке"


def test_cluster_paths_respects_min_size():
    paths = ["a", "b", "c"]
    vectors = np.vstack([unit(1, 0), unit(1, 0), unit(0, 1)])
    groups, _ = cluster_paths(paths, vectors, threshold=0.95, min_size=2)
    assert groups == [["a", "b"]]

    groups, _ = cluster_paths(paths, vectors, threshold=0.95, min_size=3)
    assert groups == []


def test_cluster_paths_empty():
    groups, centroids = cluster_paths([], np.zeros((0, 3), dtype=np.float32))
    assert groups == []
    assert centroids == 0


def test_cluster_paths_is_deterministic_regardless_of_input_order():
    vectors = np.vstack([unit(1, 0), unit(0.99, 0.14), unit(0, 1), unit(0, 0.99)])
    forward, _ = cluster_paths(["a", "b", "c", "d"], vectors, threshold=0.98, min_size=2)
    backward, _ = cluster_paths(["d", "c", "b", "a"], vectors[::-1], threshold=0.98, min_size=2)
    assert sorted(forward) == sorted(backward)


def test_coherence_reports_spread():
    paths = ["a", "b", "c"]
    vectors = np.vstack([unit(1, 0), unit(1, 0), unit(0.99, 0.14)])
    index = {p: i for i, p in enumerate(paths)}
    stats = coherence(vectors, ["a", "b", "c"], index)
    assert stats["mean"] > stats["min"]


def test_coherence_single_member():
    stats = coherence(np.vstack([unit(1, 0)]), ["a"], {"a": 0})
    assert stats["mean"] is None