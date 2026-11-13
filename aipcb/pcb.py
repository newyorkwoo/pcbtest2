"""Write .kicad_pcb (+ .kicad_pro / lib tables) from Design + placement + routes."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

from .placement import ORIGIN, rot_pt
from .schematic import root_uuid, supply_nets, symbol_uuid, uid
from .sexpr import Sym, dumps, find, find_all, head
from .spec import Design

S = Sym

LAYER_DEFS = [
    (0, "F.Cu", "signal"), (2, "B.Cu", "signal"), (9, "F.Adhes", "user", "F.Adhesive"),
    (11, "B.Adhes", "user", "B.Adhesive"), (13, "F.Paste", "user"), (15, "B.Paste", "user"),
    (5, "F.SilkS", "user", "F.Silkscreen"), (7, "B.SilkS", "user", "B.Silkscreen"),
    (1, "F.Mask", "user"), (3, "B.Mask", "user"), (17, "Dwgs.User", "user", "User.Drawings"),
    (19, "Cmts.User", "user", "User.Comments"), (21, "Eco1.User", "user", "User.Eco1"),
    (23, "Eco2.User", "user", "User.Eco2"), (25, "Edge.Cuts", "user"), (27, "Margin", "user"),
    (31, "F.CrtYd", "user", "F.Courtyard"), (29, "B.CrtYd", "user", "B.Courtyard"),
    (35, "F.Fab", "user"), (33, "B.Fab", "user"),
]


def pcb_net(name: str, d: Design | None = None) -> str:
    """Net name as the schematic produces it: local labels on the root sheet give '/NAME', power symbols
    (supply rails) give the bare global name."""
    if d is not None and name in supply_nets(d):
        return name
    return name if name.startswith("/") else "/" + name


def _layers():
    out = [S("layers")]
    for t in LAYER_DEFS:
        out.append([t[0], t[1], S(t[2])] + ([t[3]] if len(t) > 3 else []))
    return out


def _setup(d: Design):
    return [S("setup"),
            [S("pad_to_mask_clearance"), 0],
            [S("allow_soldermask_bridges_in_footprints"), S("no")],
            [S("tenting"), S("front"), S("back")],
            [S("pcbplotparams"), [S("layerselection"), S("0x00000000_00000000_55555555_5755f5ff")],
             [S("plot_on_all_layers_selection"), S("0x00000000_00000000_00000000_00000000")],
             [S("usegerberextensions"), S("no")], [S("usegerberattributes"), S("yes")],
             [S("usegerberadvancedattributes"), S("yes")], [S("creategerberjobfile"), S("yes")],
             [S("svgprecision"), 4], [S("plotframeref"), S("no")], [S("mode"), 1], [S("useauxorigin"), S("no")],
             [S("outputformat"), 1], [S("mirror"), S("no")], [S("drillshape"), 0], [S("scaleselection"), 1],
             [S("outputdirectory"), "out/gerbers/"]]]


def _stroke(w=0.05):
    return [S("stroke"), [S("width"), w], [S("type"), S("default")]]


def _edge_cuts(d: Design) -> list:
    W, H = float(d.board["width"]), float(d.board["height"])
    r = float(d.board.get("corner_radius", 0))
    x0, y0, x1, y1 = ORIGIN, ORIGIN, ORIGIN + W, ORIGIN + H
    out = []
    k = 0

    def line(a, b, c, e):
        nonlocal k
        k += 1
        out.append([S("gr_line"), [S("start"), a, b], [S("end"), c, e], _stroke(0.1), [S("layer"), "Edge.Cuts"],
                    [S("uuid"), uid(d.name, "edge", k)]])

    def arc(s, m, e):
        nonlocal k
        k += 1
        out.append([S("gr_arc"), [S("start"), *s], [S("mid"), *m], [S("end"), *e], _stroke(0.1),
                    [S("layer"), "Edge.Cuts"], [S("uuid"), uid(d.name, "edge", k)]])
    if r <= 0:
        line(x0, y0, x1, y0); line(x1, y0, x1, y1); line(x1, y1, x0, y1); line(x0, y1, x0, y0)
        return out
    c = r * (1 - math.cos(math.pi / 4))
    line(x0 + r, y0, x1 - r, y0); line(x1, y0 + r, x1, y1 - r)
    line(x1 - r, y1, x0 + r, y1); line(x0, y1 - r, x0, y0 + r)
    arc((x1 - r, y0), (x1 - c, y0 + c), (x1, y0 + r))
    arc((x1, y1 - r), (x1 - c, y1 - c), (x1 - r, y1))
    arc((x0 + r, y1), (x0 + c, y1 - c), (x0, y1 - r))
    arc((x0, y0 + r), (x0 + c, y0 + c), (x0 + r, y0))
    return out


def nc_net_names(d: Design) -> dict:
    """KiCad names every intentionally-unconnected pin's net 'unconnected-(REF-PIN-PadN)'."""
    out = {}
    for ref, num in sorted(d.nc):
        part = d.parts[ref]
        pin = next((p for p in part.symbol.pins if p.number == num), None)
        nm = f"{ref}-{pin.name}-Pad{num}" if pin and pin.name and pin.name != num else f"{ref}-Pad{num}"
        out[(ref, num)] = f"unconnected-({nm})"
    return out


