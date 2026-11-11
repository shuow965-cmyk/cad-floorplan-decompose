#!/usr/bin/env python3
"""One-screen review: category-colored overview with numbers + category thumbnails.

  python compose_review.py --source src.dxf --viewport xmin,ymin,xmax,ymax \\
      --classification classification.json --questions questions.json \\
      --font NotoSansCJKSC.otf --out review.png \\
      --title '住宅平面初分类 第1轮审核'

  --source / --viewport must be in millimetres (use classify.py's working copy
  and units.json). If --units is given, working source and viewport_mm are
  taken from that file unless overridden.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_review import render_ents, to_px  # noqa: E402

COLORS = {
    "墙体_保留": "#E6E6E6",
    "窗": "#FFBF00",
    "墙体_门框": "#7EB8FF",
    "柜体": "#FC1491",
    "家具": "#B266FF",
    "洁具": "#00E5FF",
    "电器": "#FF8C00",
    "门": "#39FF14",
    "灯具": "#FF3B3B",
    "其他": "#707070",
    "question": "#FFD000",
}
PREFIX = {
    "门": "D",
    "家具": "F",
    "柜体": "C",
    "洁具": "J",
    "电器": "E",
    "窗": "W",
    "灯具": "L",
    "墙体_门框": "JMB",
}
THUMB_CATS = ["墙体_保留", "窗", "门", "柜体", "洁具", "电器", "家具", "其他"]
PAINT_ORDER = ["墙体_保留", "墙体_门框", "窗", "门", "柜体", "洁具", "电器", "家具", "灯具"]


def dark_composite(base, overlay):
    b, o = np.array(base), np.array(overlay)
    mask = o.max(axis=2) > 25
    b[mask] = o[mask]
    return Image.fromarray(b)


def load_units(path: str) -> dict:
    if not path:
        return {}
    return json.loads(Path(path).read_text())


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Compose a one-screen category review PNG")
    p.add_argument("--source", default="", help="DXF in millimetres (working copy if units were converted)")
    p.add_argument("--viewport", default="", help="xmin,ymin,xmax,ymax in the same units as --source")
    p.add_argument("--classification", required=True)
    p.add_argument("--questions", default="", help="questions.json; optional if no Q items")
    p.add_argument("--font", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--title", default="住宅平面初分类 审核")
    p.add_argument("--content", type=int, default=1400, help="overview width/height in px")
    p.add_argument("--thumb", type=int, default=420)
    p.add_argument("--units", default="", help="optional units.json from classify.py")
    p.add_argument("--labels-out", default="", help="optional path to write placed labels JSON")
    args = p.parse_args(argv)

    units = load_units(args.units)
    src = args.source or units.get("working_source") or units.get("source")
    if not src:
        raise SystemExit("need --source or --units with working_source")
    if args.viewport:
        vp = tuple(float(x) for x in args.viewport.split(","))
    elif units.get("viewport_mm"):
        vp = tuple(units["viewport_mm"])
    else:
        raise SystemExit("need --viewport or --units with viewport_mm")
    assert len(vp) == 4

    recs = json.loads(Path(args.classification).read_text())
    questions = json.loads(Path(args.questions).read_text()) if args.questions and Path(args.questions).exists() else []

    by_cat = defaultdict(list)
    q_handles = []
    for r in recs:
        if r.get("status") == "exclude":
            continue
        if r.get("status") == "question":
            by_cat["question"].append(r)
            q_handles.append(r["handle"])
        else:
            by_cat[r.get("category") or "其他"].append(r)

    all_h = [r["handle"] for r in recs if r.get("status") != "exclude"]
    content = args.content
    print("render gray underlay…")
    gray = render_ents(src, vp, "#707070", content, content, all_h, "ignore")
    overview = gray.copy()
    for cat in PAINT_ORDER:
        hs = [r["handle"] for r in by_cat.get(cat, [])]
        if not hs:
            continue
        print("  overlay", cat, len(hs))
        overview = dark_composite(overview, render_ents(src, vp, COLORS[cat], content, content, hs, "ignore"))
    if q_handles:
        print("  overlay Q", len(q_handles))
        overview = dark_composite(
            overview, render_ents(src, vp, COLORS["question"], content, content, q_handles, "ignore")
        )

    labels = []
    for cat, prefix in PREFIX.items():
        items = [r for r in by_cat.get(cat, []) if r.get("type") == "INSERT" and r.get("bb")]
        items.sort(key=lambda r: (r["bb"][1], r["bb"][0]))
        for i, r in enumerate(items, 1):
            x0, y0, x1, y1 = r["bb"]
            labels.append({
                "id": f"{prefix}{i:02d}", "wx": (x0 + x1) / 2, "wy": (y0 + y1) / 2,
                "handle": r["handle"], "block": r.get("block") or "",
                "category": cat, "kind": "official",
            })
    rec_by_h = {str(r["handle"]).upper(): r for r in recs}
    for q in questions:
        hs = q.get("handles") or []
        rec = None
        for h in hs:
            rec = rec_by_h.get(str(h).upper())
            if rec and rec.get("bb"):
                break
        if not rec or not rec.get("bb"):
            continue
        x0, y0, x1, y1 = rec["bb"]
        labels.append({
            "id": q.get("id") or "Q", "wx": (x0 + x1) / 2, "wy": (y0 + y1) / 2,
            "handle": rec["handle"], "block": rec.get("block") or "",
            "category": "question", "kind": "q", "handles": hs,
        })

    font_label = ImageFont.truetype(args.font, 15)
    draw = ImageDraw.Draw(overview)
    boxes = []
    for lab in labels:
        cx, cy = to_px(lab["wx"], lab["wy"], vp, content, 0)
        text = lab["id"]
        rgb = (255, 208, 0) if lab["kind"] == "q" else (255, 224, 0)
        r = 18 if len(text) <= 3 else 22
        box = [cx - r, cy - r, cx + r, cy + r]
        for _ in range(8):
            hit = None
            for b in boxes:
                if box[2] >= b[0] and b[2] >= box[0] and box[3] >= b[1] and b[3] >= box[1]:
                    hit = b
                    break
            if not hit:
                break
            box[1] -= 22
            box[3] -= 22
            cy -= 22
        draw.ellipse(box, fill=(0, 0, 0), outline=rgb, width=2)
        tb = draw.textbbox((0, 0), text, font=font_label)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        draw.text((cx - tw / 2 - tb[0], cy - th / 2 - tb[1]), text, fill=rgb, font=font_label)
        boxes.append(box)
        lab["label_px"] = [cx, cy]

    thumb = args.thumb
    thumbs = {}
    for cat in THUMB_CATS:
        hs = [r["handle"] for r in by_cat.get(cat, [])]
        print("  thumb", cat, len(hs))
        tgray = render_ents(src, vp, "#707070", thumb, thumb, all_h, "ignore")
        thumbs[cat] = dark_composite(tgray, render_ents(src, vp, COLORS[cat], thumb, thumb, hs, "ignore")) if hs else tgray

    pad = 16
    title_bar = 110
    left_w = content
    right_w = thumb * 2 + pad * 3
    sheet_w = pad + left_w + pad + right_w + pad
    right_h = thumb * 4 + pad * 5 + 28 * 4
    sheet_h = title_bar + pad + max(content, right_h) + pad
    sheet = Image.new("RGB", (sheet_w, sheet_h), (0, 0, 0))
    draw = ImageDraw.Draw(sheet)
    font_title = ImageFont.truetype(args.font, 26)
    font_sub = ImageFont.truetype(args.font, 16)
    font_small = ImageFont.truetype(args.font, 15)
    font_cap = ImageFont.truetype(args.font, 14)

    legend = (
        "浅灰=墙体  琥珀=窗  绿=门  洋红=柜体  青=洁具  橙=电器  紫=家具  金=Q待确认  中灰=其他/未归类  黄圈=编号"
    )
    counts = (
        f"墙体_保留 {len(by_cat['墙体_保留'])}  窗 {len(by_cat['窗'])}  门 {len(by_cat['门'])}  "
        f"柜体 {len(by_cat['柜体'])}  洁具 {len(by_cat['洁具'])}  电器 {len(by_cat['电器'])}  "
        f"家具 {len(by_cat['家具'])}  灯具 {len(by_cat['灯具'])}  "
        f"其他 {len(by_cat['其他'])}  Q {len(questions)}项/{len(by_cat['question'])}实体  "
        f"已排除尺寸标注 {sum(1 for r in recs if r.get('status')=='exclude')}"
    )
    draw.text((pad, 12), args.title, fill=(255, 255, 255), font=font_title)
    draw.text((pad, 48), legend, fill=(200, 200, 200), font=font_sub)
    draw.text((pad, 74), counts, fill=(180, 180, 180), font=font_small)
    draw.line((0, title_bar - 1, sheet_w, title_bar - 1), fill=(60, 60, 60), width=1)

    ox, oy = pad, title_bar + pad
    sheet.paste(overview, (ox, oy))
    draw.rectangle((ox - 1, oy - 1, ox + content, oy + content), outline=(50, 50, 50))

    tx = ox + content + pad
    ty = title_bar + pad
    for i, cat in enumerate(THUMB_CATS):
        col, row = i % 2, i // 2
        x = tx + col * (thumb + pad)
        y = ty + row * (thumb + 28 + pad)
        cap = f"{cat}  n={len(by_cat.get(cat, []))}  {COLORS[cat]}"
        rgb = tuple(int(COLORS[cat][j:j + 2], 16) for j in (1, 3, 5))
        draw.text((x, y), cap, fill=rgb, font=font_cap)
        sheet.paste(thumbs[cat], (x, y + 22))
        draw.rectangle((x - 1, y + 21, x + thumb, y + 22 + thumb), outline=(50, 50, 50))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    print("saved", out, sheet.size)
    if args.labels_out:
        Path(args.labels_out).write_text(json.dumps(labels, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
