#!/usr/bin/env python3
"""Merge finalized category DXFs into one colored master drawing.

Manifest JSON example:
{
  "layers": [
    {"name": "墙体_保留", "dxf": "walls.dxf", "from_layer": "墙体_保留", "color": "#E6E6E6"},
    {"name": "家具", "dxf": "furn.dxf", "from_layer": "家具", "color": "#B266FF"}
  ],
  "other": {"name": "其他", "color": "#707070", "handles": ["AAAA"]},
  "exclude_handles": ["PERSON"],
  "exclude_types": ["DIMENSION"]
}

  python merge_master.py --manifest merge.json --source src.dxf --out-dxf merge.dxf
  python merge_master.py --check-colors merge.json   # exact hex collisions
  python merge_master.py --check-handles a.json b.json
"""
from __future__ import annotations

import argparse, json, sys
from collections import Counter, defaultdict
from pathlib import Path

import ezdxf
from ezdxf.addons.importer import Importer
from ezdxf import colors as ezcolors

# reuse overlap helper
from dxftools import rewrite_xyb, set_bylayer, cmd_overlap, is_invisible


def load(path):
    return json.loads(Path(path).read_text())


def hex_to_rgb(hx):
    hx = hx.lstrip("#")
    return tuple(int(hx[i:i + 2], 16) for i in (0, 2, 4))


def check_colors(manifest) -> int:
    seen = {}
    dups = []
    for layer in manifest["layers"] + ([manifest["other"]] if manifest.get("other") else []):
        hx = layer["color"].upper()
        if hx in seen:
            dups.append((layer["name"], seen[hx], hx))
        else:
            seen[hx] = layer["name"]
    if dups:
        print("COLOR COLLISION (exact hex):")
        for a, b, hx in dups:
            print(f"  {a} == {b}  {hx}")
        return 1
    print("no exact color collisions:",
          {L["name"]: L["color"] for L in manifest["layers"] + ([manifest["other"]] if manifest.get("other") else [])})
    return 0


def cmd_merge(args) -> int:
    man = load(args.manifest)
    src = ezdxf.readfile(args.source) if args.source else None
    doc = ezdxf.new("R2010", setup=True)
    for i, layer in enumerate(man["layers"] + ([man["other"]] if man.get("other") else [])):
        name, hx = layer["name"], layer["color"]
        if name not in doc.layers:
            doc.layers.add(name, color=8)
        try:
            doc.layers.get(name).dxf.true_color = ezcolors.rgb2int(hex_to_rgb(hx))
        except Exception:
            pass

    # import official layers from each category DXF (not leftover layers)
    for layer in man["layers"]:
        cat = ezdxf.readfile(layer["dxf"])
        want = layer.get("from_layer") or layer["name"]
        ents = [e for e in cat.modelspace() if e.dxf.layer == want]
        if not ents:
            print(f"warning: no ents on {want} in {layer['dxf']}", file=sys.stderr)
            continue
        imp = Importer(cat, doc)
        imp.import_entities(ents)
        imp.finalize()
        # newly imported ents keep from_layer name; rename if needed
        if want != layer["name"]:
            for e in doc.modelspace():
                if e.dxf.layer == want:
                    e.dxf.layer = layer["name"]
        print(f"  imported {len(ents)} from {Path(layer['dxf']).name} layer {want} -> {layer['name']}")

    exclude_h = {h.upper() for h in man.get("exclude_handles") or []}
    exclude_t = {t.upper() for t in man.get("exclude_types") or ["DIMENSION"]}
    other = man.get("other")
    if other and src is not None:
        hs = [h.upper() for h in other.get("handles") or []]
        ents, missing = [], []
        for h in hs:
            if h in exclude_h:
                continue
            e = src.entitydb.get(h)
            if e is None:
                missing.append(h)
            elif e.dxftype() in exclude_t:
                continue
            else:
                ents.append(e)
        if missing:
            print("missing 其他 handles:", missing, file=sys.stderr)
            return 2
        if ents:
            imp = Importer(src, doc)
            imp.import_entities(ents)
            imp.finalize()
            # anything not already on a declared layer is leftover import
            official = {L["name"] for L in man["layers"]}
            n = 0
            for e in doc.modelspace():
                if e.dxf.layer not in official and e.dxf.layer != other["name"]:
                    e.dxf.layer = other["name"]
                    n += 1
            print(f"  imported {len(ents)} onto {other['name']} (reassigned {n})")

    for e in doc.modelspace():
        set_bylayer(e)
        if e.dxftype() in exclude_t:
            print("warning: excluded type still present", e.dxftype(), e.dxf.handle, file=sys.stderr)
    n_xyb = rewrite_xyb(doc)
    counts = Counter(e.dxf.layer for e in doc.modelspace())
    auditor = doc.audit()
    handles = [e.dxf.handle for e in doc.modelspace()]
    dups = len(handles) - len(set(handles))
    doc.saveas(args.out_dxf)
    print("saved", args.out_dxf, "layers", dict(counts), "xyb", n_xyb,
          "audit", len(auditor.errors), "dup_dxf_handles", dups)
    return 0 if not auditor.errors and dups == 0 else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Merge category DXFs")
    sub = p.add_subparsers(dest="cmd")
    m = sub.add_parser("build", aliases=[])
    # default command = build if --manifest present
    p.add_argument("--manifest")
    p.add_argument("--source", default="")
    p.add_argument("--out-dxf")
    p.add_argument("--check-colors", action="store_true")
    p.add_argument("--check-handles", nargs="+", metavar="JSON")
    args = p.parse_args(argv)
    if args.check_handles:
        return cmd_overlap(args.check_handles)
    if not args.manifest:
        p.error("--manifest is required (except --check-handles)")
    man = load(args.manifest)
    rc = 0
    if args.check_colors or not args.out_dxf:
        rc = check_colors(man)
        if not args.out_dxf:
            return rc
    if args.out_dxf:
        rc2 = cmd_merge(args)
        return rc2 or rc
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
