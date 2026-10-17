"""Отчёты: markdown + html + контакт-листы."""

from __future__ import annotations

import datetime as dt
import html
from pathlib import Path

from .config import (
    CLUSTER_COSINE,
    MODEL_LICENSE,
    MODEL_REPO,
    NEAR_DUP_COSINE_STRONG,
    NEAR_DUP_PHASH_DISTANCE,
)
from .contact import contact_sheet
from .dedupe import BURST, EXACT, NEAR_STRONG, NEAR_WEAK, Group
from .db import Photo

KIND_LABEL = {
    EXACT: "точные дубликаты",
    NEAR_STRONG: "похожие кадры (вероятный дубль)",
    NEAR_WEAK: "похожая композиция, разные кадры",
    BURST: "burst-серии (не трогать)",
}

KIND_ORDER = [EXACT, NEAR_STRONG, NEAR_WEAK, BURST]

HTML_TEMPLATE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>kurator — разбор фотографий</title>
<style>
 :root {{ color-scheme: dark; }}
 body {{ background:#18181b; color:#e4e4e7; font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
        margin:0; padding:32px; }}
 h1 {{ font-size:26px; margin:0 0 4px; }}
 h2 {{ font-size:19px; margin:34px 0 10px; padding-bottom:6px; border-bottom:1px solid #3f3f46; }}
 .sub {{ color:#a1a1aa; margin:0 0 24px; }}
 table {{ border-collapse:collapse; width:100%; margin:10px 0; }}
 th,td {{ text-align:left; padding:6px 10px; border-bottom:1px solid #2c2c31; vertical-align:top; }}
 th {{ color:#a1a1aa; font-weight:500; font-size:13px; }}
 code {{ background:#27272a; padding:1px 5px; border-radius:4px; font-size:13px; }}
 .keep {{ color:#facc15; }}
 .drop {{ color:#fca5a5; }}
 .card {{ margin:18px 0; padding:14px; background:#1f1f23; border:1px solid #2f2f36; border-radius:10px; }}
 .meta {{ color:#a1a1aa; font-size:13px; margin:6px 0 10px; }}
 img {{ max-width:100%; border-radius:8px; }}
 .warn {{ background:#422006; border:1px solid #854d0e; color:#fde68a; padding:12px 14px; border-radius:8px; margin:18px 0; }}
 .foot {{ color:#71717a; font-size:13px; margin-top:44px; }}
</style></head><body>
{body}
</body></html>
"""


def _fmt_bytes(size: int) -> str:
    units = ["Б", "КБ", "МБ", "ГБ", "ТБ"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:,.1f} {unit}".replace(",", " ")
        value /= 1024
    return f"{value:.1f} ТБ"


def build_report(
    root: Path,
    photos: list[Photo],
    groups: list[Group],
    out_dir: Path,
    sheets: bool = True,
    max_groups: int = 40,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    sheets_dir = out_dir / "sheets"
    index = {p.rel_path: p for p in photos}

    by_kind: dict[str, list[Group]] = {kind: [] for kind in KIND_ORDER}
    for group in groups:
        by_kind.setdefault(group.kind, []).append(group)
    for kind in by_kind:
        by_kind[kind].sort(key=lambda g: (-g.size, g.members[0]))

    reclaim = 0
    victims = 0
    for group in groups:
        if group.kind == BURST:
            continue
        reclaim += sum(index[m].size for m in group.victims() if m in index)
        victims += len(group.victims())

    sheet_links: dict[str, list[str]] = {}

    if sheets:
        for kind in KIND_ORDER:
            made = []
            for number, group in enumerate(by_kind.get(kind, [])[:max_groups], start=1):
                name = f"{kind}_{number:03d}.jpg"
                keepers = {group.keeper} if group.keeper else None
                path = contact_sheet(
                    root,
                    group.members,
                    sheets_dir / name,
                    title=f"{KIND_LABEL.get(kind, kind)} · {group.size} файла(ов)",
                    keepers=keepers,
                )
                if path:
                    made.append(f"sheets/{name}")
            sheet_links[kind] = made

    md = _markdown(root, index, by_kind, reclaim, victims, sheet_links)
    (out_dir / "report.md").write_text(md, encoding="utf-8")
    html_doc = _html(root, index, by_kind, reclaim, victims, sheet_links)
    (out_dir / "report.html").write_text(html_doc, encoding="utf-8")

    return {
        "dir": str(out_dir),
        "markdown": str(out_dir / "report.md"),
        "html": str(out_dir / "report.html"),
        "sheets": sum(len(v) for v in sheet_links.values()),
        "reclaimable_bytes": reclaim,
        "reclaimable_count": victims,
        "total_groups": len(groups),
    }


def _header(root: Path, photos: list[Photo]) -> list[str]:
    return [
        "# kurator — разбор фотографий",
        "",
        f"- корень: `{root}`",
        f"- снимков в индексе: {len(photos)}",
        f"- сгенерировано: {dt.datetime.now().isoformat(timespec='seconds')}",
        f"- модель: `{MODEL_REPO}` ({MODEL_LICENSE}), локально, без сети",
        "",
    ]


def _markdown(
    root: Path,
    index: dict[str, Photo],
    by_kind: dict[str, list[Group]],
    reclaim: int,
    victims: int,
    sheet_links: dict[str, list[str]],
) -> str:
    lines = _header(root, list(index.values()))
    lines += [
        "## Итог",
        "",
        "| что | групп |",
        "|------|-------|",
    ]
    for kind in KIND_ORDER:
        lines.append(f"| {KIND_LABEL.get(kind, kind)} | {len(by_kind.get(kind, []))} |")
    lines += [
        "",
        f"Кандидатов в перенос: **{victims}** на {_fmt_bytes(reclaim)}.",
        "",
    ]

    if by_kind.get(BURST):
        lines += [
            "> burst-серии показаны для сведения и **никогда** не переносятся автоматически:",
            "> там альтернативные кадры одного момента.",
            "",
        ]

    for kind in KIND_ORDER:
        items = by_kind.get(kind, [])
        if not items:
            continue
        lines += [f"## {KIND_LABEL.get(kind, kind)}", ""]
        links = sheet_links.get(kind, [])
        for number, group in enumerate(items, start=1):
            title = group.members[0].split("/")[-1]
            detail = ""
            if kind == EXACT:
                detail = f"md5 `{group.detail.get('md5', '')[:12]}…`"
            elif kind in (NEAR_STRONG, NEAR_WEAK):
                cos = group.detail.get("cosine")
                dist = group.detail.get("phash_distance")
                parts = []
                if dist is not None:
                    parts.append(f"pHash ≤ {dist} (порог {NEAR_DUP_PHASH_DISTANCE})")
                if cos is not None:
                    parts.append(f"cosine {cos:.3f} (порог {NEAR_DUP_COSINE_STRONG})")
                detail = ", ".join(parts)
            elif kind == BURST:
                detail = str(group.detail.get("reason", ""))

            lines.append(f"### {number}. {title} — {group.size} файла(ов)")
            if detail:
                lines.append("")
                lines.append(detail)
            lines.append("")
            if number <= len(links) and links[number - 1]:
                lines.append(f"![{title}]({links[number - 1]})")
                lines.append("")
            lines.append("| файл | размер | дата | роль |")
            lines.append("|------|--------|------|------|")
            for rel in group.members:
                photo = index.get(rel)
                size = _fmt_bytes(photo.size) if photo else "—"
                taken = (photo.taken_at or "—") if photo else "—"
                role = "оставляю" if rel == group.keeper else ("не трогаю" if kind == BURST else "кандидат")
                lines.append(f"| `{rel}` | {size} | {taken} | {role} |")
            lines.append("")
            if kind == BURST:
                continue

    lines += [
        "## Как действовать",
        "",
        "```bash",
        "# посмотреть, что именно попадёт в карантин",
        "kurator apply --kind exact --dry-run",
        "",
        "# перенести (файлы не удаляются, только переезжают)",
        "kurator apply --kind exact",
        "",
        "# откатить",
        "kurator undo",
        "```",
        "",
        f"Кластеры считаются при пороге косинуса {CLUSTER_COSINE}.",
        "",
    ]
    return "\n".join(lines)


def _html(
    root: Path,
    index: dict[str, Photo],
    by_kind: dict[str, list[Group]],
    reclaim: int,
    victims: int,
    sheet_links: dict[str, list[str]],
) -> str:
    esc = html.escape
    parts: list[str] = [
        "<h1>kurator — разбор фотографий</h1>",
        f'<p class="sub"><code>{esc(str(root))}</code> · снимков в индексе: {len(index)} · '
        f"модель <code>{esc(MODEL_REPO)}</code> локально</p>",
        '<div class="warn">Ничего не удаляется: файлы только переносятся в '
        "<code>.kurator/quarantine/</code>, откат — одной командой.</div>",
        "<h2>Итог</h2><table>",
        "<tr><th>что</th><th>групп</th></tr>",
    ]
    for kind in KIND_ORDER:
        parts.append(
            f"<tr><td>{esc(KIND_LABEL.get(kind, kind))}</td>"
            f"<td>{len(by_kind.get(kind, []))}</td></tr>"
        )
    parts.append("</table>")
    parts.append(
        f"<p>Кандидатов в перенос: <b>{victims}</b> на <b>{esc(_fmt_bytes(reclaim))}</b>.</p>"
    )

    for kind in KIND_ORDER:
        items = by_kind.get(kind, [])
        if not items:
            continue
        parts.append(f"<h2>{esc(KIND_LABEL.get(kind, kind))}</h2>")
        links = sheet_links.get(kind, [])
        for number, group in enumerate(items, start=1):
            title = esc(group.members[0].split("/")[-1])
            detail_bits = []
            if kind == EXACT:
                detail_bits.append(f"md5 <code>{esc(group.detail.get('md5', '')[:12])}…</code>")
            elif kind in (NEAR_STRONG, NEAR_WEAK):
                cos = group.detail.get("cosine")
                if cos is not None:
                    detail_bits.append(f"cosine {cos:.3f}")
                if group.detail.get("phash_distance") is not None:
                    detail_bits.append(f"pHash {group.detail['phash_distance']}")
            elif kind == BURST:
                detail_bits.append(esc(str(group.detail.get("reason", ""))))
            parts.append('<div class="card">')
            parts.append(f"<b>{number}. {title}</b> — {group.size} файла(ов)")
            parts.append(f'<div class="meta">{", ".join(detail_bits)}</div>')
            if number <= len(links) and links[number - 1]:
                parts.append(f'<img src="{esc(links[number - 1])}" alt="{title}">')
            parts.append("<table><tr><th>файл</th><th>размер</th><th>дата</th><th>роль</th></tr>")
            for rel in group.members:
                photo = index.get(rel)
                size = esc(_fmt_bytes(photo.size)) if photo else "—"
                taken = esc(photo.taken_at or "—") if photo else "—"
                if kind == BURST:
                    role = '<span class="drop">не трогаю</span>'
                elif rel == group.keeper:
                    role = '<span class="keep">оставляю</span>'
                else:
                    role = '<span class="drop">кандидат</span>'
                parts.append(f"<tr><td><code>{esc(rel)}</code></td><td>{size}</td><td>{taken}</td><td>{role}</td></tr>")
            parts.append("</table></div>")

    parts.append(
        '<p class="foot">Сгенерировано локально. '
        f"Кластеры: cosine ≥ {CLUSTER_COSINE}. "
        "Сеть при работе не используется.</p>"
    )
    return HTML_TEMPLATE.format(body="\n".join(parts))