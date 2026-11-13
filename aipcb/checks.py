"""Verification: KiCad ERC / DRC + manufacturability (DFM) + design heuristics.

Every finding: {"id", "severity": error|warning|info, "category", "message", "refs": []}
"""
from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path

from . import config
from .placement import ORIGIN, rot_pt
from .spec import Design

GND_NAMES = ("GND", "AGND", "DGND", "PGND")


def _cli(*args, timeout=300) -> subprocess.CompletedProcess:
    return subprocess.run([str(config.KICAD_CLI), *map(str, args)], capture_output=True, text=True, timeout=timeout)


def run_erc(d: Design) -> dict:
    sch = d.project_dir / f"{d.name}.kicad_sch"
    out = d.project_dir / "out" / "erc.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    p = _cli("sch", "erc", "--format", "json", "--severity-all", "-o", out, sch)
    if not out.exists():
        return {"ok": False, "error": p.stderr or p.stdout, "violations": []}
    data = json.loads(out.read_text())
    viol = []
    for sh in data.get("sheets", []):
        for v in sh.get("violations", []):
            viol.append({"type": v.get("type"), "severity": v.get("severity"), "description": v.get("description"),
                         "items": [i.get("description") for i in v.get("items", [])]})
    errors = [v for v in viol if v["severity"] == "error"]
    return {"ok": not errors, "errors": len(errors), "warnings": len(viol) - len(errors), "violations": viol}


def run_drc(d: Design, refill: bool = True) -> dict:
    pcb = d.project_dir / f"{d.name}.kicad_pcb"
    out = d.project_dir / "out" / "drc.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    args = ["pcb", "drc", "--format", "json", "--severity-all", "--schematic-parity", "-o", out]
    if refill:
        args += ["--refill-zones", "--save-board"]
    p = _cli(*args, pcb)
    if not out.exists():
        return {"ok": False, "error": p.stderr or p.stdout, "violations": []}
    data = json.loads(out.read_text())

    def conv(lst):
        return [{"type": v.get("type"), "severity": v.get("severity"), "description": v.get("description"),
                 "items": [i.get("description") for i in v.get("items", [])],
                 "pos": [(i.get("pos") or {}) for i in v.get("items", [])][:2]} for v in lst]
    viol = conv(data.get("violations", []))
    unc = conv(data.get("unconnected_items", []))
    par = conv(data.get("schematic_parity", []))
    errors = [v for v in viol + par if v["severity"] == "error"] + unc
    by_type: dict[str, int] = {}
    for v in viol + unc + par:
        by_type[v["type"]] = by_type.get(v["type"], 0) + 1
    return {"ok": not errors, "errors": len(errors), "unconnected": len(unc),
            "warnings": len([v for v in viol + par if v["severity"] == "warning"]),
            "by_type": by_type, "violations": viol, "unconnected_items": unc, "parity": par}


def geom_check(d: Design, nets: tuple[str, ...] = ("GND",)) -> dict:
    """Shape-exact connectivity of the pour nets via KiCad's own Python (aipcb/kicad_geomcheck.py).

    Stricter than zonefix's sample points: items are connected only where their real copper overlaps."""
    pcb = d.project_dir / f"{d.name}.kicad_pcb"
    script = Path(__file__).with_name("kicad_geomcheck.py")
    if not config.KICAD_PYTHON.exists():
        return {"ok": True, "skipped": f"KiCad Python not found at {config.KICAD_PYTHON} (set AIPCB_KICAD_PYTHON)"}
    nets = tuple(n for n in nets if n in d.nets)
    if not nets:
        return {"ok": True, "nets": []}
    p = subprocess.run([str(config.KICAD_PYTHON), "-I", str(script), str(pcb), *nets],
                       capture_output=True, text=True, timeout=300)
    line = next((l for l in reversed(p.stdout.splitlines()) if l.startswith("{")), None)
    if p.returncode or not line:
        return {"ok": False, "error": (p.stderr or p.stdout)[-800:]}
    res = json.loads(line)
    res["ok"] = all(n["ok"] for n in res["nets"])
    return res


# ------------------------------------------------------------- heuristics
def _val(s: str) -> float | None:
    m = re.match(r"^\s*([0-9.]+)\s*([pnuµmkKMG]?)", str(s).replace("R", "").replace("Ω", ""))
    if not m:
        return None
    mult = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "": 1}[m.group(2)]
    try:
        return float(m.group(1)) * mult
    except ValueError:
        return None


