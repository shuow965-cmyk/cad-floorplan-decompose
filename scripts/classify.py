#!/usr/bin/env python3
"""First-pass classify visible DXF entities by rules.yaml, with generic shape detection.

  python classify.py --source src.dxf --rules rules.yaml --viewport xmin,ymin,xmax,ymax \\
      --out-dir out/ [--gold gold.json] [--q-crops] [--font Font.otf]

Outputs in --out-dir:
  classification.json, questions.json, unclassified.json, summary.txt, units.json
  source-mm.dxf (only if the source is not already millimetres; source file is never modified)
  report.txt (if --gold), q-sheet.png / q-sheet-02.png (if --q-crops)
  --viewport is in source-file units and is scaled together with the drawing.
"""
from __future__ import annotations

import argparse, io, json, math, sys
from collections import Counter, defaultdict
from pathlib import Path

import ezdxf
from ezdxf import bbox as ebb
from ezdxf import transform as eztransform

try:
    import yaml
except ImportError:
    yaml = None


GEOM = {
    "LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE",
    "HATCH", "INSERT", "TEXT", "MTEXT", "SOLID", "POINT", "MLINE", "DIMENSION",
}


def load_rules(path: Path) -> dict:
    text = path.read_text()
    if yaml is not None:
        return yaml.safe_load(text)
    raise SystemExit("PyYAML is required to read rules.yaml (pip install pyyaml)")


# $INSUNITS → 毫米。未列出的编码不自动换算，见 guess_unit_scale。
INSUNITS_TO_MM = {
    1: (25.4, "英寸"),
    2: (304.8, "英尺"),
    4: (1.0, "毫米"),
    5: (10.0, "厘米"),
    6: (1000.0, "米"),
}


def _wallish_layer(name: str) -> bool:
    L = (name or "").casefold()
    return any(s in L for s in ("墙", "wall", "i-110"))


def _thickness_samples(doc, limit=800):
    """Collect plausible wall-thickness samples (short sides / parallel gaps)."""
    samples = []
    horiz, vert = [], []
    for e in doc.modelspace():
        typ = e.dxftype()
        layer = e.dxf.layer if e.dxf.hasattr("layer") else ""
        if typ in ("LWPOLYLINE", "POLYLINE") and _wallish_layer(layer):
            try:
                bb = ebb.extents([e], fast=True)
                if bb and bb.has_data:
                    w = abs(bb.extmax.x - bb.extmin.x)
                    h = abs(bb.extmax.y - bb.extmin.y)
                    short, long_ = min(w, h), max(w, h)
                    if short > 0 and long_ >= short * 1.5:
                        samples.append(short)
            except Exception:
                pass
        if typ == "LINE" and _wallish_layer(layer):
            try:
                x0, y0 = float(e.dxf.start.x), float(e.dxf.start.y)
                x1, y1 = float(e.dxf.end.x), float(e.dxf.end.y)
                dx, dy = abs(x1 - x0), abs(y1 - y0)
                if dx >= dy * 8:  # horizontal
                    horiz.append((min(x0, x1), max(x0, x1), (y0 + y1) / 2))
                elif dy >= dx * 8:  # vertical
                    vert.append((min(y0, y1), max(y0, y1), (x0 + x1) / 2))
            except Exception:
                pass
        if len(samples) >= limit:
            break

    def gaps(segs, cap=200):
        segs = sorted(segs, key=lambda s: s[2])[:400]
        out = []
        for i, (a0, a1, c) in enumerate(segs):
            for b0, b1, d in segs[i + 1:i + 8]:
                overlap = min(a1, b1) - max(a0, b0)
                span = max(a1 - a0, b1 - b0)
                if span <= 0 or overlap < span * 0.4:
                    continue
                g = abs(d - c)
                if g > 0:
                    out.append(g)
                    if len(out) >= cap:
                        return out
        return out

    samples.extend(gaps(horiz))
    samples.extend(gaps(vert))
    return samples


def _model_width(doc) -> float:
    try:
        extmin, extmax = doc.header.get("$EXTMIN"), doc.header.get("$EXTMAX")
        if extmin is not None and extmax is not None:
            w = abs(float(extmax[0]) - float(extmin[0]))
            h = abs(float(extmax[1]) - float(extmin[1]))
            m = max(w, h)
            if 1e-6 < m < 1e12:
                return m
    except Exception:
        pass
    xs, ys = [], []
    for e in list(doc.modelspace())[:4000]:
        try:
            bb = ebb.extents([e], fast=True)
            if bb and bb.has_data:
                xs += [bb.extmin.x, bb.extmax.x]
                ys += [bb.extmin.y, bb.extmax.y]
        except Exception:
            pass
    if not xs:
        return 0.0
    return max(max(xs) - min(xs), max(ys) - min(ys))


