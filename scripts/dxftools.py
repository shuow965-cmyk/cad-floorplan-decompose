#!/usr/bin/env python3
"""DXF helpers for floorplan decompose: audit, xyb rewrite, handle import, layer counts, overlap check.

Examples:
  python dxftools.py audit out.dxf
  python dxftools.py layers out.dxf
  python dxftools.py xyb out.dxf
  python dxftools.py import-handles --source src.dxf --map layers.json --out cat.dxf --underlay 原图底图
  python dxftools.py overlap walls.json furniture.json sanitary.json
"""
from __future__ import annotations

import argparse, json, sys
from collections import Counter, defaultdict
from pathlib import Path

import ezdxf
from ezdxf.addons.importer import Importer
from ezdxf import colors as ezcolors


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


def rewrite_xyb(doc) -> int:
    n = 0
    for e in doc.modelspace():
        if e.dxftype() == "LWPOLYLINE":
            e.set_points(e.get_points(format="xyb"), format="xyb")
            n += 1
    return n


def set_bylayer(e) -> None:
    try:
        e.dxf.color = 256
    except Exception:
        pass
    try:
        if e.dxf.hasattr("true_color"):
            e.dxf.discard("true_color")
    except Exception:
        pass


def cmd_audit(path: str) -> int:
    doc = ezdxf.readfile(path)
    n = len(doc.audit().errors)
    n2 = len(ezdxf.readfile(path).audit().errors)
    print(f"{path}: audit {n}, reopen {n2}, ents {len(list(doc.modelspace()))}")
    return 0 if n == 0 and n2 == 0 else 1


def cmd_layers(path: str) -> int:
    doc = ezdxf.readfile(path)
    c = Counter(e.dxf.layer for e in doc.modelspace())
    for k, n in c.most_common():
        print(f"  {k}: {n}")
    print(f"  total: {sum(c.values())}")
    return 0


def cmd_xyb(path: str) -> int:
    doc = ezdxf.readfile(path)
    n = rewrite_xyb(doc)
    auditor = doc.audit()
    doc.saveas(path)
    print(f"rewrote {n} LWPOLYLINE xyb, audit {len(auditor.errors)}, saved {path}")
    return 0 if not auditor.errors else 1


def cmd_import(args) -> int:
    mapping = json.loads(Path(args.map).read_text())
    # {"层名": ["HANDLE", ...]} or {"layers": {"层名": {"handles": [...], "color": "#RRGGBB"}}}
    if "layers" in mapping and isinstance(mapping["layers"], dict):
        layer_spec = mapping["layers"]
        handle_map = {name: spec["handles"] if isinstance(spec, dict) else spec
                      for name, spec in layer_spec.items()}
        colors = {name: spec.get("color") for name, spec in layer_spec.items()
                  if isinstance(spec, dict) and spec.get("color")}
    else:
        handle_map = mapping
        colors = {}
    src = ezdxf.readfile(args.source)
    doc = ezdxf.new(args.dxf_version, setup=True)
    underlay = args.underlay
    layer_names = list(handle_map) + ([underlay] if underlay else [])
    for i, name in enumerate(layer_names):
        if name not in doc.layers:
            doc.layers.add(name, color=8 if name == underlay else (200 + i) % 255 or 1)
        hx = colors.get(name)
        if hx and hx.startswith("#") and len(hx) == 7:
            rgb = tuple(int(hx[j:j + 2], 16) for j in (1, 3, 5))
            try:
                doc.layers.get(name).dxf.true_color = ezcolors.rgb2int(rgb)
            except Exception:
                pass

    assigned = {h.upper(): layer for layer, hs in handle_map.items() for h in hs}
    if len(assigned) != sum(len(v) for v in handle_map.values()):
        print("error: duplicate handles inside the layer map", file=sys.stderr)
        return 2

    ents, missing = [], []
    if underlay:
        for e in src.modelspace():
            if is_invisible(e):
                continue
            ents.append(e)
    else:
        for h in assigned:
            e = src.entitydb.get(h)
            if e is None:
                missing.append(h)
            else:
                ents.append(e)
        if missing:
            print("missing handles:", ", ".join(missing), file=sys.stderr)
            return 2

    importer = Importer(src, doc)
    importer.import_entities(ents)
    importer.finalize()
    # Importer remaps handles; pair by order with the source list we passed.
    # Safer: assign by matching original handle via a zip with ents after import is unreliable.
    # Re-read mapping by geometry is heavy; instead import in two passes if underlay.
    # Here we re-open source handles and assign by iterating destination in import order.
    # ezdxf Importer preserves entity order of the given list.
    msp = list(doc.modelspace())
    if len(msp) != len(ents):
        print(f"warning: imported {len(msp)} != requested {len(ents)}", file=sys.stderr)
    n_assigned = 0
    for src_e, dst_e in zip(ents, msp):
        h = src_e.dxf.handle.upper()
        layer = assigned.get(h, underlay or dst_e.dxf.layer)
        dst_e.dxf.layer = layer
        set_bylayer(dst_e)
        if layer != underlay:
            n_assigned += 1
    n_xyb = rewrite_xyb(doc)
    auditor = doc.audit()
    doc.saveas(args.out)
    counts = Counter(e.dxf.layer for e in doc.modelspace())
    print("saved", args.out, "layers", dict(counts), "xyb", n_xyb,
          "highlighted", n_assigned, "audit", len(auditor.errors))
    return 0 if not auditor.errors else 1


def _collect_handles(obj, acc, path=""):
    if isinstance(obj, dict):
        if "handles" in obj and isinstance(obj["handles"], list):
            name = obj.get("id") or obj.get("name") or path or "?"
            for h in obj["handles"]:
                acc.append((str(h).upper(), name, path))
        for k, v in obj.items():
            _collect_handles(v, acc, f"{path}/{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _collect_handles(v, acc, f"{path}[{i}]")


def cmd_overlap(files) -> int:
    by_handle = defaultdict(list)
    for f in files:
        data = json.loads(Path(f).read_text())
        acc = []
        _collect_handles(data, acc, Path(f).name)
        for h, name, path in acc:
            by_handle[h].append((Path(f).name, name, path))
    dups = {h: v for h, v in by_handle.items() if len(v) > 1}
    if not dups:
        print(f"no overlapping handles across {len(files)} file(s), unique={len(by_handle)}")
        return 0
    print(f"OVERLAP {len(dups)} handles:")
    for h, v in sorted(dups.items()):
        print(f"  {h}: {v}")
    return 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Floorplan DXF helpers")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("audit"); a.add_argument("path")
    a = sub.add_parser("layers"); a.add_argument("path")
    a = sub.add_parser("xyb"); a.add_argument("path")
    a = sub.add_parser("import-handles")
    a.add_argument("--source", required=True)
    a.add_argument("--map", required=True, help="JSON: {layer: [handles]} ")
    a.add_argument("--out", required=True)
    a.add_argument("--underlay", default="", help="put remaining visible ents on this layer")
    a.add_argument("--dxf-version", default="R2010")
    a = sub.add_parser("overlap"); a.add_argument("files", nargs="+")
    args = p.parse_args(argv)
    if args.cmd == "audit":
        return cmd_audit(args.path)
    if args.cmd == "layers":
        return cmd_layers(args.path)
    if args.cmd == "xyb":
        return cmd_xyb(args.path)
    if args.cmd == "import-handles":
        return cmd_import(args)
    if args.cmd == "overlap":
        return cmd_overlap(args.files)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
