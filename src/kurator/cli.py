"""Командный интерфейс kurator."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import (
    BATCH_SIZE,
    CLUSTER_COSINE,
    MODEL_FILENAME,
    MODEL_LICENSE,
    MODEL_REPO,
    MODEL_SHA256,
    MODEL_SIZE_HINT,
NEAR_DUP_COSINE_STRONG,
    NEAR_DUP_PHASH_DISTANCE,
    QUARANTINE_DIRNAME,
    default_model_path,
)
from .db import Store
from .encoder import SiglipVision, download_model, model_is_present


def _fmt_bytes(size: int) -> str:
    units = ["Б", "КБ", "МБ", "ГБ", "ТБ"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:,.1f} {unit}".replace(",", " ")
        value /= 1024
    return f"{value:.1f} ТБ"


def _open_store(root: Path) -> Store:
    if not root.is_dir():
        raise SystemExit(f"нет такой папки: {root}")
    return Store.open(root)


class Tuning:
    def __init__(self, phash: float, cosine: float) -> None:
        self.phash = int(phash)
        self.cosine = float(cosine)


def _collect_groups(store: Store, include_burst: bool = True, tuning: Tuning | None = None):
    from .dedupe import analyze

    t = tuning or Tuning(NEAR_DUP_PHASH_DISTANCE, NEAR_DUP_COSINE_STRONG)
    photos = store.photos()
    groups, _ = analyze(
        photos,
        store,
        phash_distance=t.phash,
        strong_cosine=t.cosine,
        include_burst=include_burst,
    )
    return photos, groups


def _add_tuning_flags(parser) -> None:
    parser.add_argument(
        "--phash", type=int, default=NEAR_DUP_PHASH_DISTANCE,
        help=f"порог расстояния pHash для near-дублей (по умолчанию {NEAR_DUP_PHASH_DISTANCE})",
    )
    parser.add_argument(
        "--cosine", type=float, default=NEAR_DUP_COSINE_STRONG,
        help=f"порог косинуса для near-дублей (по умолчанию {NEAR_DUP_COSINE_STRONG})",
    )


# ---------------------------------------------------------------- команды


def cmd_models(args) -> int:
    path = Path(args.path).expanduser() if args.path else default_model_path()
    if path.is_dir():
        path = path / MODEL_FILENAME
    print(f"репозиторий : {MODEL_REPO}")
    print(f"лицензия   : {MODEL_LICENSE}")
    print(f"размер     : ~{MODEL_SIZE_HINT}")
    print(f"sha256     : {MODEL_SHA256}")
    print(f"путь       : {path}")
    if model_is_present(path):
        print("статус     : на месте")
        return 0
    print("статус     : не скачан")
    if args.pull:
        download_model(path)
        print("скачал и проверил хеш")
        return 0
    print("\nскачать: ./scripts/pull-model.sh  или  kurator models --pull")
    return 1


def cmd_index(args) -> int:
    root = Path(args.root).expanduser().resolve()
    store = _open_store(root)
    from .indexer import index_root

    model_path = Path(args.model).expanduser() if args.model else default_model_path()
    if model_path.is_dir():
        model_path = model_path / MODEL_FILENAME
    encoder = None
    if not args.no_embeddings:
        if not model_is_present(model_path):
            if args.download:
                download_model(model_path)
            else:
                print(
                    f"весов нет ({model_path}) — индекс без эмбеддингов.\n"
                    "  ./scripts/pull-model.sh, чтобы включить near-dupes и кластеры",
                    file=sys.stderr,
                )
        if model_is_present(model_path):
            encoder = SiglipVision(model_path)
            print(f"модель: {model_path} → {encoder.dim}-d, батч {BATCH_SIZE}")

    print(f"сканирую {root}")
    stats = index_root(
        root,
        store,
        encoder=encoder,
        rehash=args.rehash,
        progress=lambda message: print(message, flush=True),
    )
    store.commit()
    print(
        f"\nготово: всего {stats['seen']}, новых {stats['indexed']}, "
        f"пропущено {stats['skipped']}, удалено из индекса {stats['removed']}, "
        f"эмбеддингов {stats['embedded']}"
    )
    if stats["unreadable"]:
        print(f"нечитаемых файлов: {stats['unreadable']}", file=sys.stderr)
    store.close()
    return 0


def cmd_backfill(args) -> int:
    root = Path(args.root).expanduser().resolve()
    store = _open_store(root)
    from .indexer import backfill_embeddings, encoder_or_none

    model_path = Path(args.model).expanduser() if args.model else default_model_path()
    if model_path.is_dir():
        model_path = model_path / MODEL_FILENAME
    encoder = encoder_or_none(model_path, allow_download=args.download)
    if encoder is None:
        print("весов нет — нечего досчитывать", file=sys.stderr)
        return 1
    done = backfill_embeddings(root, store, encoder, progress=lambda m: print(m, flush=True))
    print(f"досчитано эмбеддингов: {done}")
    store.close()
    return 0


def cmd_stats(args) -> int:
    root = Path(args.root).expanduser().resolve()
    store = _open_store(root)
    photos = store.photos()
    total = sum(p.size for p in photos)
    with_md5 = sum(1 for p in photos if p.md5)
    embedded = store.embedded_count()
    print(f"корень          : {root}")
    print(f"файлов в индексе: {len(photos)}")
    print(f"размер          : {_fmt_bytes(total)}")
    print(f"последний индекс: {store.get_meta('last_index_at', '—')}")
    print(f"md5 посчитан   : {with_md5}/{len(photos)}")
    print(f"эмбеддингов     : {embedded}/{len(photos)}")
    if embedded < len(photos):
        print("  добрать: kurator backfill --root .")
    db = store.path
    print(f"база            : {db} ({_fmt_bytes(db.stat().st_size)})")
    store.close()
    return 0


def cmd_dups(args) -> int:
    root = Path(args.root).expanduser().resolve()
    store = _open_store(root)
    photos, groups = _collect_groups(
        store, include_burst=not args.no_burst,
        tuning=Tuning(getattr(args, "phash", NEAR_DUP_PHASH_DISTANCE), getattr(args, "cosine", NEAR_DUP_COSINE_STRONG)),
    )
    index = {p.rel_path: p for p in photos}

    from .dedupe import BURST, summarize

    print(summarize(groups))
    shown = 0
    for group in groups:
        if group.kind == BURST and args.skip_burst:
            continue
        if group.size < args.min_size:
            continue
        shown += 1
        if shown > args.limit:
            print(f"\n… ещё групп, показаны первые {args.limit} (подними --limit)")
            break
        head = f"[{group.kind}] {group.size} файла(ов)"
        print(f"\n{head}")
        if group.detail:
            print("  " + ", ".join(f"{k}={v}" for k, v in group.detail.items() if v is not None))
        for rel in group.members:
            photo = index[rel]
            role = ""
            if group.kind == BURST:
                role = "не трогаю"
            elif rel == group.keeper:
                role = "оставляю"
            else:
                role = "кандидат"
            print(f"  {rel}  {_fmt_bytes(photo.size)}  {photo.taken_at or '—'}  {role}")
    store.close()
    return 0


def cmd_clusters(args) -> int:
    root = Path(args.root).expanduser().resolve()
    store = _open_store(root)
    from .cluster import cluster_paths, coherence

    ids, paths, vectors = store.embeddings()
    if not paths:
        print("эмбеддингов нет — сначала kurator index", file=sys.stderr)
        store.close()
        return 1
    print(f"векторов: {len(paths)}, порог cosine ≥ {args.threshold}")

    def tick(done: int, total: int, centroids: int) -> None:
        print(f"  {done}/{total}, кластеров {centroids}", flush=True)

    groups, centroids = cluster_paths(
        paths, vectors,
        threshold=args.threshold, min_size=args.min_size, progress=tick,
    )
    index = {p: i for i, p in enumerate(paths)}
    print(f"всего кластеров: {centroids}, из них ≥ {args.min_size} файлов: {len(groups)}")
    for number, members in enumerate(groups, start=1):
        if number > args.limit:
            print(f"\n… показаны первые {args.limit}")
            break
        stats = coherence(vectors, members, index)
        folder = str(Path(members[0]).parent)
        mean = f"{stats['mean']:.3f}" if stats["mean"] is not None else "—"
        print(f"\n{number}. {len(members)} файла(ов) · однородность {mean} · {folder}")
        for rel in members[: args.preview]:
            print(f"   {rel}")
        if len(members) > args.preview:
            print(f"   … ещё {len(members) - args.preview}")
    store.close()
    return 0


def cmd_evaluate(args) -> int:
    root = Path(args.root).expanduser().resolve()
    store = _open_store(root)
    from .evaluate import evaluate, sweep

    if args.sweep:
        rows = sweep(root, store)
        print(f"{'pHash':>5} {'cosine':>6} {'near':>6} {'F1':>6} {'ловушки':>8} {'лишнее в exact':>14} {'burst':>6}")
        for row in rows:
            near, trap, safe, burst = row["near"], row["trap"], row["safe"], row["burst"]
            flag = "" if (safe["fp"] == 0 and burst["fp"] == 0) else "  ⚠"
            print(
                f"{row['phash']:>5} {row['cosine']:>6.2f} "
                f"{str(near['tp']) + '/' + str(near['fn']):>6} {near['f1']:>6.3f} "
                f"{trap['fp']:>8} {safe['fp']:>14} {burst['fp']:>6}{flag}"
            )
        print("\nловушки — сколько похожих, но разных сюжетов названо дублями")
        print("лишнее в exact — попало ли что-нибудь лишнее в карантин по умолчанию")
        print("⚠ означает, что дефолтный карантин не чист")
        store.close()
        return 0

    result = evaluate(root, store, args.phash, args.cosine)
    stats = result["stats"]
    print(f"корень: {root}")
    print(f"размечено файлов: {stats['scored']} "
          f"(оригиналов {stats['orig']}, exact {stats['exact']}, near {stats['near']}, burst {stats['burst']})")
    print()
    print(f"{'метрика':<52} {'TP':>4} {'FP':>4} {'FN':>4} {'prec':>7} {'recall':>7} {'F1':>7}")
    for metric in result["metrics"]:
        d = metric.as_dict()
        name = metric.label[:52]
        print(f"{name:<52} {d['tp']:>4} {d['fp']:>4} {d['fn']:>4} "
              f"{d['precision']:>7.3f} {d['recall']:>7.3f} {d['f1']:>7.3f}")
    print()
    print(f"кандидатов в карантин: {stats['quarantinable']} на {_fmt_bytes(stats['reclaimable_bytes'])}")

    notes = [n for m in result["metrics"] for n in m.notes]
    if notes and args.notes:
        print(f"\nподробности ({len(notes)}):")
        for note in notes[: args.notes]:
            print(f"  {note}")
    elif notes:
        print(f"\nрасхождений: {len(notes)} (показать: --notes)")
    store.close()
    return 0


def cmd_ui(args) -> int:
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"нет такой папки: {root}")
    from .web.server import resolve_port, serve

    port = resolve_port(args.port)
    serve(root, host="127.0.0.1", port=port, open_browser=not args.no_browser)
    return 0


def cmd_calibrate(args) -> int:
    root = Path(args.root).expanduser().resolve()
    store = _open_store(root)
    from .dedupe import calibration

    photos = store.photos()
    data = calibration(store, photos)
    total_pairs = data["photos"] * (data["photos"] - 1) // 2
    print(f"корень       : {root}")
    print(f"файлов       : {data['photos']}")
    print(f"эмбеддингов  : {data['embedded']}")
    print(f"всего пар    : {total_pairs}")
    if total_pairs == 0:
        store.close()
        return 1

    print("\nпары по расстоянию pHash (меньше = сильнее похожи):")
    print("  pHash <=   0 : %8d   (%.3f%% пар)" % (data["phash_pairs"][0], 100 * data["phash_pairs"][0] / total_pairs))
    for t in sorted(k for k in data["phash_pairs"] if k):
        print("  pHash <= %3d : %8d   (%.3f%% пар)" % (t, data["phash_pairs"][t], 100 * data["phash_pairs"][t] / total_pairs))

    print("\nпары по косинусу эмбеддингов (больше = сильнее похожи):")
    for t in sorted(data["cosine_pairs"]):
        print("  cosine >= %.2f : %8d   (%.3f%% пар)" % (t, data["cosine_pairs"][t], 100 * data["cosine_pairs"][t] / total_pairs))

    print("\nсколько групп выйдет при разных порогах:")
    print("  %-10s %-10s %-10s" % ("pHash", "cosine", "групп"))
    for p_hash in (2, 4, 6, 8, 10):
        for cosine in (0.90, 0.95, 0.98, 0.99):
            _, groups = _collect_groups(store, include_burst=False, tuning=Tuning(p_hash, cosine))
            near = [g for g in groups if g.kind != "exact"]
            print("  %-10d %-10.2f %-10d" % (p_hash, cosine, len(near)))
    print("\nдефолты сейчас: pHash %d, cosine %.2f" % (NEAR_DUP_PHASH_DISTANCE, NEAR_DUP_COSINE_STRONG))
    print("подбери значения, при которых групп разумное число, и передай их")
    print("флагами --phash и --cosine в dups/report/apply.")
    store.close()
    return 0


def cmd_report(args) -> int:
    root = Path(args.root).expanduser().resolve()
    store = _open_store(root)
    from .report import build_report

    photos, groups = _collect_groups(store, tuning=Tuning(args.phash, args.cosine))
    out_dir = Path(args.out).expanduser() if args.out else root / "out"
    info = build_report(
        root, photos, groups, out_dir, sheets=not args.no_sheets, max_groups=args.max_groups
    )
    print(f"отчёт:   {info['html']}")
    print(f"markdown:{info['markdown']}")
    print(f"листов:  {info['sheets']}")
    print(
        f"кандидатов: {info['reclaimable_count']} на {_fmt_bytes(info['reclaimable_bytes'])}"
    )
    store.close()
    return 0


def _collect_groups_protected(store: Store, tuning: Tuning | None = None):
    from .dedupe import analyze

    t = tuning or Tuning(NEAR_DUP_PHASH_DISTANCE, NEAR_DUP_COSINE_STRONG)
    photos = store.photos()
    groups, protected = analyze(
        photos, store, phash_distance=t.phash, strong_cosine=t.cosine, include_burst=True
    )
    return photos, groups, protected


def cmd_apply(args) -> int:
    root = Path(args.root).expanduser().resolve()
    store = _open_store(root)
    from .quarantine import apply_moves, plan_moves

    wanted = {k.strip() for k in args.kind.split(",") if k.strip()}
    photos, groups, protected = _collect_groups_protected(
        store, tuning=Tuning(args.phash, args.cosine)
    )
    selected: list[Group] = [g for g in groups if g.kind in wanted]
    victims = sorted({rel for g in selected for rel in g.victims()})
    skipped = [rel for rel in victims if rel in protected]

    if not victims:
        print("нечего переносить")
        store.close()
        return 0

    moves = plan_moves(root, victims)
    index = {p.rel_path: p for p in photos}
    reclaim = sum(index[rel].size for rel in victims if rel in index)
    print(f"кандидатов: {len(victims)} на {_fmt_bytes(reclaim)}")
    if skipped:
        print(f"пропущено как burst: {len(skipped)} (никогда не трогаю)")
    for move in moves[: args.limit]:
        print(f"  {move.src.relative_to(root)}  →  {QUARANTINE_DIRNAME}/{move.dst.relative_to(root / QUARANTINE_DIRNAME)}")
    if len(moves) > args.limit:
        print(f"  … ещё {len(moves) - args.limit}")

    if args.dry_run:
        print("\n--dry-run: ничего не перемещено")
        store.close()
        return 0
    if not args.yes:
        print(f"\nповтори с --yes, чтобы выполнить ({len(moves)} файлов)")
        store.close()
        return 1

    result = apply_moves(store, moves, kind=",".join(sorted(wanted)), dry_run=False)
    print(f"\nперенесено: {result['moved']}")
    if result["failed"]:
        print(f"с ошибками: {len(result['failed'])}")
    print(f"манифест:   {result['manifest']}")
    print("откат: kurator undo")
    store.close()
    return 0


def cmd_undo(args) -> int:
    root = Path(args.root).expanduser().resolve()
    store = _open_store(root)
    from .quarantine import undo_last_run

    if not args.yes:
        print("откат переместит файлы обратно. Повтори с --yes.")
        store.close()
        return 1
    result = undo_last_run(store)
    print(f"возвращено: {result.get('moved', 0)}")
    if result.get("missing"):
        print(f"не найдено: {len(result['missing'])}")
    if result.get("message"):
        print(result["message"])
    store.close()
    return 0


# ---------------------------------------------------------------- разбор


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kurator",
        description="Офлайновый разбор фотопарка: дубликаты, near-dupes, визуальные кластеры.",
    )
    parser.add_argument("--version", action="version", version=f"kurator {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("models", help="где лежат веса и как их скачать")
    p.add_argument("--path", help="куда смотреть/качать (по умолчанию models/)")
    p.add_argument("--pull", action="store_true", help="скачать и проверить хеш")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("index", help="построить или обновить индекс")
    p.add_argument("root", nargs="?", default=".", help="корень фотопарки")
    p.add_argument("--model", help="путь к onnx")
    p.add_argument("--download", action="store_true", help="скачать веса, если их нет")
    p.add_argument("--no-embeddings", action="store_true", help="только хеши, без модели")
    p.add_argument("--rehash", action="store_true", help="пересчитать всё заново")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("backfill", help="досчитать эмбеддинги для уже проиндексированных")
    p.add_argument("root", nargs="?", default=".", help="корень фотопарки")
    p.add_argument("--model", help="путь к onnx")
    p.add_argument("--download", action="store_true", help="скачать веса, если их нет")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("stats", help="что в индексе")
    p.add_argument("root", nargs="?", default=".", help="корень фотопарки")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("dups", help="показать группы дубликатов")
    p.add_argument("root", nargs="?", default=".", help="корень фотопарки")
    p.add_argument("--min-size", type=int, default=2, help="минимальный размер группы")
    p.add_argument("--limit", type=int, default=25, help="сколько групп показать")
    p.add_argument("--skip-burst", action="store_true", help="не показывать burst")
    p.add_argument("--no-burst", action="store_true", help="вообще не считать burst")
    _add_tuning_flags(p)
    p.set_defaults(func=cmd_dups)

    p = sub.add_parser("clusters", help="визуальные кластеры по эмбеддингам")
    p.add_argument("root", nargs="?", default=".", help="корень фотопарки")
    p.add_argument("--threshold", type=float, default=CLUSTER_COSINE, help="порог косинуса")
    p.add_argument("--min-size", type=int, default=3, help="минимальный размер кластера")
    p.add_argument("--limit", type=int, default=20, help="сколько кластеров показать")
    p.add_argument("--preview", type=int, default=8, help="сколько файлов на кластер")
    p.set_defaults(func=cmd_clusters)

    p = sub.add_parser("evaluate", help="замерить точность дедупа против эталонной разметки")
    p.add_argument("root", nargs="?", default=".", help="корень фотопарки")
    p.add_argument("--sweep", action="store_true", help="перебрать пороги в таблице")
    p.add_argument("--notes", type=int, default=20, help="сколько расхождений показать")
    _add_tuning_flags(p)
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("ui", help="локальный веб-интерфейс в браузере")
    p.add_argument("root", nargs="?", default=".", help="корень фотопарки")
    p.add_argument("--port", type=int, default=8765, help="порт (берёт следующий свободный)")
    p.add_argument("--no-browser", action="store_true", help="не открывать браузер самому")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("calibrate", help="подобрать пороги near-дублей по своим фото")
    p.add_argument("root", nargs="?", default=".", help="корень фотопарки")
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("report", help="markdown + html + контакт-листы")
    p.add_argument("root", nargs="?", default=".", help="корень фотопарки")
    p.add_argument("--out", help="куда положить отчёт (по умолчанию <root>/out)")
    p.add_argument("--no-sheets", action="store_true", help="без картинок")
    p.add_argument("--max-groups", type=int, default=40, help="листов на категорию")
    _add_tuning_flags(p)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("apply", help="перенести кандидатов в карантин")
    p.add_argument("root", nargs="?", default=".", help="корень фотопарки")
    p.add_argument(
        "--kind",
        default="exact",
        help="какие группы трогать: exact,near-strong (near-weak и burst — никогда)",
    )
    p.add_argument("--dry-run", action="store_true", help="только показать план")
    p.add_argument("--yes", action="store_true", help="выполнить")
    p.add_argument("--limit", type=int, default=30, help="сколько строк показать")
    _add_tuning_flags(p)
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("undo", help="вернуть последний перенос обратно")
    p.add_argument("root", nargs="?", default=".", help="корень фотопарки")
    p.add_argument("--yes", action="store_true", help="выполнить")
    p.set_defaults(func=cmd_undo)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())