def _footprint(d: Design, ref: str, pos: dict, net_num: dict, pin_net: dict, nc_nets: dict) -> list:
    part = d.parts[ref]
    fp = part.footprint
    node = copy.deepcopy(fp.node)
    x, y, rot = pos["x"], pos["y"], pos["rot"]
    out = [S("footprint"), part.footprint_id, [S("layer"), "F.Cu"], [S("uuid"), uid(d.name, "fp", ref)],
           [S("at"), round(x, 4), round(y, 4)] + ([rot] if rot else [])]
    skip = {"version", "generator", "generator_version", "layer", "tedit", "uuid"}
    for c in node[2:]:
        h = head(c)
        if h in skip or h is None:
            continue
        if h == "property":
            key = c[1]
            if key == "Reference":
                c[2] = ref
            elif key == "Value":
                c[2] = part.value
            elif key == "Footprint":
                c[2] = part.footprint_id
            at = find(c, "at")
            if at is not None:
                ang = (at[3] if len(at) > 3 else 0) + rot
                del at[3:]
                if ang % 360:
                    at.append(ang % 360)
            if find(c, "uuid") is None:
                c.append([S("uuid"), uid(d.name, "fpprop", ref, key)])
        elif h in ("fp_text",):
            at = find(c, "at")
            if at is not None:
                ang = (at[3] if len(at) > 3 else 0) + rot
                del at[3:]
                if ang % 360:
                    at.append(ang % 360)
        elif h == "pad":
            at = find(c, "at")
            ang = (at[3] if len(at) > 3 else 0) + rot
            del at[3:]
            if ang % 360:
                at.append(ang % 360)
            net = pin_net.get((ref, str(c[1])))
            c[:] = [x for x in c if head(x) not in ("net", "uuid")]
            if net is None and (ref, str(c[1])) in nc_nets:
                c.append([S("net"), net_num[nc_nets[(ref, str(c[1]))]], nc_nets[(ref, str(c[1]))]])
            if net is not None:
                c.append([S("net"), net_num[net], pcb_net(net, d)])
                pin = next((p for p in part.symbol.pins if p.number == str(c[1])), None)
                if pin:
                    c.append([S("pinfunction"), pin.name])
                    c.append([S("pintype"), pin.etype])
            c.append([S("uuid"), uid(d.name, "pad", ref, c[1], at[1], at[2])])
        elif h == "model":
            p = str(c[1])
            for v in ("${KICAD6_3DMODEL_DIR}", "${KICAD7_3DMODEL_DIR}", "${KICAD8_3DMODEL_DIR}", "${KICAD9_3DMODEL_DIR}"):
                p = p.replace(v, "${KICAD10_3DMODEL_DIR}")
            c[1] = p
        elif h == "zone":
            # footprint keepouts: rotate/translate polygon into board coords
            poly = find(c, "polygon")
            if poly is not None:
                for xy in find_all(find(poly, "pts"), "xy"):
                    dx, dy = rot_pt(xy[1], xy[2], rot)
                    xy[1], xy[2] = round(x + dx, 4), round(y + dy, 4)
            c[:] = [k for k in c if head(k) != "uuid"] + [[S("uuid"), uid(d.name, "fpzone", ref)]]
        out.append(c)
    # keep footprint fields identical to the schematic symbol (schematic-parity DRC)
    sym_props = part.symbol.props if part.symbol else {}
    fields = {"Datasheet": part.data.get("datasheet", sym_props.get("Datasheet", "~")),
              "Description": part.data.get("description", sym_props.get("Description", ""))}
    for k, key in (("MPN", "mpn"), ("LCSC", "lcsc"), ("Manufacturer", "manufacturer")):
        if part.data.get(key):
            fields[k] = str(part.data[key])
    out = [c for c in out if not (head(c) == "property" and c[1] in fields)]
    for k, v in fields.items():
        out.append([S("property"), k, str(v), [S("at"), 0, 0, 0], [S("layer"), "F.Fab"], [S("hide"), S("yes")],
                    [S("uuid"), uid(d.name, "fpprop", ref, k)],
                    [S("effects"), [S("font"), [S("size"), 1, 1], [S("thickness"), 0.15]]]])
    out.append([S("path"), f"/{symbol_uuid(d, ref)}"])
    out.append([S("sheetname"), "/"])
    out.append([S("sheetfile"), f"{d.name}.kicad_sch"])
    if part.data.get("dnp"):
        out.append([S("attr"), S("smd"), S("dnp")])
    return out