def design_checks(d: Design, positions: dict | None, routes: dict | None) -> list[dict]:
    f: list[dict] = []
    add = lambda sev, cat, msg, refs=(): f.append({"severity": sev, "category": cat, "message": msg, "refs": list(refs)})
    pin_net = d.pin_net()
    gnd = [n for n in d.nets if n.upper() in GND_NAMES]

    def part_nets(ref):
        return {n for (r, _), n in pin_net.items() if r == ref}

    # --- spec validation
    for e in d.errors:
        add("error", "netlist", e)
    for w in d.warnings:
        add("warning", "netlist", w)

    # --- decoupling caps per IC supply pin
    caps = {r: p for r, p in d.parts.items() if r.startswith("C")}
    for ref, part in d.parts.items():
        if not ref.startswith("U") or not part.symbol:
            continue
        supply = {pin_net.get((ref, p.number)) for p in part.symbol.pins if p.etype == "power_in"} - {None} - set(gnd)
        for net in supply:
            dec = [c for c, cp in caps.items() if net in part_nets(c) and part_nets(c) & set(gnd)]
            if not dec:
                add("error", "power-integrity", f"{ref} supply '{net}' has no decoupling capacitor to GND", [ref])
                continue
            if positions and ref in positions:
                pin_xy = []
                for pad in part.footprint.pads:
                    if pin_net.get((ref, pad.number)) == net:
                        dx, dy = rot_pt(pad.x, pad.y, positions[ref]["rot"])
                        pin_xy.append((positions[ref]["x"] + dx, positions[ref]["y"] + dy))
                best = min((math.hypot(positions[c]["x"] - x, positions[c]["y"] - y), c)
                           for c in dec if c in positions for x, y in pin_xy) if pin_xy else None
                if best and best[0] > 6.0:
                    add("warning", "power-integrity",
                        f"nearest decoupling cap {best[1]} is {best[0]:.1f} mm from {ref} '{net}' pin (aim < 3 mm)", [ref, best[1]])

    # --- I2C pull-ups
    for net in d.nets:
        if re.search(r"(^|_)(SDA|SCL)($|_)", net.upper()):
            pulls = [r for r in d.parts if r.startswith("R") and net in part_nets(r)
                     and (part_nets(r) - {net}) and not (part_nets(r) & set(gnd))]
            if not pulls:
                add("error", "signal", f"I2C net {net} has no pull-up resistor")

    # --- USB-C sink CC resistors
    for ref, part in d.parts.items():
        if part.symbol and "USB_C" in part.symbol_id:
            for cc in ("CC1", "CC2"):
                pins = [p.number for p in part.symbol.pins if p.name == cc]
                if not pins:
                    continue
                net = pin_net.get((ref, pins[0]))
                rs = [r for r in d.parts if r.startswith("R") and net and net in part_nets(r) and part_nets(r) & set(gnd)]
                vals = [_val(d.parts[r].value) for r in rs]
                if not rs:
                    add("error", "usb", f"{ref} {cc} lacks a 5.1k pull-down: a USB-C charger will not supply 5 V", [ref])
                elif not any(v and 4.5e3 <= v <= 5.7e3 for v in vals):
                    add("warning", "usb", f"{ref} {cc} pull-down is {[d.parts[r].value for r in rs]}, expected 5.1k", rs)

    # --- custom symbols need a human datasheet cross-check
    for ref, part in d.parts.items():
        if part.symbol_id.split(":")[0] in {p.stem for p in (d.project_dir / "lib").glob("*.kicad_sym")}:
            add("warning", "library", f"{ref} uses AI-generated symbol {part.symbol_id}: verify pin mapping against datasheet", [ref])

    # --- custom footprints (drawn from a datasheet by the AI) need a land-pattern cross-check
    pretty = {p.stem for p in (d.project_dir / "lib").glob("*.pretty")}
    for ref, part in d.parts.items():
        if part.footprint_id.split(":")[0] in pretty:
            add("warning", "library", f"{ref} uses AI-generated footprint {part.footprint_id}: verify hole/pad "
                f"positions against the datasheet drawing (print 1:1 and test-fit a sample)", [ref])

    # --- BOM completeness
    missing = [r for r, p in d.parts.items() if p.footprint and not p.data.get("dnp")
               and not p.footprint_id.startswith("MountingHole") and not (p.data.get("lcsc") or p.data.get("mpn"))]
    if missing:
        add("warning", "bom", f"{len(missing)} parts lack LCSC/MPN: {', '.join(missing[:15])}", missing)

    # --- mechanical
    holes = [r for r, p in d.parts.items() if p.footprint_id.startswith("MountingHole")]
    if not holes:
        add("info", "mechanical", "no mounting holes defined")

    # --- routing
    if routes:
        st = routes.get("stats", {})
        for n in st.get("failed", []):
            add("error", "routing", f"net '{n}' could not be fully routed", [])
        widths = [s["w"] for s in routes.get("segments", [])]
        if widths and min(widths) < d.fab["min_track"]:
            add("error", "dfm", f"track width {min(widths)} < fab min {d.fab['min_track']}")
    # --- power net widths
    for n in d.rules["power_nets"]:
        if routes:
            segs = [s for s in routes.get("segments", []) if s["net"] == n]
            thin = [s for s in segs if s["w"] < d.net_width(n) - 1e-6]
            if thin:
                L = sum(math.hypot(s["x2"] - s["x1"], s["y2"] - s["y1"]) for s in thin)
                tot = sum(math.hypot(s["x2"] - s["x1"], s["y2"] - s["y1"]) for s in segs) or 1.0
                w = min(s["w"] for s in thin)
                if L <= 3.0 and L / tot < 0.1:
                    add("info", "power-integrity", f"power net {n}: {L:.1f} mm of {tot:.0f} mm necked to {w} mm "
                        f"(pad entry into fine-pitch pins)")
                else:
                    add("warning", "power-integrity", f"power net {n} has {L:.1f} mm of {tot:.0f} mm necked down to {w} mm")
    return f


