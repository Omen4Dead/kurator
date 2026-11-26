"""Разведка для 1.13: распределение pHash-расстояния и косинуса по типам пар.

Считает по уже проиндексированному набору:
  - ожидаемые near-пары (ground truth), разбитые по типу деформации;
  - пары ловушек (должны остаться ненайденными).

Ничего не меняет в индексе, только печатает таблицу.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/kurator-test")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kurator.db import Store  # noqa: E402
from kurator.dedupe import store_paths  # noqa: E402
from kurator.hashing import hamming  # noqa: E402


def main() -> int:
    truth = json.loads((ROOT / "ground_truth.json").read_text(encoding="utf-8"))
    by_name = {entry["rel"]: entry for entry in truth.values()}

    store = Store.open(ROOT)
    photos = {p.rel_path: p for p in store.photos()}
    _, _, vectors = store.embeddings()
    positions = {path: i for i, path in enumerate(store_paths(store))}

    def stats(pairs: list[tuple[str, str]], label: str) -> None:
        rows = []
        for a, b in pairs:
            pa, pb = photos.get(a), photos.get(b)
            if not pa or not pb or not pa.phash or not pb.phash:
                continue
            dist = hamming(pa.phash_int, pb.phash_int)
            cos = None
            if vectors.shape[0] and a in positions and b in positions:
                cos = float(np.dot(vectors[positions[a]], vectors[positions[b]]))
            rows.append((dist, cos))
        if not rows:
            print(f"{label:<28} пусто")
            return
        dists = sorted(r[0] for r in rows)
        coss = sorted(r[1] for r in rows if r[1] is not None)
        cos_line = (
            f"cos: min {coss[0]:.3f} p25 {coss[len(coss)//4]:.3f} "
            f"med {coss[len(coss)//2]:.3f} p75 {coss[3*len(coss)//4]:.3f}"
            if coss else "cos: нет векторов"
        )
        print(f"{label:<28} n={len(rows):>3}  phash: min {dists[0]:>2} "
              f"med {dists[len(dists)//2]:>2} max {dists[-1]:>2}   {cos_line}")

    near_pairs: dict[str, list[tuple[str, str]]] = {}
    trap_pairs: list[tuple[str, str]] = []
    rel_by_name = {Path(rel).name: rel for rel in by_name}
    for rel, e in by_name.items():
        origin = rel_by_name.get(e.get("derived_from") or "")
        if not origin:
            continue
        pair = tuple(sorted((rel, origin)))
        if e["kind"] == "near-dup":
            key = e.get("deform") or "resize/recompress"
            near_pairs.setdefault(key, []).append(pair)
        elif e["kind"] == "lookalike":
            trap_pairs.append(pair)

    print(f"набор: {ROOT}\n")
    for key in sorted(near_pairs):
        stats(near_pairs[key], f"near: {key}")
    stats(trap_pairs, "ловушки (не дубли!)")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