def _zone(d: Design, net: str, num: int, layer: str) -> list:
    W, H = float(d.board["width"]), float(d.board["height"])
    m = 0.0
    pts = [(ORIGIN + m, ORIGIN + m), (ORIGIN + W - m, ORIGIN + m), (ORIGIN + W - m, ORIGIN + H - m), (ORIGIN + m, ORIGIN + H - m)]
    return [S("zone"), [S("net"), num], [S("net_name"), pcb_net(net, d)], [S("layer"), layer], [S("uuid"), uid(d.name, "zone", net, layer)],
            [S("hatch"), S("edge"), 0.5], [S("priority"), 0],
            [S("connect_pads"), [S("clearance"), 0.25]], [S("min_thickness"), 0.2], [S("filled_areas_thickness"), S("no")],
            [S("fill"), S("yes"), [S("thermal_gap"), 0.3], [S("thermal_bridge_width"), 0.3], [S("island_removal_mode"), 0]],
            [S("polygon"), [S("pts")] + [[S("xy"), round(a, 3), round(b, 3)] for a, b in pts]]]


def write_project_file(d: Design) -> Path:
    r = d.rules
    fab = d.fab
    pro = {
        "board": {"design_settings": {
            "defaults": {"board_outline_line_width": 0.1, "copper_line_width": 0.2, "silk_line_width": 0.12,
                         "silk_text_size_h": 1.0, "silk_text_size_v": 1.0, "silk_text_thickness": 0.15},
            "rules": {"min_clearance": fab["min_clearance"], "min_track_width": fab["min_track"],
                      "min_via_diameter": fab["min_via_dia"], "min_via_annular_width": fab["min_annular"],
                      "min_through_hole_diameter": fab["min_hole"], "min_copper_edge_clearance": fab["edge_clearance"],
                      "min_hole_to_hole": 0.25, "min_hole_clearance": 0.25, "min_silk_clearance": 0.0,
                      "min_text_height": fab["min_silk_h"], "min_text_thickness": fab["min_silk_w"],
                      "solder_mask_to_copper_clearance": 0.0, "min_microvia_diameter": 0.2, "min_microvia_drill": 0.1},
            "track_widths": [0.0] + sorted({r["track"], r["power_track"], *map(float, (r.get("net_widths") or {}).values())}),
            "via_dimensions": [{"diameter": 0.0, "drill": 0.0}, {"diameter": r["via"][0], "drill": r["via"][1]}],
            "rule_severities": {"silk_overlap": "ignore", "silk_over_copper": "warning", "silk_edge_clearance": "ignore",
                                "lib_footprint_mismatch": "ignore", "lib_footprint_issues": "ignore",
                                "text_height": "warning", "text_thickness": "warning", "courtyards_overlap": "error",
                                "footprint_type_mismatch": "ignore", "isolated_copper": "warning",
                                "starved_thermal": "ignore", "via_dangling": "warning", "track_dangling": "warning"},
        }},
        "meta": {"filename": f"{d.name}.kicad_pro", "version": 3},
        "net_settings": {"classes": [{
            "name": "Default", "clearance": r["clearance"], "track_width": r["track"],
            "via_diameter": r["via"][0], "via_drill": r["via"][1], "microvia_diameter": 0.3, "microvia_drill": 0.1,
            "diff_pair_width": 0.2, "diff_pair_gap": 0.25, "wire_width": 6, "bus_width": 12, "line_style": 0,
            "pcb_color": "rgba(0, 0, 0, 0.000)", "schematic_color": "rgba(0, 0, 0, 0.000)", "priority": 2147483647}],
            "meta": {"version": 4}},
        "schematic": {"drawing": {"default_line_thickness": 6.0}, "legacy_lib_dir": "", "legacy_lib_list": []},
        "erc": {"rule_severities": {"lib_symbol_issues": "ignore", "lib_symbol_mismatch": "ignore",
                                    "footprint_link_issues": "ignore", "simulation_model_issue": "ignore"}},
        "sheets": [[root_uuid(d), "Root"]],
    }
    p = d.project_dir / f"{d.name}.kicad_pro"
    p.write_text(json.dumps(pro, indent=2))
    rules = ["(version 1)"]
    for ref, part in d.parts.items():
        fp = part.footprint
        if not fp:
            continue
        gap = 9.0
        pads = [p for p in fp.pads if p.copper and p.number]
        for i, a in enumerate(pads):
            aw, ah = (a.h, a.w) if int(a.angle) % 180 == 90 else (a.w, a.h)
            for b in pads[i + 1:]:
                if a.number == b.number:
                    continue
                bw, bh = (b.h, b.w) if int(b.angle) % 180 == 90 else (b.w, b.h)
                g = max(abs(a.x - b.x) - (aw + bw) / 2, abs(a.y - b.y) - (ah + bh) / 2)
                gap = min(gap, g)
        if gap < r["clearance"] - 1e-6:
            rules.append(f"""(rule "fine_pitch_{ref}"
  (constraint clearance (min {max(fab['min_clearance'], round(gap - 0.005, 3))}mm))
  (condition "A.Type == 'Pad' && B.Type == 'Pad' && A.memberOfFootprint('{ref}') && B.memberOfFootprint('{ref}')"))""")
    (d.project_dir / f"{d.name}.kicad_dru").write_text("\n".join(rules) + "\n")
    lib = d.project_dir / "lib"
    syms = sorted(lib.glob("*.kicad_sym")) if lib.exists() else []
    fps = sorted(lib.glob("*.pretty")) if lib.exists() else []
    (d.project_dir / "sym-lib-table").write_text(
        "(sym_lib_table\n  (version 7)\n" + "".join(
            f'  (lib (name "{s.stem}")(type "KiCad")(uri "${{KIPRJMOD}}/lib/{s.name}")(options "")(descr "aipcb project lib"))\n'
            for s in syms) + ")\n")
    (d.project_dir / "fp-lib-table").write_text(
        "(fp_lib_table\n  (version 7)\n" + "".join(
            f'  (lib (name "{f.stem}")(type "KiCad")(uri "${{KIPRJMOD}}/lib/{f.name}")(options "")(descr "aipcb project lib"))\n'
            for f in fps) + ")\n")
    return p


