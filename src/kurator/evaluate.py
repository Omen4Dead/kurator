"""Оценка качества дедупликации против эталонной разметки.

Эталон пишет scripts/make_testset.py в ground_truth.json. Здесь группы,
которые нашёл kurator, сравниваются с тем, что должно было найтись.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import NEAR_DUP_COSINE_STRONG, NEAR_DUP_PHASH_DISTANCE
from .db import Store
from .dedupe import EXACT as KIND_EXACT, Group, analyze, burst_members

ORIGINAL = "original"
EXACT_DUP = "exact-dup"
NEAR_DUP = "near-dup"
LOOKALIKE = "lookalike"
BURST_KIND = "burst"


@dataclass
class Metrics:
    label: str
    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def precision(self) -> float:
        found = self.true_positive + self.false_positive
        return self.true_positive / found if found else 1.0

    @property
    def recall(self) -> float:
        total = self.true_positive + self.false_negative
        return self.true_positive / total if total else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "tp": self.true_positive,
            "fp": self.false_positive,
            "fn": self.false_negative,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
        }


def load_truth(root: Path) -> dict[str, dict]:
    path = root / "ground_truth.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"нет разметки: {path}\n  сгенерируй набор: python scripts/make_testset.py {root}"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {entry["rel"]: entry for entry in payload.values()}


def _pairs(groups: list[Group]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for group in groups:
        members = sorted(group.members)
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                out.add((members[i], members[j]))
    return out


def _expected_pairs(truth: dict[str, dict], kind: str) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for entry in truth.values():
        if entry["kind"] != kind:
            continue
        origin = entry.get("derived_from")
        if not origin:
            continue
        origin_rel = next(
            (t["rel"] for t in truth.values() if t["rel"].endswith("/" + origin) or t["rel"] == origin),
            None,
        )
        if origin_rel is None:
            continue
        a, b = sorted((entry["rel"], origin_rel))
        out.add((a, b))
    return out


def evaluate(
    root: Path,
    store: Store,
    phash: int = NEAR_DUP_PHASH_DISTANCE,
    cosine: float = NEAR_DUP_COSINE_STRONG,
) -> dict:
    truth = load_truth(root)
    known = {rel for rel in truth if any(p.rel_path == rel for p in store.photos())}
    truth = {rel: entry for rel, entry in truth.items() if rel in known}

    photos = store.photos()
    by_path = {p.rel_path: p for p in photos}
    scoped = [p for p in photos if p.rel_path in truth]

    all_groups, protected = analyze(
        scoped, store, phash_distance=phash, strong_cosine=cosine, include_burst=True
    )
    found_burst = burst_members(scoped)
    found_exact = [g for g in all_groups if g.kind == KIND_EXACT]
    found_near = [g for g in all_groups if g.kind not in (KIND_EXACT, BURST_KIND)]
    filtered = found_exact + found_near
    dropped_by_burst = sum(
        len(g.members)
        for g in filtered
    ) - len({rel for g in filtered for rel in g.members})

    exact_m = Metrics("точные дубликаты (md5)")
    exact_found = _pairs(found_exact)
    for a, b in exact_found:
        kinds = {truth.get(a, {}).get("kind"), truth.get(b, {}).get("kind")}
        if EXACT_DUP in kinds:
            exact_m.true_positive += 1
        else:
            exact_m.false_positive += 1
            exact_m.notes.append(f"ложное срабатывание: {pair[0]} ↔ {pair[1]}")
    for pair in _expected_pairs(truth, EXACT_DUP):
        if pair not in exact_found:
            exact_m.false_negative += 1
            exact_m.notes.append(f"пропущено: {pair[0]} ↔ {pair[1]}")

    near_m = Metrics(f"похожие кадры (pHash ≤ {phash}, cosine ≥ {cosine})")
    near_pairs = _pairs(found_near)
    burst_by_truth = {
        rel for rel, entry in truth.items() if entry["kind"] == BURST_KIND
    }
    for a, b in near_pairs:
        kinds = {truth.get(a, {}).get("kind"), truth.get(b, {}).get("kind")}
        if NEAR_DUP in kinds:
            near_m.true_positive += 1
        elif burst_by_truth & {a, b}:
            near_m.false_positive += 1
            near_m.notes.append(f"burst назван дублем: {a} ↔ {b}")
        else:
            near_m.false_positive += 1
            near_m.notes.append(f"ложное срабатывание: {a} ↔ {b}")
    for pair in _expected_pairs(truth, NEAR_DUP):
        if pair not in near_pairs:
            near_m.false_negative += 1
            near_m.notes.append(f"пропущено: {pair[0]} ↔ {pair[1]}")

    expected_burst = {
        entry["rel"] for entry in truth.values() if entry["kind"] == BURST_KIND
    }

    trap_m = Metrics("ловушки не названы дублями")
    trap_pairs = _expected_pairs(truth, LOOKALIKE)
    reported = _pairs(found_exact) | _pairs(found_near)
    for pair in trap_pairs:
        if pair in reported:
            trap_m.false_positive += 1
            trap_m.notes.append(f"ловушка попала в дубли: {pair[0]} ↔ {pair[1]}")
    trap_m.true_positive = len(trap_pairs) - trap_m.false_positive
    trap_m.false_negative = len(trap_pairs) - trap_m.true_positive

    redundant_kinds = {EXACT_DUP, NEAR_DUP}

    exact_victims = sorted(
        {rel for g in filtered if g.kind == KIND_EXACT for rel in g.victims()}
    )
    near_victims = sorted(
        {rel for g in filtered if g.kind != KIND_EXACT for rel in g.victims()}
    )
    quarantinable = sorted(set(exact_victims) | set(near_victims))

    default_m = Metrics("карантин по умолчанию (только exact) чист")
    for rel in exact_victims:
        if truth.get(rel, {}).get("kind") in redundant_kinds:
            default_m.true_positive += 1
        else:
            default_m.false_positive += 1
            default_m.notes.append(f"в карантин лишнее: {rel}")

    optin_m = Metrics("если разрешить near-дубли вручную")
    for rel in near_victims:
        if truth.get(rel, {}).get("kind") in redundant_kinds:
            optin_m.true_positive += 1
        else:
            optin_m.false_positive += 1
            optin_m.notes.append(f"near-кандидат лишний: {rel}")

    burst_m = Metrics("burst: ни один кадр не уходит в карантин")
    burst_m.true_positive = len(expected_burst & found_burst)
    burst_m.false_negative = len(expected_burst - found_burst)
    leaked = [rel for rel in quarantinable if rel in found_burst]
    burst_m.false_positive = len(leaked)
    for rel in leaked:
        burst_m.notes.append(f"burst попал бы в карантин: {rel}")

    stats = {
        "scored": len(truth),
        "orig": sum(1 for e in truth.values() if e["kind"] == ORIGINAL),
        "exact": sum(1 for e in truth.values() if e["kind"] == EXACT_DUP),
        "near": sum(1 for e in truth.values() if e["kind"] == NEAR_DUP),
        "burst": len(expected_burst),
        "lookalike": len({rel for rel, e in truth.items() if e["kind"] == LOOKALIKE}),
        "quarantinable": len(quarantinable),
        "reclaimable_bytes": sum(
            by_path[rel].size for rel in quarantinable if rel in by_path
        ),
        "dropped_by_burst": dropped_by_burst,
        "groups_after": len(filtered),
        "exact_victims": len(exact_victims),
        "near_victims": len(near_victims),
    }

    return {
        "stats": stats,
        "metrics": [exact_m, near_m, trap_m, default_m, optin_m, burst_m],
    }


def sweep(
    root: Path,
    store: Store,
    phash_values: tuple[int, ...] = (2, 4, 6, 8, 10),
    cosine_values: tuple[float, ...] = (0.80, 0.85, 0.90, 0.95, 0.98),
) -> list[dict]:
    rows = []
    for p_hash in phash_values:
        for cosine in cosine_values:
            result = evaluate(root, store, p_hash, cosine)
            rows.append(
                {
                    "phash": p_hash,
                    "cosine": cosine,
                    "near": next(m for m in result["metrics"] if m.label.startswith("похожие")).as_dict(),
                    "trap": next(m for m in result["metrics"] if m.label.startswith("ловушки")).as_dict(),
                    "safe": next(m for m in result["metrics"] if m.label.startswith("карантин по умолчанию")).as_dict(),
                    "optin": next(m for m in result["metrics"] if m.label.startswith("если разрешить")).as_dict(),
                    "burst": next(m for m in result["metrics"] if m.label.startswith("burst")).as_dict(),
                    "reclaimable": result["stats"]["reclaimable_bytes"],
                }
            )
    return rows