def full_report(d: Design) -> dict:
    out = d.project_dir / "out"
    pos = json.loads((out / "placement.json").read_text())["positions"] if (out / "placement.json").exists() else None
    routes = json.loads((out / "routes.json").read_text()) if (out / "routes.json").exists() else None
    findings = design_checks(d, pos, routes)
    erc = json.loads((out / "erc_summary.json").read_text()) if (out / "erc_summary.json").exists() else None
    drc = json.loads((out / "drc_summary.json").read_text()) if (out / "drc_summary.json").exists() else None
    sims = json.loads((out / "sim" / "summary.json").read_text()) if (out / "sim" / "summary.json").exists() else []
    if erc:
        for v in erc.get("violations", []):
            if v["severity"] in ("error", "warning"):
                f_sev = v["severity"]
                findings.append({"severity": f_sev, "category": "erc", "message": f"{v['type']}: {v['description']} {v['items']}", "refs": []})
    if drc:
        for v in drc.get("violations", []) + drc.get("parity", []):
            if v["severity"] in ("error", "warning"):
                findings.append({"severity": v["severity"], "category": "drc", "message": f"{v['type']}: {v['description']} {v['items']}", "refs": []})
        for v in drc.get("unconnected_items", []):
            findings.append({"severity": "error", "category": "drc", "message": f"unconnected: {v['items']}", "refs": []})
        g = drc.get("geom") or {}
        if g.get("error"):
            findings.append({"severity": "error", "category": "drc", "message": f"geometric pour check failed to run: {g['error']}", "refs": []})
        for n in g.get("nets", []):
            for c in n["detached"]:
                what = f"pads {c['pads']}" if c["pads"] else f"dead copper {c['islands']}"
                findings.append({"severity": "error", "category": "drc",
                                 "message": f"geometric check: {n['net']} copper not attached to the main plane at {c['at']}: {what}",
                                 "refs": [p.split('.')[0] for p in c["pads"]]})
    for s in sims:
        if not s.get("ok"):
            findings.append({"severity": "error", "category": "simulation", "message": f"simulation {s['name']} failed: {s.get('error') or s.get('checks')}", "refs": []})
    counts = {k: sum(1 for x in findings if x["severity"] == k) for k in ("error", "warning", "info")}
    report = {"counts": counts, "auto_pass": counts["error"] == 0, "findings": findings,
              "erc": {k: erc[k] for k in ("ok", "errors", "warnings")} if erc else None,
              "drc": {k: drc.get(k) for k in ("ok", "errors", "warnings", "unconnected", "by_type")} if drc else None,
              "simulations": [{k: s.get(k) for k in ("name", "title", "ok", "measures", "checks")} for s in sims]}
    (out / "checks.json").write_text(json.dumps(report, indent=1))
    return report
