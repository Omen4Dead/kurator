import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kurator.config import MODEL_FILENAME, default_model_path
from kurator.db import Store

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "make_testset.py"
SCENES = int(os.environ.get("KURATOR_TEST_SCENES", "40"))


def _model_path() -> Path:
    path = default_model_path()
    return path / MODEL_FILENAME if path.is_dir() else path


def _model_present() -> bool:
    return _model_path().is_file()


@pytest.mark.skipif(not _model_present(), reason="нет весов: ./scripts/pull-model.sh")
def test_pipeline_matches_ground_truth(tmp_path):
    root = tmp_path / "archive"
    subprocess.run(
        [sys.executable, str(SCRIPT), str(root), str(SCENES)],
        check=True,
        capture_output=True,
    )

    from kurator.encoder import SiglipVision
    from kurator.evaluate import evaluate
    from kurator.indexer import index_root

    store = Store.open(root)
    index_root(root, store, encoder=SiglipVision(_model_path()))

    result = evaluate(root, store)
    metrics = {m.label.split("(")[0].strip(): m for m in result["metrics"]}
    stats = result["stats"]

    def get(prefix: str):
        return next(m for k, m in metrics.items() if k.startswith(prefix))

    exact = get("точные")
    default = get("карантин по умолчанию")
    burst = get("burst")
    traps = get("ловушки")
    near = get("похожие")

    assert (exact.false_positive, exact.false_negative) == (0, 0), "точные дубли должны быть точными"
    assert (default.false_positive, default.false_negative) == (0, 0), (
        "карантин по умолчанию трогает только exact, и он обязан быть чистым"
    )
    assert burst.false_positive == 0, "burst-кадр попал бы в карантин"

    assert near.false_negative == 0, f"пропущено настоящих near-дублей: {near.notes[:3]}"
    assert traps.false_positive <= max(1, traps.true_positive // 2), (
        f"слишком много ловушек принято за дубли: {traps.false_positive} из "
        f"{traps.true_positive + traps.false_positive}; " + "; ".join(traps.notes[:3])
    )
    store.close()


@pytest.mark.skipif(not _model_present(), reason="нет весов: ./scripts/pull-model.sh")
def test_apply_never_touches_burst(tmp_path):
    root = tmp_path / "archive"
    subprocess.run(
        [sys.executable, str(SCRIPT), str(root), str(SCENES)],
        check=True,
        capture_output=True,
    )

    from kurator.dedupe import analyze
    from kurator.encoder import SiglipVision
    from kurator.indexer import index_root

    store = Store.open(root)
    index_root(root, store, encoder=SiglipVision(_model_path()))

    photos = store.photos()
    groups, protected = analyze(photos, store)
    quarantinable = {rel for g in groups if g.kind != "burst" for rel in g.victims()}

    assert quarantinable, "на таком наборе должно что-то найтись"
    assert not (quarantinable & protected), "burst-кадры попали в кандидаты"
    store.close()


@pytest.mark.skipif(not _model_present(), reason="нет весов: ./scripts/pull-model.sh")
def test_apply_and_undo_round_trip(tmp_path):
    root = tmp_path / "archive"
    subprocess.run(
        [sys.executable, str(SCRIPT), str(root), str(SCENES)],
        check=True,
        capture_output=True,
    )

    from kurator.encoder import SiglipVision
    from kurator.indexer import index_root
    from kurator.quarantine import apply_moves, plan_moves, undo_last_run

    store = Store.open(root)
    index_root(root, store, encoder=SiglipVision(_model_path()))
    before = {p.rel_path for p in store.photos()}

    victims = sorted(rel for rel in before if rel.endswith("(1).jpg"))
    assert victims

    moves = plan_moves(root, victims)
    apply_moves(store, moves, kind="exact", dry_run=False)
    assert not any((root / rel).exists() for rel in victims)

    undo_last_run(store)
    assert all((root / rel).exists() for rel in victims)
    store.close()