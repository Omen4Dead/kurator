"""Разведка порога NEAR_TIER2_COLOR_DIFF на эталоне.

Считает сигналы верификатора (verify.pair_signals) по:
  - ожидаемым near-парам: original -> деформации (crop/rot/frame/shot/edited,
    кроме burst и (1) — они идут по основному ярусу);
  - ловушкам: original -> lookalike той же сцены.

Вывод: таблица сигналов, чтобы порог отделял деформации от ловушек.
"""

import json
from pathlib import Path

from kurator.verify import pair_signals

ROOT = Path("/tmp/kurator-test")


def load_pairs():
    gt = json.load(open(ROOT / "ground_truth.json"))
    by_name = {name: info["rel"] for name, info in gt.items()}
    expected, traps = [], []
    for name, info in gt.items():
        src = info.get("derived_from")
        if not src:
            continue
        src_name = Path(src).name
        if src_name not in by_name:
            continue
        pair = (by_name[src_name], by_name[name])
        if "lookalike" in name:
            traps.append(pair)
        else:
            expected.append(pair)
    return expected, traps


def main():
    expected, traps = load_pairs()
    print(f"ожидаемые near-пары: {len(expected)}, ловушки: {len(traps)}\n")
    rows = []
    for label, pairs in (("EXPECT", expected), ("TRAP", traps)):
        for a, b in pairs:
            sig = pair_signals(ROOT, a, b)
            if sig is None:
                print(f"{label}  НЕ ЧИТАЕТСЯ  {Path(b).name}")
                continue
            corr, hist, cdiff = sig
            rows.append((label, Path(b).name, corr, hist, cdiff))
    rows.sort(key=lambda r: (r[0] != "TRAP", -r[4]))
    print(f"{'тип':7} {'пара':44} {'corr':>6} {'hist':>6} {'cdiff':>7}")
    for label, name, corr, hist, cdiff in rows:
        flag = ""
        if corr >= 0.80 and hist >= 0.60:
            flag = " <- пройдёт corr+hist"
        print(f"{label:7} {name:44} {corr:6.3f} {hist:6.3f} {cdiff:7.2f}{flag}")
    exp_cd = sorted(r[4] for r in rows if r[0] == "EXPECT")
    trap_cd = sorted(r[4] for r in rows if r[0] == "TRAP")
    print(f"\ncdiff ожидаемые: min={exp_cd[0]:.2f} max={exp_cd[-1]:.2f}")
    print(f"cdiff ловушки:   min={trap_cd[0]:.2f} max={trap_cd[-1]:.2f}")
    for t in (8, 10, 12, 15, 20, 25):
        tp = sum(1 for r in rows if r[0] == "EXPECT" and r[4] <= t)
        fp = sum(1 for r in rows if r[0] == "TRAP" and r[4] <= t)
        print(f"порог {t:5.1f}: near-проходит {tp}/{len(exp_cd)}, ловушек проходит {fp}/{len(trap_cd)}")


if __name__ == "__main__":
    main()
