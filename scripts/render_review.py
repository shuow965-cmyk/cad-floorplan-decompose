#!/usr/bin/env python3
"""Render a category review PNG: full-plan gray underlay, highlight one class, yellow labels.

Example:
  python render_review.py --source src.dxf --viewport xmin,ymin,xmax,ymax \\
      --font NotoSansCJKSC.otf --highlight-handles furn_handles.txt --highlight-color '#B266FF' \\
      --title '家具 第1版' --legend '灰=底图  紫=家具  黄圈=F编号' --out furn.png \\
      --labels labels.json
"""
from __future__ import annotations

import argparse, json
from pathlib import Path

import cairosvg
import ezdxf
import numpy as np
from ezdxf.addons.drawing import Frontend, RenderContext, layout, config
from ezdxf.addons.drawing.svg import SVGBackend
from ezdxf.math import BoundingBox2d
from PIL import Image, ImageDraw, ImageFont


def load_handles(s: str) -> list[str]:
    p = Path(s)
    if p.exists():
        text = p.read_text().strip()
        if text.startswith("{") or text.startswith("["):
            data = json.loads(text)
            if isinstance(data, list):
                out = []
                for x in data:
                    if isinstance(x, str):
                        out.append(x)
                    elif isinstance(x, dict) and "handles" in x:
                        out.extend(x["handles"])
                return [h.upper() for h in out]
            # dict of id -> handles or {"handles":[]}
            acc = []
            def walk(o):
                if isinstance(o, dict):
                    if "handles" in o:
                        acc.extend(o["handles"])
                    else:
                        for v in o.values():
                            walk(v)
                elif isinstance(o, list):
                    for v in o:
                        walk(v)
            walk(data)
            return [str(h).upper() for h in acc]
        return [ln.strip().upper() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    return [h.strip().upper() for h in s.split(",") if h.strip()]


def render_ents(src_path, viewport, color, width, height, handles=None, hatch="ignore"):
    document = ezdxf.readfile(src_path)
    if handles is not None:
        want = set(h.upper() for h in handles)
        for e in list(document.modelspace()):
            if e.dxf.handle.upper() not in want:
                try:
                    document.modelspace().unlink_entity(e)
                except Exception:
                    try:
                        document.modelspace().delete_entity(e)
                    except Exception:
                        pass
    hatch_pol = config.HatchPolicy.IGNORE if hatch == "ignore" else config.HatchPolicy.NORMAL
    cfg = config.Configuration(
        background_policy=config.BackgroundPolicy.BLACK,
        color_policy=config.ColorPolicy.CUSTOM,
        custom_fg_color=color,
        lineweight_policy=config.LineweightPolicy.RELATIVE_FIXED,
        lineweight_scaling=1.0,
        hatch_policy=hatch_pol,
        hatching_timeout=10.0,
    )
    backend = SVGBackend()
    ctx = RenderContext(document)
    try:
        ctx.set_current_layout(document.modelspace())
    except Exception:
        pass
    Frontend(ctx, backend, config=cfg).draw_layout(document.modelspace(), finalize=True)
    x0, y0, x1, y1 = viewport
    svg = backend.get_string(
        layout.Page(200, 200),
        settings=layout.Settings(fixed_stroke_width=0.7, crop_at_margins=True),
        render_box=BoundingBox2d([(x0, y0), (x1, y1)]),
    )
    png = cairosvg.svg2png(bytestring=svg.encode(), output_width=width, output_height=height)
    import io
    return Image.open(io.BytesIO(png)).convert("RGB")


def to_px(wx, wy, viewport, content, title):
    x0, y0, x1, y1 = viewport
    s = min(content / (x1 - x0), content / (y1 - y0))
    off_x = (content - (x1 - x0) * s) / 2
    off_y = (content - (y1 - y0) * s) / 2
    return off_x + (wx - x0) * s, title + off_y + (y1 - wy) * s


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Render a category review PNG")
    p.add_argument("--source", required=True)
    p.add_argument("--viewport", required=True, help="xmin,ymin,xmax,ymax")
    p.add_argument("--font", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--highlight-handles", required=True)
    p.add_argument("--highlight-color", default="#B266FF")
    p.add_argument("--gray", default="#707070")
    p.add_argument("--underlay-png", default="", help="optional pre-rendered gray content PNG")
    p.add_argument("--title", default="")
    p.add_argument("--legend", default="")
    p.add_argument("--labels", default="", help="JSON list of {id, wx, wy} or {id, label_px}")
    p.add_argument("--label-color", default="#FFE000")
    p.add_argument("--label-radius", type=int, default=16)
    p.add_argument("--content", type=int, default=1800)
    p.add_argument("--title-height", type=int, default=100)
    p.add_argument("--hatch", choices=["ignore", "normal"], default="ignore")
    p.add_argument("--q-handles", default="", help="optional uncertain handles, drawn gold")
    p.add_argument("--q-color", default="#FFD000")
    args = p.parse_args(argv)

    vp = tuple(float(x) for x in args.viewport.split(","))
    assert len(vp) == 4
    content, title_h = args.content, args.title_height
    hl = load_handles(args.highlight_handles)
    qh = load_handles(args.q_handles) if args.q_handles else []

    if args.underlay_png:
        gray = Image.open(args.underlay_png).convert("RGB")
        if gray.size != (content, content):
            gray = gray.resize((content, content))
    else:
        # all modelspace ents as gray (caller should pass a cropped/source DXF)
        src = ezdxf.readfile(args.source)
        all_h = [e.dxf.handle for e in src.modelspace()]
        gray = render_ents(args.source, vp, args.gray, content, content, all_h, args.hatch)

    hi = render_ents(args.source, vp, args.highlight_color, content, content, hl, args.hatch)
    g, h = np.array(gray), np.array(hi)
    mask = h.max(axis=2) > 25
    g[mask] = h[mask]
    if qh:
        gold = render_ents(args.source, vp, args.q_color, content, content, qh, args.hatch)
        q = np.array(gold)
        g[q.max(axis=2) > 25] = q[q.max(axis=2) > 25]

    canvas = Image.new("RGB", (content, content + title_h), (0, 0, 0))
    canvas.paste(Image.fromarray(g), (0, title_h))
    draw = ImageDraw.Draw(canvas)
    font_title = ImageFont.truetype(args.font, 20)
    font_legend = ImageFont.truetype(args.font, 15)
    font_label = ImageFont.truetype(args.font, 16)
    if args.title:
        draw.text((16, 10), args.title, fill=(255, 255, 255), font=font_title)
    if args.legend:
        draw.text((16, 38), args.legend, fill=(200, 200, 200), font=font_legend)
    draw.line((0, title_h - 1, content, title_h - 1), fill=(60, 60, 60), width=1)

    if args.labels:
        labels = json.loads(Path(args.labels).read_text())
        rgb = tuple(int(args.label_color[i:i + 2], 16) for i in (1, 3, 5))
        r = args.label_radius
        boxes = []
        for lab in labels:
            if "label_px" in lab:
                cx, cy = lab["label_px"]
            else:
                cx, cy = to_px(lab["wx"], lab["wy"], vp, content, title_h)
            box = (cx - r, cy - r, cx + r, cy + r)
            for b in boxes:
                if box[2] >= b[0] and b[2] >= box[0] and box[3] >= b[1] and b[3] >= box[1]:
                    print("LABEL OVERLAP", lab.get("id"), box)
            draw.ellipse(box, fill=(0, 0, 0), outline=rgb, width=2)
            text = str(lab.get("id") or lab.get("text") or "")
            tb = draw.textbbox((0, 0), text, font=font_label)
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
            draw.text((cx - tw / 2 - tb[0], cy - th / 2 - tb[1]), text, fill=rgb, font=font_label)
            boxes.append(box)
    canvas.save(args.out)
    print("saved", args.out, canvas.size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