def guess_unit_scale(doc) -> tuple[float, str, str]:
    """When $INSUNITS is 0/unset, guess from wall-thickness distribution and extents."""
    width = _model_width(doc)
    samples = _thickness_samples(doc)
    samples = [s for s in samples if s > 1e-9]
    samples.sort()
    med = samples[len(samples) // 2] if samples else None
    measurement = doc.header.get("$MEASUREMENT")
    metric = (measurement == 1)

    def pick(factor, name, why):
        return factor, name, why

    # Prefer thickness if we have enough samples
    if med is not None and len(samples) >= 8:
        if 80 <= med <= 450 and 3000 <= width <= 200000:
            return pick(1.0, "毫米（推断）", f"墙厚中位数 {med:.1f}、图幅 {width:.0f}，按毫米")
        if 3 <= med <= 16 and 80 <= width <= 4000:
            return pick(25.4, "英寸（推断）", f"墙厚中位数 {med:.1f}、图幅 {width:.0f}，按英寸×25.4")
        if 8 <= med <= 50 and 200 <= width <= 8000 and metric:
            return pick(10.0, "厘米（推断）", f"墙厚中位数 {med:.1f}、图幅 {width:.0f}，测量制为公制，按厘米×10")
        if 0.08 <= med <= 0.5 and 4 <= width <= 80:
            return pick(1000.0, "米（推断）", f"墙厚中位数 {med:.4f}、图幅 {width:.2f}，按米×1000")
        if 0.3 <= med <= 2.0 and 15 <= width <= 250 and not metric:
            return pick(304.8, "英尺（推断）", f"墙厚中位数 {med:.2f}、图幅 {width:.1f}，按英尺×304.8")

    # Extent-only fallback
    if 4000 <= width <= 200000:
        return pick(1.0, "毫米（推断）", f"图幅 {width:.0f} 落在常见毫米住宅范围；墙厚样本不足或含糊，按毫米不缩放")
    if 80 <= width <= 2500 and not metric:
        return pick(25.4, "英寸（推断）", f"图幅 {width:.0f} 更像英寸平面，按英寸×25.4")
    if 80 <= width <= 2500 and metric:
        return pick(10.0, "厘米（推断）", f"图幅 {width:.0f} 且测量制为公制，按厘米×10")
    if 4 <= width <= 80:
        return pick(1000.0, "米（推断）", f"图幅 {width:.2f} 更像米制平面，按米×1000")
    return pick(1.0, "毫米（默认）", f"无法从墙厚/图幅可靠判断（宽 {width:.3f}，样本 {len(samples)}），按毫米不缩放")


def detect_unit_scale(doc) -> dict:
    raw = doc.header.get("$INSUNITS")
    try:
        code = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        code = 0
    if code in INSUNITS_TO_MM:
        factor, name = INSUNITS_TO_MM[code]
        return {
            "insunits": code, "unit_name": name, "scale_to_mm": factor,
            "guessed": False,
            "note": f"$INSUNITS={code}（{name}），换算系数 {factor}",
        }
    factor, name, why = guess_unit_scale(doc)
    return {
        "insunits": code, "unit_name": name, "scale_to_mm": factor,
        "guessed": True,
        "note": f"$INSUNITS={code or '未设'}，{why}",
    }


def prepare_source_mm(src_path: str, viewport, out_dir: Path) -> tuple[str, tuple | None, dict]:
    """If the source is not millimetres, write a scaled copy under out_dir. Never overwrite the source."""
    src = Path(src_path).resolve()
    doc = ezdxf.readfile(str(src))
    info = detect_unit_scale(doc)
    info["source"] = str(src)
    factor = float(info["scale_to_mm"])
    vp_src = list(viewport) if viewport is not None else None
    info["viewport_source"] = vp_src
    if abs(factor - 1.0) < 1e-12:
        info["working_source"] = str(src)
        info["viewport_mm"] = vp_src
        return str(src), viewport, info

    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / "source-mm.dxf"
    # scale a freshly loaded doc; do not save back to src
    log = eztransform.scale_uniform(list(doc.modelspace()), factor)
    n_err = 0
    try:
        n_err = sum(1 for _ in log)
    except Exception:
        pass
    try:
        doc.header["$INSUNITS"] = 4
    except Exception:
        pass
    doc.saveas(str(dst))
    vp_mm = None
    if viewport is not None:
        vp_mm = tuple(c * factor for c in viewport)
    info["working_source"] = str(dst)
    info["viewport_mm"] = list(vp_mm) if vp_mm is not None else None
    info["note"] = info["note"] + f"；已写工作副本 {dst}（未改源文件）"
    if n_err:
        info["note"] += f"；变换时有 {n_err} 条日志"
    return str(dst), vp_mm, info


def rank_insert_candidates(f: dict, ruleset: dict, max_n: int = 2) -> list[str]:
    """Score an unknown INSERT against generic shape profiles; keep top max_n categories."""
    th = (ruleset.get("shape") or {}).get("insert") or {}
    table = (ruleset.get("shape") or {}).get("table") or {}
    cab = (ruleset.get("shape") or {}).get("cabinet") or {}
    ins = f.get("insert") or {}
    short = f.get("thickness") or 0
    long_ = f.get("extent") or 0
    aspect = f.get("aspect") or (long_ / short if short else 0)
    n = ins.get("n_ents") or 0
    n_arc = ins.get("n_arc") or 0
    n_cir = ins.get("n_circle") or 0
    n_hatch = ins.get("n_hatch") or 0
    n_large = ins.get("n_large_cir") or 0
    n_tiny = ins.get("n_tiny_cir") or 0
    scores = {k: 0.0 for k in ("家具", "柜体", "洁具", "电器", "门", "灯具", "窗")}

    # windows: longish rectangle, wall-ish thickness
    if 600 <= long_ <= 2800 and 80 <= short <= 900 and aspect >= 1.6:
        scores["窗"] += 2.5 + min(aspect, 6) * 0.2
    # doors: swing roughly square
    if 600 <= short <= 1300 and 600 <= long_ <= 1300 and aspect <= 1.35:
        scores["门"] += 2.0
    # furniture: midsize / table / bed / sofa / chair
    ts, tl = table.get("short") or [700, 1200], table.get("long") or [1400, 2400]
    if ts[0] <= short <= ts[1] and tl[0] <= long_ <= tl[1]:
        scores["家具"] += 3.0
    if 800 <= long_ <= 2500 and 400 <= short <= 2000 and n >= 4:
        scores["家具"] += 1.8
    if (th.get("bed_min", 1500) <= short <= th.get("bed_max", 2300)
            and th.get("bed_min", 1500) <= long_ <= th.get("bed_max", 2300) and n >= 10):
        scores["家具"] += 3.5
    if (th.get("sofa_min_long", 2400) <= long_ <= th.get("sofa_max_long", 5000)
            and n_arc >= 10):
        scores["家具"] += 2.5
    if (th.get("chair_min", 400) <= short <= th.get("chair_max", 1100)
            and aspect <= 1.3 and n_arc >= 4):
        scores["家具"] += 2.0
    # cabinet: one side in cabinet depth
    dlo, dhi = (cab.get("depth") or [300, 650])
    if dlo <= short <= dhi and long_ >= 700:
        scores["柜体"] += 2.8
    # sanitary
    if (350 <= short <= 900 and 350 <= long_ <= 1800 and n_arc >= 6 and n_hatch == 0):
        scores["洁具"] += 2.2
    if n_tiny >= 15 and long_ <= 600:
        scores["洁具"] += 3.0
    # appliances: fridge / cooktop
    if (th.get("fridge_min_short", 500) <= short <= th.get("fridge_max_short", 750)
            and th.get("fridge_min_long", 1500) <= long_ <= th.get("fridge_max_long", 2100)):
        scores["电器"] += 3.0 + (1.0 if n_large else 0)
    cs, cl = th.get("cooktop_short") or [300, 550], th.get("cooktop_long") or [550, 900]
    if cs[0] <= short <= cs[1] and cl[0] <= long_ <= cl[1]:
        scores["电器"] += 2.5 + (1.0 if n_hatch else 0) + (0.5 if n_arc >= 8 else 0)
    # lights: thin bar or tiny footprint
    if short <= 80 and long_ >= 800:
        scores["灯具"] += 2.5
    if long_ <= 250 and n_cir >= 1:
        scores["灯具"] += 1.2

    ranked = sorted(((s, k) for k, s in scores.items() if s > 0), reverse=True)
    if ranked:
        return [k for s, k in ranked[:max_n]]
    return ["家具", "柜体"][:max_n]


def refine_unknown_inserts(rows, ruleset):
    for r in rows:
        if r.get("status") != "question":
            continue
        f = r.get("entity") or {}
        if f.get("type") != "INSERT":
            continue
        rule = r.get("rule") or ""
        if "unknown-insert" in rule or len(r.get("candidates") or []) > 2:
            r["candidates"] = rank_insert_candidates(f, ruleset, 2)


def is_invisible(e) -> bool:
    if int(getattr(e.dxf, "invisible", 0) or 0):
        return True
    if str(e.dxf.layer).lower() == "defpoints":
        return True
    try:
        layer = e.doc.layers.get(e.dxf.layer)
        if layer.is_off() or layer.is_frozen():
            return True
    except Exception:
        pass
    return False


def entity_bbox(e):
    try:
        bb = ebb.extents([e], fast=True)
        if bb and bb.has_data:
            return (float(bb.extmin.x), float(bb.extmin.y),
                    float(bb.extmax.x), float(bb.extmax.y))
    except Exception:
        pass
    return None


def in_viewport(bb, vp) -> bool:
    if vp is None or bb is None:
        return True
    cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
    return vp[0] <= cx <= vp[2] and vp[1] <= cy <= vp[3]


def center(bb):
    return ((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2)


def contained(inner, outer, m=0) -> bool:
    return (outer[0] - m <= inner[0] and outer[1] - m <= inner[1]
            and outer[2] + m >= inner[2] and outer[3] + m >= inner[3])


def inflate(bb, m):
    return (bb[0] - m, bb[1] - m, bb[2] + m, bb[3] + m)


def dist2(a, b):
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def insert_stats(e) -> dict:
    n_line = n_arc = n_circle = n_ellipse = n_spline = n_hatch = n_lw = n_ents = 0
    n_tiny = n_large = 0
    try:
        for ve in e.virtual_entities():
            n_ents += 1
            typ = ve.dxftype()
            if typ == "LINE":
                n_line += 1
            elif typ == "ARC":
                n_arc += 1
            elif typ == "CIRCLE":
                n_circle += 1
                r = float(ve.dxf.radius) if ve.dxf.hasattr("radius") else 0
                if r <= 8:
                    n_tiny += 1
                if r >= 80:
                    n_large += 1
            elif typ == "ELLIPSE":
                n_ellipse += 1
            elif typ == "SPLINE":
                n_spline += 1
            elif typ == "HATCH":
                n_hatch += 1
            elif typ in ("LWPOLYLINE", "POLYLINE"):
                n_lw += 1
    except Exception:
        pass
    return {
        "n_ents": n_ents, "n_line": n_line, "n_arc": n_arc, "n_circle": n_circle,
        "n_ellipse": n_ellipse, "n_spline": n_spline, "n_hatch": n_hatch, "n_lw": n_lw,
        "n_tiny_cir": n_tiny, "n_large_cir": n_large,
        "spline_frac": (n_spline / n_ents) if n_ents else 0.0,
    }


def feats(e) -> dict:
    bb = entity_bbox(e)
    w = h = area = extent = thick = aspect = None
    if bb:
        w, h = abs(bb[2] - bb[0]), abs(bb[3] - bb[1])
        area = w * h
        extent = max(w, h)
        thick = min(w, h)
        aspect = (extent / thick) if thick else None
    typ = e.dxftype()
    closed = bool(getattr(e, "closed", False)) if typ in ("LWPOLYLINE", "POLYLINE") else typ in ("CIRCLE", "ELLIPSE")
    radius = float(e.dxf.radius) if typ == "CIRCLE" and e.dxf.hasattr("radius") else None
    block = e.dxf.name if typ == "INSERT" else ""
    color = int(getattr(e.dxf, "color", 256) or 256)
    n_bulge = npts = 0
    pts = []
    if typ == "LWPOLYLINE":
        try:
            raw = list(e.get_points("xyb"))
            npts = len(raw)
            n_bulge = sum(1 for p in raw if abs(p[2] or 0) > 1e-6)
            pts = [(p[0], p[1]) for p in raw]
        except Exception:
            pass
    elif typ == "LINE":
        try:
            pts = [(e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)]
            npts = 2
        except Exception:
            pass
    elif typ == "ARC":
        try:
            c, r = e.dxf.center, float(e.dxf.radius)
            a0, a1 = math.radians(e.dxf.start_angle), math.radians(e.dxf.end_angle)
            pts = [(c.x + r * math.cos(a0), c.y + r * math.sin(a0)),
                   (c.x + r * math.cos(a1), c.y + r * math.sin(a1)), (c.x, c.y)]
        except Exception:
            pass
    cx = cy = None
    if typ in ("CIRCLE", "ELLIPSE") and e.dxf.hasattr("center"):
        cx, cy = float(e.dxf.center.x), float(e.dxf.center.y)
    elif bb:
        cx, cy = center(bb)
    ell = None
    if typ == "ELLIPSE":
        try:
            mj = e.dxf.major_axis
            maj = math.hypot(mj.x, mj.y) * 2
            ell = (maj, maj * float(e.dxf.ratio))
        except Exception:
            pass
    ins = insert_stats(e) if typ == "INSERT" else None
    return {
        "handle": e.dxf.handle.upper(),
        "type": typ,
        "layer": e.dxf.layer,
        "linetype": str(e.dxf.linetype or "BYLAYER"),
        "color": color,
        "block": block,
        "closed": closed,
        "bb": bb,
        "w": w, "h": h, "area": area, "extent": extent, "thickness": thick,
        "aspect": aspect, "radius": radius, "ell": ell, "cx": cx, "cy": cy,
        "n_bulge": n_bulge, "npts": npts, "pts": pts, "insert": ins,
    }


def layer_hit(layer: str, aliases: dict, key: str) -> bool:
    keys = aliases.get(key) or [key]
    L = layer.casefold()
    return any(str(s).casefold() in L for s in keys)


def block_hit(name: str, keywords: dict, key: str) -> bool:
    words = keywords.get(key) or []
    n = (name or "").casefold()
    return any(str(s).casefold() in n for s in words)


def match_rule(f: dict, rule: dict, ruleset: dict) -> bool:
    m = rule.get("match") or {}
    if m.get("learned_block"):
        return False
    if types := m.get("types"):
        if f["type"] not in types:
            return False
    if not_types := m.get("not_types"):
        if f["type"] in not_types:
            return False
    aliases = ruleset.get("layer_aliases") or {}
    if key := m.get("layer_of"):
        if not layer_hit(f["layer"], aliases, key):
            return False
    if layers := m.get("layers"):
        L = f["layer"].casefold()
        if not any(str(s).casefold() in L for s in layers):
            return False
    if not_layers := m.get("not_layers"):
        L = f["layer"].casefold()
        if any(str(s).casefold() in L for s in not_layers):
            return False
    if ltc := m.get("linetype_contains"):
        lt = f["linetype"].casefold()
        if not any(str(s).casefold() in lt for s in ltc):
            return False
    if nlt := m.get("not_linetype_contains"):
        lt = f["linetype"].casefold()
        if any(str(s).casefold() in lt for s in nlt):
            return False
    if colors := m.get("colors"):
        if f["color"] not in colors:
            return False
    if key := m.get("block_keywords_of"):
        if f["type"] != "INSERT" or not block_hit(f["block"], ruleset.get("block_keywords") or {}, key):
            return False
    if "closed" in m and m["closed"] is not None:
        if f["closed"] != bool(m["closed"]):
            return False
    def rng(val, lo, hi):
        if val is None:
            return False
        if lo is not None and val < lo:
            return False
        if hi is not None and val > hi:
            return False
        return True
    if "min_thickness" in m or "max_thickness" in m:
        if not rng(f["thickness"], m.get("min_thickness"), m.get("max_thickness")):
            return False
    if "min_area" in m or "max_area" in m:
        if not rng(f["area"], m.get("min_area"), m.get("max_area")):
            return False
    if "min_extent" in m or "max_extent" in m:
        if not rng(f["extent"], m.get("min_extent"), m.get("max_extent")):
            return False
    if "circle_r_min" in m or "circle_r_max" in m:
        if f["type"] != "CIRCLE" or not rng(f["radius"], m.get("circle_r_min"), m.get("circle_r_max")):
            return False
    return True


def learned_block_category(f: dict, ruleset: dict):
    if f["type"] != "INSERT":
        return None
    name = (f["block"] or "").casefold()
    for rec in (ruleset.get("learned") or {}).get("blocks") or []:
        if str(rec.get("name") or "").casefold() == name:
            return rec.get("category"), rec.get("note") or "learned block"
    return None


def classify_one(f: dict, ruleset: dict) -> dict:
    for rec in (ruleset.get("learned") or {}).get("exceptions") or []:
        if str(rec.get("handle") or "").upper() == f["handle"]:
            cat = rec.get("category") or rec.get("action")
            if cat == "exclude" or rec.get("action") == "exclude":
                return {"status": "exclude", "category": None, "rule": "learned-exception",
                        "confidence": "high", "candidates": [], "reason": rec.get("note") or "learned exception"}
            return {"status": "classified", "category": cat, "rule": "learned-exception",
                    "confidence": "high", "candidates": [cat], "reason": rec.get("note") or "learned exception"}

    high, medium = [], []
    lb = learned_block_category(f, ruleset)
    if lb:
        high.append((lb[0], {"id": "learned-block", "confidence": "high", "reason": lb[1], "action": None, "category": lb[0]}))

    for rule in ruleset.get("rules") or []:
        if (rule.get("match") or {}).get("learned_block"):
            continue
        if not match_rule(f, rule, ruleset):
            continue
        conf = rule.get("confidence") or "medium"
        action = rule.get("action")
        cat = rule.get("category")
        item = (action or cat, rule)
        if conf == "high":
            high.append(item)
        else:
            medium.append(item)

    def pack(item, status, cands=None):
        key, rule = item
        action = rule.get("action")
        cat = None if action in ("exclude", "question") else (rule.get("category") or (key if key not in ("exclude", "question") else None))
        if action == "exclude" or key == "exclude":
            status = "exclude"
            cat = None
        if action == "question":
            status = "question"
            cands = rule.get("candidates") or cands or []
        return {
            "status": status,
            "category": cat,
            "rule": rule.get("id"),
            "confidence": rule.get("confidence"),
            "candidates": cands or ([cat] if cat else rule.get("candidates") or []),
            "reason": rule.get("reason") or "",
        }

    if high:
        cats = []
        for key, rule in high:
            if rule.get("action") == "exclude" or key == "exclude":
                return pack((key, rule), "exclude")
            if rule.get("action") == "question":
                cats.append(("__Q__", rule))
            else:
                cats.append((rule.get("category") or key, rule))
        uniq = {c for c, _ in cats if c != "__Q__"}
        if len(uniq) > 1:
            return {
                "status": "question",
                "category": None,
                "rule": ",".join(r.get("id") for _, r in cats),
                "confidence": "conflict",
                "candidates": sorted(uniq),
                "reason": "多条高置信规则指向不同类别",
            }
        if uniq:
            cat = next(iter(uniq))
            rule = next(r for c, r in cats if c == cat)
            return pack((cat, rule), "classified", [cat])
        return pack(cats[0], "question")

    if medium:
        classed = [(rule.get("category") or key, rule) for key, rule in medium
                   if rule.get("action") not in ("question", "exclude")]
        qs = [(key, rule) for key, rule in medium if rule.get("action") == "question"]
        uniq = {c for c, _ in classed}
        if len(uniq) == 1 and not qs:
            cat, rule = classed[0]
            return pack((cat, rule), "classified", [cat])
        cands = sorted(uniq | set().union(*[set(r.get("candidates") or []) for _, r in qs]))
        if not cands:
            cands = sorted(uniq)
        if len(medium) == 1:
            reason = medium[0][1].get("reason") or "需要确认"
        elif qs and not classed:
            reason = qs[0][1].get("reason") or "无法从规则唯一判定"
        else:
            reason = "中等置信规则指向多个类别，需确认"
        return {
            "status": "question",
            "category": None,
            "rule": ",".join(r.get("id") for _, r in medium),
            "confidence": "medium",
            "candidates": cands,
            "reason": reason,
        }

    return {
        "status": "unclassified",
        "category": "其他",
        "rule": None,
        "confidence": None,
        "candidates": ["其他"],
        "reason": "无规则命中，列入未归类",
    }


def _set_cls(r, cat, conf, rule, reason, action=None, cands=None):
    if action == "exclude" or cat == "排除":
        r.update(status="exclude", category=None, rule=rule, confidence=conf,
                 candidates=[], reason=reason)
        return
    if conf == "high":
        r.update(status="classified", category=cat, rule=rule, confidence="high",
                 candidates=[cat], reason=reason)
    else:
        r.update(status="question", category=None, rule=rule, confidence="medium",
                 candidates=cands or [cat], reason=reason)


def _locked(r) -> bool:
    return r["status"] in ("classified", "exclude")


def _is_dashed(f) -> bool:
    lt = (f.get("linetype") or "").upper()
    return any(s in lt for s in ("ISO", "DASH", "HIDDEN"))


def apply_shapes(rows, ruleset):
    """Fill gaps with generic geometry. Never overrides already classified/excluded."""
    th = ruleset.get("shape") or {}
    aliases = ruleset.get("layer_aliases") or {}
    by_h = {r["entity"]["handle"]: r for r in rows}

    def furn(f):
        return layer_hit(f["layer"], aliases, "家具层")

    def wallish(f):
        return layer_hit(f["layer"], aliases, "墙体") or layer_hit(f["layer"], aliases, "管道")

    def ceil(f):
        return layer_hit(f["layer"], aliases, "天花")

    # ---- INSERT profiles ----
    ti = th.get("insert") or {}
    for r in rows:
        if _locked(r):
            continue
        f = r["entity"]
        if f["type"] != "INSERT":
            continue
        st = f.get("insert") or {}
        w, h = f["w"] or 0, f["h"] or 0
        short, long_ = min(w, h), max(w, h)
        aspect = (long_ / short) if short else 999
        n = st.get("n_ents") or 0
        n_arc = st.get("n_arc") or 0
        n_cir = st.get("n_circle") or 0
        n_spl = st.get("n_spline") or 0
        n_tiny = st.get("n_tiny_cir") or 0
        n_large = st.get("n_large_cir") or 0
        frac_spl = st.get("spline_frac") or 0.0

        if n_spl >= ti.get("person_min_splines", 10) and frac_spl >= ti.get("person_spline_frac", 0.55):
            _set_cls(r, "排除", "high", "shape-person", "块内以样条为主，多为人物轮廓", action="exclude")
            continue
        if n_tiny >= ti.get("shower_min_tiny_circles", 20) and long_ <= ti.get("shower_max_extent", 500):
            _set_cls(r, "洁具", "high", "shape-shower-spray", "块内大量微小圆点，常见花洒喷面")
            continue
        # 灶具/烟机常带填充或更多圆，须先于坐便器判定，避免把 600×400 级灶具认成马桶
        n_hatch = st.get("n_hatch") or 0
        cs = ti.get("cooktop_short") or [300, 550]
        cl = ti.get("cooktop_long") or [550, 900]
        if cs[0] <= short <= cs[1] and cl[0] <= long_ <= cl[1] and n_arc >= ti.get("cooktop_min_arcs", 20) and n_hatch >= 1:
            _set_cls(r, "电器", "medium", "shape-cooktop",
                     "尺度接近灶具/烟机，圆弧较多且含填充，请确认", cands=["电器", "家具"])
            continue
        if (ti.get("toilet_min_side", 350) <= short <= ti.get("toilet_max_side", 900)
                and ti.get("toilet_min_side", 350) <= long_ <= ti.get("toilet_max_side", 900)
                and ti.get("toilet_min_aspect", 1.25) <= aspect <= ti.get("toilet_max_aspect", 2.3)
                and n_arc >= ti.get("toilet_min_arcs", 8)
                and n_cir <= ti.get("toilet_max_circles", 8)
                and n_hatch == 0):
            _set_cls(r, "洁具", "high", "shape-toilet", "块外包接近坐便器，且含多段圆弧（盆+水箱）")
            continue
        if (aspect >= ti.get("curtain_min_aspect", 6)
                and short <= ti.get("curtain_max_thickness", 200)
                and long_ >= ti.get("curtain_min_length", 600)
                and n_arc >= ti.get("curtain_min_arcs", 8)):
            _set_cls(r, "家具", "high", "shape-curtain", "细长条且大量圆弧，常见窗帘褶皱")
            continue
        if (ti.get("bed_min", 1500) <= short <= ti.get("bed_max", 2300)
                and ti.get("bed_min", 1500) <= long_ <= ti.get("bed_max", 2300)
                and n >= ti.get("bed_min_ents", 20)):
            _set_cls(r, "家具", "high", "shape-bed", "块外包接近双人床尺寸，内部线条较多")
            continue
        if (ti.get("sofa_min_long", 2400) <= long_ <= ti.get("sofa_max_long", 5000)
                and ti.get("sofa_min_short", 700) <= short <= ti.get("sofa_max_short", 2200)
                and n_arc >= ti.get("sofa_min_arcs", 15)):
            _set_cls(r, "家具", "high", "shape-sofa", "长条外包且大量圆弧，常见沙发/组合沙发")
            continue
        if (ti.get("chair_min", 400) <= short <= ti.get("chair_max", 1100)
                and long_ <= ti.get("chair_max", 1100)
                and aspect <= ti.get("chair_max_aspect", 1.25)
                and n_arc >= ti.get("chair_min_arcs", 5)
                and n_large == 0):
            _set_cls(r, "家具", "high", "shape-chair", "近方形、多圆弧、无大圆，常见椅凳")
            continue
        if (ti.get("fridge_min_short", 500) <= short <= ti.get("fridge_max_short", 750)
                and ti.get("fridge_min_long", 1500) <= long_ <= ti.get("fridge_max_long", 2100)
                and n_large >= 1):
            _set_cls(r, "电器", "high", "shape-fridge", "平面尺寸接近冰箱，且块内有较大圆")
            continue
        if short <= 80 and long_ >= 800 and n_arc == 0 and n >= 4:
            _set_cls(r, "灯具", "medium", "shape-linear-bar",
                     "细长条且几乎无圆弧，可能是灯带或窗帘盒，请确认", cands=["灯具", "家具"])
            continue
        if 800 <= long_ <= 2500 and 400 <= short <= 2000 and n >= 4:
            _set_cls(r, "家具", "medium", "shape-insert-midsize",
                     "尺度像家具或台面，请确认", cands=["家具"])
            continue

    # ---- exploded / primitive geometry ----
    fs = [r["entity"] for r in rows]
    closed = [f for f in fs if f["type"] in ("LWPOLYLINE", "POLYLINE") and f.get("closed")
              and f.get("bb") and f.get("w")]
    circles = [f for f in fs if f["type"] == "CIRCLE" and f.get("radius") and f.get("cx") is not None]
    ellipses = [f for f in fs if f["type"] == "ELLIPSE" and f.get("ell") and f.get("cx") is not None]
    lines = [f for f in fs if f["type"] in ("LINE", "LWPOLYLINE") and f.get("bb")]
    arcs = [f for f in fs if f["type"] == "ARC" and f.get("bb")]

    # concentric drain pairs on wall/pipe
    dp = th.get("drain_pair") or {}
    wr, fr_ = dp.get("wall_r") or [30, 90], dp.get("furniture_r") or [15, 40]
    used_cir = set()
    for i, a in enumerate(circles):
        for b in circles[i + 1:]:
            if dist2((a["cx"], a["cy"]), (b["cx"], b["cy"])) > (min(a["radius"], b["radius"]) * 0.4 + 5) ** 2:
                continue
            ra, rb = sorted([a["radius"], b["radius"]])
            if wr[0] <= ra <= wr[1] and wr[0] <= rb <= wr[1] and (wallish(a) or wallish(b)):
                for f in (a, b):
                    r = by_h[f["handle"]]
                    if not _locked(r):
                        _set_cls(r, "洁具", "high", "shape-drain-pair",
                                 "墙/管道层上的同心圆，常见地漏或下水")
                used_cir.update((a["handle"], b["handle"]))
            elif fr_[0] <= ra <= fr_[1] and fr_[0] <= rb <= fr_[1] and (furn(a) or furn(b)):
                for f in (a, b):
                    r = by_h[f["handle"]]
                    if not _locked(r):
                        _set_cls(r, "洁具", "high", "shape-basin-drain",
                                 "家具层上的小同心圆，常见盆下水")
                used_cir.update((a["handle"], b["handle"]))

    # downlight pair on furniture layer + short cross lines through center
    dl = th.get("downlight_pair") or {}
    dlr, clen, ctol = dl.get("r") or [55, 160], dl.get("cross_len") or [150, 400], dl.get("center_tol", 40)
    for i, a in enumerate(circles):
        if not furn(a):
            continue
        for b in circles[i + 1:]:
            if not furn(b):
                continue
            if dist2((a["cx"], a["cy"]), (b["cx"], b["cy"])) > 25 ** 2:
                continue
            ra, rb = a["radius"], b["radius"]
            if not (dlr[0] <= ra <= dlr[1] and dlr[0] <= rb <= dlr[1]):
                continue
            # skip if already tagged sanitary (smaller drains)
            if a["handle"] in used_cir and min(ra, rb) < 50:
                continue
            cxy = ((a["cx"] + b["cx"]) / 2, (a["cy"] + b["cy"]) / 2)
            for f in (a, b):
                r = by_h[f["handle"]]
                if not _locked(r):
                    _set_cls(r, "灯具", "high", "shape-downlight-pair",
                             "家具层上较大的同心圆，常见筒灯符号")
            for ln in lines:
                if not furn(ln) or _locked(by_h[ln["handle"]]):
                    continue
                extent = ln.get("extent") or 0
                if not (clen[0] <= extent <= clen[1]):
                    continue
                # distance of segment midpoint to circle center
                if ln.get("cx") is None:
                    continue
                if math.hypot(ln["cx"] - cxy[0], ln["cy"] - cxy[1]) <= ctol:
                    _set_cls(by_h[ln["handle"]], "灯具", "high", "shape-downlight-cross",
                             "穿过筒灯圆心的短十字线")

    # rounded basins + faucets + small drains
    bs = th.get("basin") or {}
    bshort, blong = (bs.get("min_short", 300), bs.get("max_short", 700)), (bs.get("min_long", 300), bs.get("max_long", 900))
    fmaj, fmin = bs.get("faucet_major") or [15, 50], bs.get("faucet_minor") or [8, 25]
    fdist, drain_r, gmargin = bs.get("faucet_max_dist", 160), bs.get("drain_r") or [15, 40], bs.get("grow_margin", 50)
    basins = []
    for f in closed:
        if not furn(f):
            continue
        short, long_ = f["thickness"], f["extent"]
        if (f.get("n_bulge") or 0) < bs.get("min_bulges", 2):
            continue
        if not (bshort[0] <= short <= bshort[1] and blong[0] <= long_ <= blong[1]):
            continue
        basins.append(f)
        r = by_h[f["handle"]]
        if not _locked(r):
            _set_cls(r, "洁具", "high", "shape-basin",
                     "带圆角的闭合框，尺度接近台盆/淋浴盆")
    for seed in basins:
        sc = (seed["cx"], seed["cy"]) if seed.get("cx") is not None else center(seed["bb"])
        exp = inflate(seed["bb"], gmargin)
        for el in ellipses:
            if not furn(el):
                continue
            maj, minor = el["ell"]
            if not (fmaj[0] <= maj <= fmaj[1] and fmin[0] <= minor <= fmin[1]):
                continue
            if math.hypot(el["cx"] - sc[0], el["cy"] - sc[1]) > fdist and not contained(el["bb"], exp, 80):
                continue
            r = by_h[el["handle"]]
            if not _locked(r):
                _set_cls(r, "洁具", "high", "shape-faucet", "台盆旁的小椭圆，常见龙头")
        for c in circles:
            if not furn(c):
                continue
            if not (drain_r[0] <= c["radius"] <= drain_r[1]):
                continue
            if not (exp[0] <= c["cx"] <= exp[2] and exp[1] <= c["cy"] <= exp[3]):
                continue
            r = by_h[c["handle"]]
            if not _locked(r):
                _set_cls(r, "洁具", "high", "shape-basin-outlet", "台盆范围内的小圆，常见下水")
        # small faucet arcs / short lines near the basin
        for ar in arcs:
            if not furn(ar) or _locked(by_h[ar["handle"]]):
                continue
            if (ar.get("extent") or 0) > 80:
                continue
            ac = (ar["cx"], ar["cy"]) if ar.get("cx") is not None else center(ar["bb"])
            if math.hypot(ac[0] - sc[0], ac[1] - sc[1]) <= fdist + 40:
                _set_cls(by_h[ar["handle"]], "洁具", "high", "shape-faucet-arc",
                         "台盆旁的小圆弧，常见龙头细部")
        for ln in lines:
            if ln.get("closed"):
                continue
            if not furn(ln) or _locked(by_h[ln["handle"]]):
                continue
            length = ln.get("extent") or 0
            if not (80 <= length <= 140):
                continue
            lc = (ln["cx"], ln["cy"]) if ln.get("cx") is not None else center(ln["bb"])
            if math.hypot(lc[0] - sc[0], lc[1] - sc[1]) <= fdist + 40:
                _set_cls(by_h[ln["handle"]], "洁具", "high", "shape-faucet-stem",
                         "台盆旁的短直线，常见龙头细部")

    # round tub/basin: large circle + faucet ellipses
    rb = th.get("round_basin") or {}
    rr = rb.get("r") or [150, 400]
    for c in circles:
        if not furn(c) or not (rr[0] <= c["radius"] <= rr[1]):
            continue
        if _locked(by_h[c["handle"]]) and by_h[c["handle"]].get("category") != "洁具":
            continue
        faucets = []
        for el in ellipses:
            if not furn(el):
                continue
            maj, minor = el["ell"]
            if not (fmaj[0] <= maj <= fmaj[1] and fmin[0] <= minor <= fmin[1]):
                continue
            if math.hypot(el["cx"] - c["cx"], el["cy"] - c["cy"]) <= c["radius"] + fdist:
                faucets.append(el)
        drains = [d for d in circles if furn(d) and drain_r[0] <= d["radius"] <= drain_r[1]
                  and math.hypot(d["cx"] - c["cx"], d["cy"] - c["cy"]) <= c["radius"] * 0.6]
        if len(faucets) < 2 and not drains:
            continue
        if not _locked(by_h[c["handle"]]):
            _set_cls(by_h[c["handle"]], "洁具", "high", "shape-round-basin",
                     "较大圆旁有龙头椭圆或下水，常见圆形浴缸/盆")
        for el in faucets:
            if not _locked(by_h[el["handle"]]):
                _set_cls(by_h[el["handle"]], "洁具", "high", "shape-faucet", "圆盆旁的小椭圆，常见龙头")
        for d in drains:
            if not _locked(by_h[d["handle"]]):
                _set_cls(by_h[d["handle"]], "洁具", "high", "shape-basin-outlet", "圆盆内的小圆，常见下水")
        sc = (c["cx"], c["cy"])
        for ar in arcs:
            if not furn(ar) or _locked(by_h[ar["handle"]]):
                continue
            if (ar.get("extent") or 0) > 80:
                continue
            ac = (ar["cx"], ar["cy"]) if ar.get("cx") is not None else center(ar["bb"])
            if math.hypot(ac[0] - sc[0], ac[1] - sc[1]) <= c["radius"] + 40:
                _set_cls(by_h[ar["handle"]], "洁具", "high", "shape-faucet-arc",
                         "圆盆旁的小圆弧，常见龙头细部")
        for ln in lines:
            if ln.get("closed") or not furn(ln) or _locked(by_h[ln["handle"]]):
                continue
            length = ln.get("extent") or 0
            if not (80 <= length <= 140):
                continue
            lc = (ln["cx"], ln["cy"]) if ln.get("cx") is not None else center(ln["bb"])
            if math.hypot(lc[0] - sc[0], lc[1] - sc[1]) <= c["radius"] + 40:
                _set_cls(by_h[ln["handle"]], "洁具", "high", "shape-faucet-stem",
                         "圆盆旁的短直线，常见龙头细部")

    # cabinet frames (closed or nearly-closed / U-shape) + contained X + spread
    cab = th.get("cabinet") or {}
    cdep, cminl = cab.get("depth") or [300, 650], cab.get("min_length", 800)
    inset = cab.get("offset_inset") or [8, 60]
    xtol, xfrac = cab.get("x_corner_tol", 80), cab.get("x_span_frac", 0.45)
    gap_close = cab.get("nearly_closed_gap", 30)
    spread_tol = cab.get("spread_tol", 15)
    tv_thick = cab.get("spread_tv_thick", 80)
    tv_len_f = cab.get("spread_tv_len_factor", 2.0)
    no_spread = set()

    def _pt_gap(f):
        pts = f.get("pts") or []
        if len(pts) < 2:
            return None
        return math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1])

    def closedish(f):
        if f.get("closed"):
            return True
        g = _pt_gap(f)
        return g is not None and g <= gap_close and len(f.get("pts") or []) >= 4

    def path_len(f):
        pts = f.get("pts") or []
        return sum(math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
                   for i in range(len(pts) - 1))

    def is_axis(f):
        return min(f.get("w") or 0, f.get("h") or 0) <= 15

    def cabinet_depth_box(f):
        short, long_ = f.get("thickness"), f.get("extent")
        if short is None or long_ is None:
            return False
        return cdep[0] <= short <= cdep[1] and long_ >= cminl

    # frames: closedish, or unclosed U/rect of cabinet depth (opening ≈ short side)
    furn_frames = []
    for f in fs:
        if f["type"] not in ("LWPOLYLINE", "POLYLINE") or not furn(f) or not f.get("bb"):
            continue
        if closedish(f) or (cabinet_depth_box(f) and len(f.get("pts") or []) >= 3):
            furn_frames.append(f)

    paired = set()
    cabinet_outers = []
    for i, a in enumerate(furn_frames):
        ca = (a["cx"], a["cy"]) if a.get("cx") is not None else center(a["bb"])
        for b in furn_frames[i + 1:]:
            cb = (b["cx"], b["cy"]) if b.get("cx") is not None else center(b["bb"])
            if dist2(ca, cb) > 80 ** 2:
                continue
            if contained(a["bb"], b["bb"], 5):
                outer, inner = b, a
            elif contained(b["bb"], a["bb"], 5):
                outer, inner = a, b
            else:
                continue
            dw = abs(outer["w"] - inner["w"]) / 2
            dh = abs(outer["h"] - inner["h"]) / 2
            if not (inset[0] <= dw <= inset[1] and inset[0] <= dh <= inset[1]):
                continue
            short, long_ = outer["thickness"], outer["extent"]
            min_len = cminl
            # 已是柜深内框时，外框可略短于 800（如 700 级床边柜）
            if cdep[0] <= (inner["thickness"] or 0) <= cdep[1] and (inner["extent"] or 0) >= 500:
                min_len = min(cminl, 600)
            if not (cdep[0] <= short <= cdep[1] and long_ >= min_len):
                continue
            paired.update((outer["handle"], inner["handle"]))
            cabinet_outers.append(outer)
            for f in (outer, inner):
                r = by_h[f["handle"]]
                if not _locked(r):
                    _set_cls(r, "柜体", "high", "shape-cabinet-offset",
                             "内外相套、间距约板厚的柜框（含贴墙未闭合框）")

    def is_diag_in(ln, box):
        pts = ln.get("pts") or []
        if len(pts) < 2 or len(pts) > 3:
            return False
        (x0, y0), (x1, y1) = pts[0], pts[-1]
        w, h = box[2] - box[0], box[3] - box[1]
        if min(w, h) < 80 or max(w, h) < 200:
            return False
        if abs(x1 - x0) < xfrac * w or abs(y1 - y0) < xfrac * h:
            return False
        if abs(x1 - x0) < 20 or abs(y1 - y0) < 20:
            return False
        if not contained(ln["bb"], box, 40):
            return False
        corners = [(box[0], box[1]), (box[0], box[3]), (box[2], box[1]), (box[2], box[3])]
        def near_c(x, y):
            return min(math.hypot(x - cx, y - cy) for cx, cy in corners) <= xtol
        return near_c(x0, y0) and near_c(x1, y1)

    def has_x_inside(frame):
        return any(is_diag_in(ln, frame["bb"]) for ln in lines
                   if furn(ln) and not ln.get("closed") and ln["handle"] != frame["handle"])

    for outer in cabinet_outers:
        for ln in lines:
            if ln.get("closed") or not furn(ln) or _locked(by_h[ln["handle"]]):
                continue
            if is_diag_in(ln, outer["bb"]):
                _set_cls(by_h[ln["handle"]], "柜体", "high", "shape-cabinet-x",
                         "柜体框内对角线，常见柜门交叉线")

    for f in furn_frames:
        if f["handle"] in paired:
            continue
        r = by_h[f["handle"]]
        if not (r["status"] == "classified" and r.get("category") == "柜体"):
            continue
        if f["thickness"] is None or f["thickness"] < cdep[0]:
            continue
        for ln in lines:
            if ln.get("closed") or not furn(ln) or _locked(by_h[ln["handle"]]):
                continue
            if is_diag_in(ln, f["bb"]):
                _set_cls(by_h[ln["handle"]], "柜体", "high", "shape-cabinet-x",
                         "柜体框内对角线，常见柜门交叉线")

    # shallow closed dashed frame with X (end/side cabinet, not full 300+ depth)
    sh_dep = cab.get("shallow_depth") or [100, 320]
    sh_min = cab.get("shallow_min_length", 400)
    for f in furn_frames:
        r = by_h[f["handle"]]
        if _locked(r) or not closedish(f) or not _is_dashed(f):
            continue
        if not (sh_dep[0] <= (f["thickness"] or 0) <= sh_dep[1] and (f["extent"] or 0) >= sh_min):
            continue
        if not has_x_inside(f):
            continue
        _set_cls(r, "柜体", "high", "shape-cabinet-shallow-x",
                 "较浅的虚线闭合框内有交叉线，常见端部柜或薄柜")
        cabinet_outers.append(f)
        for ln in lines:
            if ln.get("closed") or not furn(ln) or _locked(by_h[ln["handle"]]):
                continue
            if is_diag_in(ln, f["bb"]) or contained(ln["bb"], f["bb"], 20):
                if is_diag_in(ln, f["bb"]):
                    _set_cls(by_h[ln["handle"]], "柜体", "high", "shape-cabinet-x",
                             "薄柜框内交叉线")

    # L-shaped / wall-following cabinet outlines: offset sibling polylines, NO interior spread
    lmin = cab.get("l_path_min", 2500)
    loff = cab.get("l_offset") or [300, 650]

    def _seg_dist(px, py, a, b):
        ax, ay = a; bx, by = b
        vx, vy = bx - ax, by - ay
        den = vx * vx + vy * vy
        if den < 1e-6:
            return math.hypot(px - ax, py - ay)
        t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / den))
        return math.hypot(px - (ax + t * vx), py - (ay + t * vy))

    def _median_offset(fa, fb):
        pa, pb = fa.get("pts") or [], fb.get("pts") or []
        if len(pa) < 2 or len(pb) < 2:
            return None
        ds = []
        for pts, other in ((pa, pb), (pb, pa)):
            for x, y in pts:
                best = min(_seg_dist(x, y, other[i], other[i + 1]) for i in range(len(other) - 1))
                ds.append(best)
        if not ds:
            return None
        ds.sort()
        return ds[len(ds) // 2]

    furn_plines = [f for f in fs if f["type"] in ("LWPOLYLINE", "POLYLINE") and furn(f)
                   and len(f.get("pts") or []) >= 4 and path_len(f) >= lmin
                   and not closedish(f)
                   and (f.get("thickness") or 0) >= cab.get("l_min_short", 900)]
    for i, a in enumerate(furn_plines):
        for b in furn_plines[i + 1:]:
            # bbox overlap required
            if not (a["bb"][0] <= b["bb"][2] and b["bb"][0] <= a["bb"][2]
                    and a["bb"][1] <= b["bb"][3] and b["bb"][1] <= a["bb"][3]):
                continue
            pa, pb = path_len(a), path_len(b)
            if min(pa, pb) / max(pa, pb) < 0.5:
                continue
            off = _median_offset(a, b)
            if off is None or not (loff[0] <= off <= loff[1]):
                continue
            for f in (a, b):
                r = by_h[f["handle"]]
                if not _locked(r):
                    _set_cls(r, "柜体", "high", "shape-cabinet-run",
                             "沿墙的柜体外沿（两条线间距约柜深），不定框内物体")
                    no_spread.add(f["handle"])

    # nightstands: 400–600-ish rectangle beside a detected bed
    ns = cab.get("nightstand_short") or [400, 650]
    nl = cab.get("nightstand_long") or [400, 800]
    nasp = cab.get("nightstand_max_aspect", 1.8)
    ngap = cab.get("nightstand_bed_gap", 300)
    beds = [r["entity"] for r in rows
            if r.get("status") == "classified" and r.get("category") == "家具"
            and (r.get("rule") or "") == "shape-bed" and r["entity"].get("bb")]
    def _gap_boxes(a, b):
        gx = max(0.0, max(a[0] - b[2], b[0] - a[2]))
        gy = max(0.0, max(a[1] - b[3], b[1] - a[3]))
        return gx, gy
    for f in furn_frames:
        r = by_h[f["handle"]]
        if _locked(r) or not closedish(f) or (f.get("n_bulge") or 0) > 0:
            continue
        short, long_ = f["thickness"], f["extent"]
        if short is None or not (ns[0] <= short <= ns[1] and nl[0] <= long_ <= nl[1]):
            continue
        if (f.get("aspect") or 99) > nasp:
            continue
        beside = False
        for bed in beds:
            gx, gy = _gap_boxes(f["bb"], bed["bb"])
            if gx <= ngap and gy <= ngap and max(gx, gy) >= 0:
                # not mostly inside the bed
                if contained(f["bb"], bed["bb"], 20):
                    continue
                beside = True
                break
        if beside:
            _set_cls(r, "柜体", "high", "shape-nightstand",
                     "床侧近方形闭合框，常见床头柜")

    # curtain boxes: thin long closed on furniture layer
    cs = th.get("curtain_strip") or {}
    ct, cmin = cs.get("thickness") or [40, 90], cs.get("min_length", 1500)
    cab_seeds_now = [r["entity"] for r in rows
                     if r["status"] == "classified" and r.get("category") == "柜体" and r["entity"].get("bb")]
    def _inside_any_cab(f, tol=15):
        pts = f.get("pts") or []
        for s in cab_seeds_now:
            exp = inflate(s["bb"], tol)
            if pts and all(exp[0] <= x <= exp[2] and exp[1] <= y <= exp[3] for x, y in pts):
                return True
            if not pts and f.get("bb") and contained(f["bb"], s["bb"], tol):
                return True
        return False
    for f in [x for x in furn_frames if closedish(x)]:
        if _locked(by_h[f["handle"]]):
            continue
        if ct[0] <= (f["thickness"] or 0) <= ct[1] and (f["extent"] or 0) >= cmin:
            if _inside_any_cab(f):
                continue
            _set_cls(by_h[f["handle"]], "家具", "high", "shape-curtain-box",
                     "细长闭合条，常见窗帘盒/轨")

    # dining / writing table (plain closed rect, not cabinet depth)
    tb = th.get("table") or {}
    tshort, tlong = tb.get("short") or [700, 1200], tb.get("long") or [1400, 2400]
    for f in [x for x in furn_frames if closedish(x)]:
        if _locked(by_h[f["handle"]]):
            continue
        if (f.get("n_bulge") or 0) > 0:
            continue
        if tshort[0] <= (f["thickness"] or 0) <= tshort[1] and tlong[0] <= (f["extent"] or 0) <= tlong[1]:
            _set_cls(by_h[f["handle"]], "家具", "high", "shape-table",
                     "家具层上较大的无圆角矩形，常见桌面")

    # ceiling dashed large rounded frame → 灯具
    cf = th.get("ceiling_light_frame") or {}
    for f in closed:
        if not ceil(f) or _locked(by_h[f["handle"]]):
            continue
        if (f.get("area") or 0) < cf.get("min_area", 2000000):
            continue
        if (f.get("n_bulge") or 0) < cf.get("min_bulges", 2):
            continue
        if not _is_dashed(f):
            continue
        _set_cls(by_h[f["handle"]], "灯具", "high", "shape-ceiling-frame",
                 "天花层上较大的虚线圆角框，常见灯槽范围")

    # ----- object spread: internals of high-confidence cabinet seeds -----
    OTHER = {"墙体_保留", "窗", "墙体_门框", "洁具", "电器", "家具", "灯具", "门"}

    def reserved(r):
        if r["status"] == "exclude":
            return True
        if r["status"] == "classified" and r.get("category") in OTHER:
            return True
        if r["status"] == "question" and "柜体" not in (r.get("candidates") or []):
            return True
        return False

    def inside_seed(f, seed, tol):
        sb = seed["bb"]
        pts = f.get("pts") or []
        exp = inflate(sb, tol)
        if pts:
            return all(exp[0] <= x <= exp[2] and exp[1] <= y <= exp[3] for x, y in pts)
        fb = f.get("bb")
        return bool(fb) and contained(fb, sb, tol)

    def looks_internal(f, seed):
        if f["type"] in ("INSERT", "CIRCLE", "ELLIPSE", "ARC", "TEXT", "MTEXT",
                         "DIMENSION", "SPLINE", "HATCH", "SOLID"):
            return False
        depth = min(seed["w"], seed["h"])
        short = min(f.get("w") or 0, f.get("h") or 0)
        long_ = max(f.get("w") or 0, f.get("h") or 0)
        # 沿柜长向的细实线：常见电视/饰条，不扩散
        if short <= tv_thick and long_ > tv_len_f * depth and not _is_dashed(f):
            return False
        if (f.get("area") or 0) > (seed.get("area") or 0) * 1.02 and short > 2:
            return False
        if short < 2 and long_ >= 20:
            return True
        if _is_dashed(f):
            return True
        if is_axis(f) and long_ <= depth * 1.2:
            return True
        if closedish(f) and long_ <= (seed["extent"] or 0) + 20:
            return True
        if f["type"] in ("LINE", "LWPOLYLINE", "POLYLINE") and long_ <= (seed["extent"] or 0) + 20:
            return True
        return False

    seeds = []
    for r in rows:
        if r["status"] != "classified" or r.get("category") != "柜体":
            continue
        f = r["entity"]
        if not f.get("bb") or f["handle"] in no_spread:
            continue
        seeds.append(f)
    # spread from outers only (skip inners fully inside a larger seed)
    spread_from = []
    for s in seeds:
        if any(t["handle"] != s["handle"] and contained(s["bb"], t["bb"], 5)
               and (t.get("area") or 0) > (s.get("area") or 0) * 1.01
               for t in seeds):
            continue
        spread_from.append(s)

    for seed in spread_from:
        for r in rows:
            f = r["entity"]
            if f["handle"] == seed["handle"] or not furn(f):
                continue
            if reserved(r):
                continue
            if r["status"] == "classified" and r.get("category") == "柜体":
                continue
            if not inside_seed(f, seed, spread_tol):
                continue
            if not looks_internal(f, seed):
                continue
            _set_cls(r, "柜体", "high", "shape-cabinet-spread",
                     "已识别柜体外框内的分隔线/内框/交叉线/柜内虚线")

    # leftover cabinet-depth closed frames → confirmation Q (家具/柜体)
    ap = th.get("ambiguous_panel") or {}
    adep, amin = ap.get("depth") or [330, 650], ap.get("min_length", 700)
    for f in [x for x in furn_frames if closedish(x)]:
        r = by_h[f["handle"]]
        if _locked(r) or r["status"] == "question":
            continue
        if f["thickness"] is None:
            continue
        if adep[0] <= f["thickness"] <= adep[1] and f["extent"] >= amin:
            # 近方形闭合框更像冰箱/椅凳，不作为柜体确认项
            if (f.get("aspect") or 1) < 1.6:
                continue
            _set_cls(r, "柜体", "medium", "shape-panel-confirm",
                     "柜深尺度的闭合框，但缺少内框或交叉线，请确认是柜体还是家具",
                     cands=["柜体", "家具"])


def iter_visible(src_path, viewport):
    doc = ezdxf.readfile(src_path)
    for e in doc.modelspace():
        if e.dxftype() not in GEOM:
            continue
        if is_invisible(e):
            continue
        bb = entity_bbox(e)
        if viewport and not in_viewport(bb, viewport):
            continue
        yield e, feats(e)


def classify_doc(src, ruleset, viewport=None):
    rows = []
    for e, f in iter_visible(src, viewport):
        rec = classify_one(f, ruleset)
        rec["entity"] = f
        rows.append(rec)
    apply_shapes(rows, ruleset)
    refine_unknown_inserts(rows, ruleset)
    return rows


def group_questions(rows):
    """Group unknown INSERTs by block name; similar confirmation panels by rule+size; others 1:1."""
    groups, panels, ungrouped = defaultdict(list), defaultdict(list), []
    for r in rows:
        if r["status"] != "question":
            continue
        f = r["entity"]
        if f["type"] == "INSERT" and f["block"]:
            groups[("INSERT", f["block"])].append(r)
        elif r.get("rule") == "shape-panel-confirm" and f.get("bb"):
            short = int(round((f["thickness"] or 0) / 50) * 50)
            long_ = int(round((f["extent"] or 0) / 100) * 100)
            panels[(r["rule"], short, long_)].append(r)
        else:
            ungrouped.append(r)
    items = []
    gid = 1

    def add_item(kind, recs, block="", typ=None, layer=None):
        nonlocal gid
        f0 = recs[0]["entity"]
        sizes = sorted({(round(r["entity"]["w"] or 0), round(r["entity"]["h"] or 0)) for r in recs})
        cands = []
        for rec in recs:
            for c in rec["candidates"]:
                if c not in cands:
                    cands.append(c)
        items.append({
            "id": f"Q{gid:02d}",
            "kind": kind,
            "block": block or (f0.get("block") or ""),
            "count": len(recs),
            "handles": [r["entity"]["handle"] for r in recs],
            "candidates": cands,
            "reason": recs[0]["reason"],
            "sample_bb": f0["bb"],
            "size_mm": list(sizes[0]) if sizes else None,
            "sizes_mm": [list(s) for s in sizes],
            "type": typ or f0["type"],
            "layer": layer or f0["layer"],
            "rule": recs[0].get("rule"),
        })
        gid += 1

    for (kind, name), recs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        add_item("block", recs, block=name)
    for _, recs in sorted(panels.items(), key=lambda kv: -len(kv[1])):
        add_item("panel", recs)
    for r in ungrouped:
        add_item("entity", [r])
    return items


def write_outputs(out: Path, rows, questions, unit_info=None):
    out.mkdir(parents=True, exist_ok=True)
    slim = []
    for r in rows:
        f = r["entity"]
        slim.append({
            "handle": f["handle"], "type": f["type"], "layer": f["layer"],
            "linetype": f["linetype"], "color": f["color"], "block": f["block"],
            "bb": [round(x, 2) for x in f["bb"]] if f["bb"] else None,
            "w": None if f["w"] is None else round(f["w"], 1),
            "h": None if f["h"] is None else round(f["h"], 1),
            "status": r["status"], "category": r["category"], "rule": r["rule"],
            "confidence": r["confidence"], "candidates": r["candidates"],
            "reason": r["reason"],
        })
    (out / "classification.json").write_text(json.dumps(slim, ensure_ascii=False, indent=2) + "\n")
    (out / "questions.json").write_text(json.dumps(questions, ensure_ascii=False, indent=2) + "\n")
    unclass = [s for s in slim if s["status"] == "unclassified"]
    (out / "unclassified.json").write_text(json.dumps(unclass, ensure_ascii=False, indent=2) + "\n")
    by = Counter(s["category"] or s["status"] for s in slim)
    lines = [
        "初分类摘要",
    ]
    if unit_info:
        lines.append(f"  单位: {unit_info.get('note')}")
        if unit_info.get("working_source") and unit_info.get("working_source") != unit_info.get("source"):
            lines.append(f"  工作副本: {unit_info.get('working_source')}（源文件未改）")
        if unit_info.get("viewport_mm"):
            vp = unit_info["viewport_mm"]
            lines.append("  视口(mm): " + ",".join(str(round(x, 3)) if isinstance(x, float) else str(x) for x in vp))
    lines += [
        f"  可见实体 {len(slim)}",
        f"  按类别/状态: {dict(by)}",
        f"  已分类 {sum(1 for s in slim if s['status']=='classified')}",
        f"  未归类 {len(unclass)}",
        f"  排除 {sum(1 for s in slim if s['status']=='exclude')}",
        f"  待确认实体 {sum(1 for s in slim if s['status']=='question')}（合并为 {len(questions)} 项 Q）",
        "",
        "=== 未归类句柄 ===",
    ]
    for s in unclass:
        lines.append(f"  {s['handle']} {s['type']} layer={s['layer']} lt={s['linetype']}")
    lines += ["", "=== Q 清单 ==="]
    for q in questions:
        sz = ""
        if q.get("size_mm"):
            sz = f"  {q['size_mm'][0]}×{q['size_mm'][1]}mm"
        lines.append(f"  {q['id']} n={q['count']}{sz} candidates={q['candidates']} "
                     f"{q.get('block') or q.get('type') or ''}  {q['reason']}")
    (out / "summary.txt").write_text("\n".join(lines) + "\n")
    if unit_info:
        (out / "units.json").write_text(json.dumps(unit_info, ensure_ascii=False, indent=2) + "\n")
    return by


def gold_report(rows, gold_path: Path, out: Path):
    gold = json.loads(gold_path.read_text())
    h2g = {}
    for cat, hs in gold.items():
        for h in hs:
            h2g[str(h).upper()] = cat
    confusion = Counter()
    wrong, q_but, un_but, extra = [], [], [], []
    correct = 0
    considered = 0
    gold_n = Counter(h2g.values())
    hit_n = Counter()
    for r in rows:
        h = r["entity"]["handle"]
        g = h2g.get(h)
        if g is None:
            continue
        considered += 1
        if r["status"] == "classified":
            p = r["category"]
        elif r["status"] == "unclassified":
            p = "其他"
        elif r["status"] == "exclude":
            p = "排除"
        else:
            p = "Q"
        confusion[(g, p)] += 1
        if p == g:
            correct += 1
            hit_n[g] += 1
        elif p == "Q":
            q_but.append((h, g, r["candidates"], r["rule"]))
        elif p == "其他" and g != "其他":
            un_but.append((h, g, r["rule"]))
        elif g == "其他" and p not in ("其他", "Q"):
            extra.append((h, g, p, r["rule"]))
        else:
            wrong.append((h, g, p, r["rule"], r["entity"]["type"], r["entity"]["layer"]))

    seen = {r["entity"]["handle"] for r in rows}
    missing = [h for h, c in h2g.items() if h not in seen]
    acc = correct / considered if considered else 0
    assigned = [r for r in rows if r["status"] == "classified"]
    assigned_ok = sum(1 for r in assigned if h2g.get(r["entity"]["handle"]) == r["category"])
    prec = assigned_ok / len(assigned) if assigned else 0

    lines = [
        "自测对照已定稿",
        f"  对照实体 {considered}  命中（预测=定稿，含「其他」与排除）{correct}  命中率 {acc:.1%}",
        f"  已自动定类 {len(assigned)}  其中正确 {assigned_ok}  定类精确率 {prec:.1%}",
        f"  误分到错误类别 {len(wrong)}",
        f"  定稿有类但被标未归类 {len(un_but)}",
        f"  定稿是「其他」但被分进某类 {len(extra)}",
        f"  进入 Q（不算命中，也不算误分）{len(q_but)}",
        f"  定稿中未出现在本次可见集 {len(missing)}",
        "",
        "=== 各类召回（命中/定稿） ===",
    ]
    for cat, n in gold_n.most_common():
        lines.append(f"  {cat:8} {hit_n[cat]}/{n}  {hit_n[cat]/n:.1%}" if n else f"  {cat}")
    lines += ["", "=== 混淆（定稿 → 预测） 次数>0 ==="]
    for (g, p), n in sorted(confusion.items(), key=lambda kv: -kv[1]):
        mark = "OK" if g == p else "  "
        lines.append(f"  {mark} {g:8} → {p:8}  n={n}")
    lines += ["", "=== 主要误分（定稿有类，预测成另一类） ==="]
    by_pair = Counter((g, p) for _, g, p, *_ in wrong)
    for (g, p), n in by_pair.most_common():
        lines.append(f"  {g} → {p}  n={n}")
        for item in [w for w in wrong if w[1] == g and w[2] == p][:8]:
            h, g, p, rule, typ, layer = item
            lines.append(f"      {h} {typ} layer={layer} rule={rule}")
    lines += ["", "=== 定稿为其他但被收进某类（多收） 按预测类 ==="]
    by_p = Counter(p for *_, p, rule in extra)
    for p, n in by_p.most_common():
        lines.append(f"  → {p} n={n}")
        for item in [e for e in extra if e[2] == p][:8]:
            lines.append(f"      {item[0]} rule={item[3]}")
    lines += ["", "=== 定稿有类但落进未归类 按定稿类 ==="]
    by_g = Counter(g for _, g, _ in un_but)
    for g, n in by_g.most_common():
        lines.append(f"  {g} n={n}")
    lines += ["", "=== 进 Q 的定稿类分布 ==="]
    by_g = Counter(g for _, g, *_ in q_but)
    for g, n in by_g.most_common():
        lines.append(f"  {g} n={n}  rules={Counter(rule for _, gg, _, rule in q_but if gg==g)}")
    report = "\n".join(lines) + "\n"
    (out / "report.txt").write_text(report)
    print(report)
    return acc, prec, dict(by_pair)


def _render_crop(src_path, viewport, color, width, height, handles=None):
    from ezdxf.addons.drawing import Frontend, RenderContext, layout, config
    from ezdxf.addons.drawing.svg import SVGBackend
    from ezdxf.math import BoundingBox2d
    import cairosvg
    from PIL import Image

    document = ezdxf.readfile(src_path)
    if handles is not None:
        want = {h.upper() for h in handles}
        msp = document.modelspace()
        for e in list(msp):
            if e.dxf.handle.upper() not in want:
                try:
                    msp.unlink_entity(e)
                except Exception:
                    try:
                        msp.delete_entity(e)
                    except Exception:
                        pass
    cfg = config.Configuration(
        background_policy=config.BackgroundPolicy.BLACK,
        color_policy=config.ColorPolicy.CUSTOM,
        custom_fg_color=color,
        lineweight_policy=config.LineweightPolicy.RELATIVE_FIXED,
        lineweight_scaling=1.4 if handles is not None else 1.0,
        hatch_policy=config.HatchPolicy.IGNORE,
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
        settings=layout.Settings(fixed_stroke_width=0.9 if handles is not None else 0.6, crop_at_margins=True),
        render_box=BoundingBox2d([(x0, y0), (x1, y1)]),
    )
    png = cairosvg.svg2png(bytestring=svg.encode(), output_width=width, output_height=height)
    return Image.open(io.BytesIO(png)).convert("RGB")


def _composite_gold(gray, gold, gold_rgb=(255, 196, 0)):
    import numpy as np
    g = np.array(gray)
    h = np.array(gold)
    mask = h.max(axis=2) > 30
    out = g.copy()
    out[mask] = gold_rgb
    from PIL import Image
    return Image.fromarray(out)


def _union_bb(bbs, margin, min_size=1200, max_size=9000):
    xs = [b[0] for b in bbs] + [b[2] for b in bbs]
    ys = [b[1] for b in bbs] + [b[3] for b in bbs]
    x0, y0, x1, y1 = min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin
    w, h = x1 - x0, y1 - y0
    if w < min_size:
        pad = (min_size - w) / 2
        x0, x1 = x0 - pad, x1 + pad
    if h < min_size:
        pad = (min_size - h) / 2
        y0, y1 = y0 - pad, y1 + pad
    # cap extremely large unions
    if x1 - x0 > max_size:
        mid = (x0 + x1) / 2
        x0, x1 = mid - max_size / 2, mid + max_size / 2
    if y1 - y0 > max_size:
        mid = (y0 + y1) / 2
        y0, y1 = mid - max_size / 2, mid + max_size / 2
    return (x0, y0, x1, y1)


def _instance_groups(bbs, compact=8000):
    """If all instances fit a compact union, one group; else one group per instance (max 3)."""
    if not bbs:
        return []
    union_w = max(b[2] for b in bbs) - min(b[0] for b in bbs)
    union_h = max(b[3] for b in bbs) - min(b[1] for b in bbs)
    if union_w <= compact and union_h <= compact:
        return [bbs]
    return [[b] for b in bbs[:3]]


def maybe_q_sheet(src, rows, questions, font, out: Path, max_per_sheet=9):
    """Contact sheet: gray underlay + gold highlight, caption with name/count/size/candidates."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        import numpy as np
        import cairosvg  # noqa: F401
    except Exception as ex:
        print("skip q-crops:", ex)
        return
    font_path = font
    if not font_path or not Path(font_path).exists():
        print("skip q-crops: font not found")
        return
    if not questions:
        print("no Q items, skip q-sheet")
        return

    ft = ImageFont.truetype(font_path, 15)
    fs = ImageFont.truetype(font_path, 13)
    fh = ImageFont.truetype(font_path, 20)
    h2bb = {r["entity"]["handle"]: r["entity"]["bb"] for r in rows if r["entity"].get("bb")}

    CELL_W, IMG_H, CAP_H = 520, 420, 118
    CELL_H = IMG_H + CAP_H
    COLS = 3
    GAP, MARGIN, TITLE = 16, 24, 52

    crops = []
    for q in questions:
        bbs = [h2bb[h] for h in q["handles"] if h in h2bb]
        groups = _instance_groups(bbs)
        parts = []
        for g in groups:
            vp = _union_bb(g, margin=400)
            try:
                gray = _render_crop(src, vp, "#707070", CELL_W, IMG_H, handles=None)
                gold = _render_crop(src, vp, "#FFC400", CELL_W, IMG_H, handles=q["handles"])
                parts.append(_composite_gold(gray, gold))
            except Exception as ex:
                print("  crop failed", q["id"], ex)
        if not parts:
            im = Image.new("RGB", (CELL_W, IMG_H), (16, 16, 16))
        elif len(parts) == 1:
            im = parts[0]
        else:
            # side-by-side mini crops
            w = CELL_W // len(parts)
            im = Image.new("RGB", (CELL_W, IMG_H), (16, 16, 16))
            for i, p in enumerate(parts):
                im.paste(p.resize((w - 2, IMG_H)), (i * w, 0))
        crops.append((q, im))

    n_sheets = max(1, math.ceil(len(crops) / max_per_sheet))
    paths = []
    for si in range(n_sheets):
        chunk = crops[si * max_per_sheet:(si + 1) * max_per_sheet]
        rows_n = math.ceil(len(chunk) / COLS)
        W = MARGIN * 2 + COLS * CELL_W + (COLS - 1) * GAP
        H = TITLE + MARGIN + rows_n * CELL_H + (rows_n - 1) * GAP + MARGIN
        canvas = Image.new("RGB", (W, H), (12, 12, 12))
        d = ImageDraw.Draw(canvas)
        title = "Q 清单（灰底 + 金高亮待确认项）"
        if n_sheets > 1:
            title += f"  {si + 1}/{n_sheets}"
        d.text((MARGIN, 14), title, fill=(240, 240, 240), font=fh)
        for i, (q, im) in enumerate(chunk):
            r, c = divmod(i, COLS)
            x = MARGIN + c * (CELL_W + GAP)
            y = TITLE + r * (CELL_H + GAP)
            # frame
            d.rectangle([x - 2, y - 2, x + CELL_W + 1, y + CELL_H + 1], outline=(60, 60, 60))
            canvas.paste(im, (x, y))
            cap_y = y + IMG_H + 6
            sz = ""
            if q.get("sizes_mm"):
                sz = "、".join(f"{a}×{b}mm" for a, b in q["sizes_mm"][:3])
            elif q.get("size_mm"):
                sz = f"{q['size_mm'][0]}×{q['size_mm'][1]}mm"
            name = q.get("block") or q.get("type") or ""
            cands = " / ".join(q.get("candidates") or [])
            line1 = f"{q['id']}  {name}  ×{q['count']}"
            line2 = f"尺寸 {sz}" if sz else ""
            line3 = f"候选：{cands}"
            line4 = (q.get("reason") or "")[:42]
            d.text((x + 8, cap_y), line1, fill=(255, 196, 0), font=ft)
            if line2:
                d.text((x + 8, cap_y + 22), line2, fill=(210, 210, 200), font=fs)
            d.text((x + 8, cap_y + 42), line3, fill=(180, 220, 180), font=fs)
            d.text((x + 8, cap_y + 62), line4, fill=(170, 170, 170), font=fs)
        name = "q-sheet.png" if si == 0 else f"q-sheet-{si + 1:02d}.png"
        path = out / name
        canvas.save(path)
        paths.append(path)
        print("saved", path)
    return paths


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Classify floorplan entities by rules.yaml")
    p.add_argument("--source", required=True)
    p.add_argument("--rules", required=True)
    p.add_argument("--viewport", default="", help="xmin,ymin,xmax,ymax；省略则不过滤")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--gold", default="", help="optional {category: [handles]} to score")
    p.add_argument("--q-crops", action="store_true")
    p.add_argument("--font", default="")
    args = p.parse_args(argv)
    vp = None
    if args.viewport:
        vp = tuple(float(x) for x in args.viewport.split(","))
        assert len(vp) == 4
    ruleset = load_rules(Path(args.rules))
    out = Path(args.out_dir)
    work_src, vp_mm, unit_info = prepare_source_mm(args.source, vp, out)
    print("units:", unit_info.get("note"))
    if work_src != str(Path(args.source).resolve()):
        print("working copy:", work_src)
    rows = classify_doc(work_src, ruleset, vp_mm)
    questions = group_questions(rows)
    by = write_outputs(out, rows, questions, unit_info=unit_info)
    print("classified", len(rows), "by", dict(by), "Q items", len(questions), "→", out)
    if args.gold:
        gold_report(rows, Path(args.gold), out)
    if args.q_crops:
        maybe_q_sheet(work_src, rows, questions, args.font, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