def generate(d: Design, positions: dict, routes: dict | None, out_path: Path | None = None,
             zones: bool = True) -> Path:
    out_path = Path(out_path or d.project_dir / f"{d.name}.kicad_pcb")
    net_names = list(d.nets)
    net_num = {n: i + 1 for i, n in enumerate(net_names)}
    pin_net = d.pin_net()
    tree = [S("kicad_pcb"), [S("version"), 20241229], [S("generator"), "pcbnew"], [S("generator_version"), "9.0"],
            [S("general"), [S("thickness"), float(d.board.get("thickness", 1.6))], [S("legacy_teardrops"), S("no")]],
            [S("paper"), "A4"],
            [S("title_block"), [S("title"), d.raw.get("title", d.name)], [S("rev"), str(d.raw.get("rev", "A"))]],
            _layers(), _setup(d), [S("net"), 0, ""]]
    tree += [[S("net"), net_num[n], pcb_net(n, d)] for n in net_names]
    nc_nets = nc_net_names(d)
    for k, nm in enumerate(sorted(set(nc_nets.values()))):
        net_num[nm] = len(net_names) + 1 + k
        tree.append([S("net"), net_num[nm], nm])
    for ref in d.parts:
        if ref in positions and d.parts[ref].footprint:
            tree.append(_footprint(d, ref, positions[ref], net_num, pin_net, nc_nets))
    tree += _edge_cuts(d)
    # silkscreen title
    if d.raw.get("silk_text", True):
        tree.append([S("gr_text"), f"{d.raw.get('title', d.name)} rev {d.raw.get('rev', 'A')}",
                     [S("at"), ORIGIN + 2.5, ORIGIN + float(d.board["height"]) - 2.0, 0], [S("layer"), "B.SilkS"],
                     [S("uuid"), uid(d.name, "title")],
                     [S("effects"), [S("font"), [S("size"), 1, 1], [S("thickness"), 0.15]],
                      [S("justify"), S("left"), S("bottom"), S("mirror")]]])
    if routes:
        for k, s in enumerate(routes.get("segments", [])):
            tree.append([S("segment"), [S("start"), s["x1"], s["y1"]], [S("end"), s["x2"], s["y2"]], [S("width"), s["w"]],
                         [S("layer"), s["layer"]], [S("net"), net_num[s["net"]]], [S("uuid"), uid(d.name, "seg", k)]])
        for k, v in enumerate(routes.get("vias", [])):
            tree.append([S("via"), [S("at"), v["x"], v["y"]], [S("size"), v["d"]], [S("drill"), v["drill"]],
                         [S("layers"), "F.Cu", "B.Cu"], [S("net"), net_num[v["net"]]], [S("uuid"), uid(d.name, "via", k)]])
    if zones:
        for n in net_names:
            if n.upper() in ("GND",):
                tree.append(_zone(d, n, net_num[n], "B.Cu"))
                tree.append(_zone(d, n, net_num[n], "F.Cu"))
    tree.append([S("embedded_fonts"), S("no")])
    out_path.write_text(dumps(tree) + "\n", encoding="utf-8")
    write_project_file(d)
    return out_path
