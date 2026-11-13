"""Previews (schematic / PCB SVG, 3D render) and fabrication package (Gerber, drill, BOM, CPL)."""
from __future__ import annotations

import csv
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

from . import config
from .spec import Design


def _cli(*args, timeout=300):
    return subprocess.run([str(config.KICAD_CLI), *map(str, args)], capture_output=True, text=True, timeout=timeout)


def previews(d: Design) -> dict:
    out = d.project_dir / "out" / "preview"
    out.mkdir(parents=True, exist_ok=True)
    sch = d.project_dir / f"{d.name}.kicad_sch"
    pcb = d.project_dir / f"{d.name}.kicad_pcb"
    res = {}
    if sch.exists():
        p = _cli("sch", "export", "svg", "-e", "-o", out, sch)
        svgs = sorted(out.glob(f"{d.name}*.svg"))
        if svgs:
            target = out / "schematic.svg"
            svgs[0].replace(target)
            res["schematic"] = str(target)
        else:
            res["schematic_error"] = p.stderr[-500:]
    if pcb.exists():
        for name, layers, extra in (
                ("pcb_top", "F.Cu,F.SilkS,F.Mask,Edge.Cuts", []),
                ("pcb_bottom", "B.Cu,B.SilkS,Edge.Cuts", ["--mirror"]),
                ("pcb_all", "B.Cu,F.Cu,F.SilkS,Edge.Cuts", [])):
            target = out / f"{name}.svg"
            _cli("pcb", "export", "svg", "--mode-single", "--page-size-mode", "2", "--exclude-drawing-sheet",
                 "-l", layers, *extra, "-o", target, pcb)
            if target.exists():
                res[name] = str(target)
        for name, extra in (("render_top", ["--side", "top"]), ("render_iso", ["--rotate", "-40,0,30", "--perspective"]),
                            ("render_bottom", ["--side", "bottom"])):
            target = out / f"{name}.png"
            _cli("pcb", "render", "-w", "1400", "-h", "900", "--quality", "basic", "--background", "opaque",
                 *extra, "-o", target, pcb, timeout=600)
            if target.exists():
                res[name] = str(target)
    (out / "index.json").write_text(json.dumps(res, indent=1))
    return res


def bom_rows(d: Design) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for ref, p in d.parts.items():
        if not p.footprint or p.data.get("dnp") or p.footprint_id.startswith("MountingHole"):
            continue
        key = (p.value, p.footprint_id, p.data.get("lcsc", ""), p.data.get("mpn", ""))
        g = groups.setdefault(key, {"Comment": p.value, "Designator": [], "Footprint": p.footprint_id.split(":")[-1],
                                    "LCSC Part #": p.data.get("lcsc", ""), "MPN": p.data.get("mpn", ""),
                                    "Manufacturer": p.data.get("manufacturer", ""),
                                    "Description": p.data.get("description", "")})
        g["Designator"].append(ref)
    rows = []
    for g in groups.values():
        g["Designator"] = ",".join(sorted(g["Designator"], key=lambda r: (r.rstrip("0123456789"), int("0" + "".join(c for c in r if c.isdigit())))))
        g["Qty"] = len(g["Designator"].split(","))
        rows.append(g)
    return sorted(rows, key=lambda r: r["Designator"])


def export(d: Design, force: bool = False) -> dict:
    out = d.project_dir / "out"
    review = out / "review.json"
    checks = out / "checks.json"
    gate = {"review": None, "auto_pass": None}
    if review.exists():
        gate["review"] = json.loads(review.read_text()).get("verdict")
    if checks.exists():
        gate["auto_pass"] = json.loads(checks.read_text()).get("auto_pass")
    if not force and not (gate["review"] == "PASS" and gate["auto_pass"]):
        return {"ok": False, "blocked": True, "gate": gate,
                "message": "Fab export requires auto checks with 0 errors AND an AI review verdict of PASS "
                           "(use force=True to override for a prototype)."}
    fab = out / "fab"
    if fab.exists():
        shutil.rmtree(fab)
    g = fab / "gerbers"
    g.mkdir(parents=True)
    pcb = d.project_dir / f"{d.name}.kicad_pcb"
    layers = "F.Cu,B.Cu,F.Paste,B.Paste,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts"
    r1 = _cli("pcb", "export", "gerbers", "-l", layers, "--subtract-soldermask", "-o", g, pcb)
    r2 = _cli("pcb", "export", "drill", "--format", "excellon", "--excellon-units", "mm", "--generate-map",
              "--map-format", "gerberx2", "-o", g, pcb)
    zpath = fab / f"{d.name}_gerbers.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(g.iterdir()):
            z.write(f, f.name)
    # BOM (JLCPCB format + full)
    rows = bom_rows(d)
    with open(fab / f"{d.name}_BOM.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["Comment", "Designator", "Footprint", "LCSC Part #", "Qty", "MPN", "Manufacturer", "Description"])
        w.writeheader()
        w.writerows(rows)
    # CPL
    pos = fab / "pos_kicad.csv"
    _cli("pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both", "--exclude-dnp", "-o", pos, pcb)
    cpl = fab / f"{d.name}_CPL.csv"
    if pos.exists():
        with open(pos, encoding="utf-8") as fh, open(cpl, "w", newline="", encoding="utf-8") as oh:
            rd = csv.DictReader(fh)
            w = csv.writer(oh)
            w.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
            for r in rd:
                if r["Ref"].startswith("H"):
                    continue
                w.writerow([r["Ref"], f"{float(r['PosX']):.3f}mm", f"{float(r['PosY']):.3f}mm",
                            "Top" if r["Side"] == "top" else "Bottom", f"{float(r['Rot']):.1f}"])
    _cli("sch", "export", "pdf", "-o", fab / f"{d.name}_schematic.pdf", d.project_dir / f"{d.name}.kicad_sch")
    _cli("pcb", "export", "step", "--subst-models", "-f", "-o", fab / f"{d.name}.step", pcb, timeout=900)
    files = sorted(str(p.relative_to(fab)) for p in fab.rglob("*") if p.is_file())
    res = {"ok": zpath.exists() and (fab / f"{d.name}_BOM.csv").exists(), "dir": str(fab), "files": files,
           "gerber_log": (r1.stderr + r2.stderr)[-800:], "gate": gate, "forced": force and gate["review"] != "PASS",
           "note": "JLCPCB CPL rotations may need per-footprint offsets; check in their preview before ordering."}
    (fab / "manifest.json").write_text(json.dumps(res, indent=1))
    return res
