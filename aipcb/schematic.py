"""Generate a KiCad schematic (.kicad_sch) from a Design.

Every pin gets a net label at its connection point (no wire spaghetti), parts are
grouped into framed functional blocks, PWR_FLAGs and no-connect markers are added.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from . import library
from .sexpr import Sym, dumps, find
from .spec import Design

GRID = 1.27
NS = uuid.UUID("6f1c1f7e-8a40-4b8f-9d1c-3b8f2a8e0c11")
LABEL_CH = 1.05     # approx label char width (mm) at 1.27 font


def uid(*parts) -> str:
    return str(uuid.uuid5(NS, "/".join(str(p) for p in parts)))


def snap(v: float) -> float:
    return round(round(v / GRID) * GRID, 4)


def S(x):
    return Sym(x)


def _effects(justify: str | None = None, hide=False, size=1.27):
    e = [S("effects"), [S("font"), [S("size"), size, size]]]
    if justify:
        e.append([S("justify")] + [S(j) for j in justify.split()])
    if hide:
        e.append([S("hide"), S("yes")])
    return e


def _label_dir(pin_angle: int) -> int:
    return {0: 180, 180: 0, 90: 270, 270: 90}.get(pin_angle % 360, 0)


class _Inst:
    def __init__(self, ref, part, unit, sym):
        self.ref, self.part, self.unit, self.sym = ref, part, unit, sym
        self.pins = [p for p in sym.pins if p.unit in (0, unit)]
        xmin, ymin, xmax, ymax = sym.bbox(unit)
        self.box = (xmin, ymin, xmax, ymax)
        self.lab = {"l": 0.0, "r": 0.0, "t": 0.0, "b": 0.0}
        self.x = self.y = 0.0
        self.sides: set = set()
        self.text_side = None

    def measure(self, pin_net):
        sides = set()
        for p in self.pins:
            net = pin_net.get((self.ref, p.number))
            ln = (len(net) * LABEL_CH + 2.0) if net else 2.0
            side = {0: "l", 180: "r", 90: "b", 270: "t"}.get(p.angle % 360, "l")
            sides.add(side)
            self.lab[side] = max(self.lab[side], ln)
        # vertical pin labels run straight through text placed above/below the body, so a part with top/bottom
        # pins carries its reference/value on a side that has no pins (resistors, caps, FETs, diodes...)
        self.sides = sides
        self.text_side = None
        if sides & {"t", "b"}:
            self.text_side = "r" if "r" not in sides else ("l" if "l" not in sides else None)
        tw = max(len(self.ref), len(str(self.part.value or ""))) * LABEL_CH + 2.0
        if self.text_side:
            self.lab[self.text_side] = max(self.lab[self.text_side], tw)
        else:
            # centred reference/value wider than the body: reserve the overhang on both sides
            pad = (tw - (self.box[2] - self.box[0])) / 2
            if pad > 0:
                self.lab["l"] = max(self.lab["l"], pad)
                self.lab["r"] = max(self.lab["r"], pad)

    @property
    def w(self):
        return (self.box[2] - self.box[0]) + self.lab["l"] + self.lab["r"] + 6

    @property
    def h(self):
        return (self.box[3] - self.box[1]) + self.lab["t"] + self.lab["b"] + 11


def generate(d: Design, out_path: Path | None = None) -> Path:
    library.set_project_libs(d.project_dir)
    out_path = Path(out_path or d.project_dir / f"{d.name}.kicad_sch")
    root = uid(d.name, "root")
    pin_net = d.pin_net()

    # ---------------- instances grouped per block
    blocks: dict[str, list[_Inst]] = {}
    for ref, part in d.parts.items():
        if not part.symbol:
            continue
        for u in range(1, part.symbol.units + 1):
            inst = _Inst(ref, part, u, part.symbol)
            if part.symbol.units > 1 and not inst.pins:
                continue
            inst.measure(pin_net)
            blocks.setdefault(part.block, []).append(inst)

    # power flags: nets with power_in pins but no power_out driver
    flags = []
    for net, pins in d.nets.items():
        types = set()
        for ref, num in pins:
            part = d.parts[ref]
            for p in part.symbol.pins:
                if p.number == num:
                    types.add(p.etype)
        if "power_in" in types and "power_out" not in types:
            flags.append(net)

    # ---------------- layout: rows inside blocks, shelf-pack blocks
    block_rects = []
    for name, insts in blocks.items():
        insts.sort(key=lambda i: -(i.box[3] - i.box[1]))
        max_row_w = max(160.0, max(i.w for i in insts) + 10)
        x = y = 0.0
        row_h = 0.0
        bw = 0.0
        for inst in insts:
            if x > 0 and x + inst.w > max_row_w:
                x = 0.0; y += row_h; row_h = 0.0
            inst.x = x + inst.lab["l"] + 3 - inst.box[0]
            inst.y = y + inst.lab["t"] + 6 + inst.box[3]
            x += inst.w
            bw = max(bw, x)
            row_h = max(row_h, inst.h)
        block_rects.append([name, insts, bw + 4, y + row_h + 4])

    margin = 20.0
    papers = (("A4", (297, 210)), ("A3", (420, 297)), ("A2", (594, 420)), ("A1", (841, 594)), ("A0", (1189, 841)))

    def pack(sheet_w):
        bx = by = 0.0
        shelf_h = 0.0
        out = []
        for name, insts, w, h in sorted(block_rects, key=lambda b: -b[3]):
            if bx > 0 and bx + w > sheet_w:
                bx = 0.0; by += shelf_h + 8; shelf_h = 0.0
            out.append((name, insts, margin + bx, margin + by, w, h))
            bx += w + 8
            shelf_h = max(shelf_h, h)
        uw = max(p[2] + p[4] for p in out) + margin
        uh = by + shelf_h + margin * 2 + 20
        k = next((i for i, (_, (pw, ph)) in enumerate(papers) if uw <= pw and uh <= ph), len(papers) - 1)
        return (k, uw * uh), out, uw, uh, k

    # shelf-pack at several widths and keep the smallest paper (then the tightest fit): landscape sheets
    # are filled instead of stacking every block in one tall column
    widest = max(w for _, _, w, _ in block_rects)
    best = min((pack(sw) for sw in [widest] + [float(v) for v in range(200, 1180, 20) if v > widest]),
               key=lambda r: r[0])
    _, placed, used_w, used_h, k = best
    paper = papers[k][0]

    # ---------------- emit
    lib_syms = {}
    items = []
    for name, insts, ox, oy, w, h in placed:
        items.append([S("rectangle"), [S("start"), snap(ox), snap(oy)], [S("end"), snap(ox + w), snap(oy + h)],
                      [S("stroke"), [S("width"), 0.2], [S("type"), S("dash")]], [S("fill"), [S("type"), S("none")]],
                      [S("uuid"), uid(d.name, "blk", name)]])
        items.append([S("text"), name.upper().replace("_", " "), [S("exclude_from_sim"), S("no")],
                      [S("at"), snap(ox + 2), snap(oy + 4), 0],
                      _effects("left bottom", size=2.0), [S("uuid"), uid(d.name, "blkt", name)]])
        for inst in insts:
            sx, sy = snap(ox + inst.x), snap(oy + inst.y)
            part, sym = inst.part, inst.sym
            lib_syms[sym.lib_id] = sym.node
            xmin, ymin, xmax, ymax = inst.box
            u = uid(d.name, "sym", inst.ref, inst.unit)
            ib = find(sym.node, "in_bom")
            fp_attr = find(part.footprint.node, "attr") if part.footprint else None
            fp_excl = fp_attr is not None and any(str(a) == "exclude_from_bom" for a in fp_attr[1:])
            in_bom = "no" if fp_excl or (ib is not None and str(ib[1]) == "no") else "yes"
            node = [S("symbol"), [S("lib_id"), sym.lib_id], [S("at"), sx, sy, 0], [S("unit"), inst.unit],
                    [S("exclude_from_sim"), S("no")], [S("in_bom"), S(in_bom)], [S("on_board"), S("yes")],
                    [S("dnp"), S("yes" if part.data.get("dnp") else "no")], [S("uuid"), u]]

            def prop(k, v, x, y, just=None, hide=False):
                return [S("property"), k, str(v), [S("at"), snap(x), snap(y), 0], _effects(just, hide)]

            cy = sy - (ymax + ymin) / 2
            if inst.text_side == "r":
                node.append(prop("Reference", inst.ref, sx + xmax + 1.5, cy - 1.27, "left"))
                node.append(prop("Value", part.value, sx + xmax + 1.5, cy + 1.27, "left"))
            elif inst.text_side == "l":
                node.append(prop("Reference", inst.ref, sx + xmin - 1.5, cy - 1.27, "right"))
                node.append(prop("Value", part.value, sx + xmin - 1.5, cy + 1.27, "right"))
            else:
                # centred over / under the body, beyond any vertical pin labels
                cx = sx + (xmin + xmax) / 2
                top = inst.lab["t"] if "t" in inst.sides else 0.0
                bot = inst.lab["b"] if "b" in inst.sides else 0.0
                node.append(prop("Reference", inst.ref, cx, sy - ymax - top - 1.8))
                node.append(prop("Value", part.value, cx, sy - ymin + bot + 2.2))
            node.append(prop("Footprint", part.footprint_id, sx, sy, hide=True))
            node.append(prop("Datasheet", part.data.get("datasheet", sym.props.get("Datasheet", "~")), sx, sy, hide=True))
            node.append(prop("Description", part.data.get("description", sym.props.get("Description", "")), sx, sy, hide=True))
            for k, key in (("MPN", "mpn"), ("LCSC", "lcsc"), ("Manufacturer", "manufacturer")):
                if part.data.get(key):
                    node.append(prop(k, part.data[key], sx, sy, hide=True))
            for p in sym.pins:
                node.append([S("pin"), p.number, [S("uuid"), uid(d.name, "pin", inst.ref, p.number)]])
            node.append([S("instances"), [S("project"), d.name, [S("path"), f"/{root}",
                         [S("reference"), inst.ref], [S("unit"), inst.unit]]]])
            items.append(node)

            for p in inst.pins:
                px, py = snap(sx + p.x), snap(sy - p.y)
                key = (inst.ref, p.number)
                net = pin_net.get(key)
                if net:
                    ang = _label_dir(p.angle)
                    just = "right bottom" if ang in (180, 270) else "left bottom"
                    items.append([S("label"), net, [S("at"), px, py, ang], [S("fields_autoplaced"), S("yes")],
                                  _effects(just), [S("uuid"), uid(d.name, "lbl", inst.ref, p.number)]])
                elif key in d.nc:
                    items.append([S("no_connect"), [S("at"), px, py], [S("uuid"), uid(d.name, "nc", inst.ref, p.number)]])

    # PWR_FLAGs along the bottom
    if flags:
        flag_sym = library.get_symbol("power:PWR_FLAG")
        lib_syms[flag_sym.lib_id] = flag_sym.node
        fy = snap(used_h - margin - 10)
        for i, net in enumerate(flags):
            fx = snap(margin + 10 + i * max(25.0, len(net) * LABEL_CH + 12))
            ref = f"#FLG{i + 1:02d}"
            items.append([S("symbol"), [S("lib_id"), "power:PWR_FLAG"], [S("at"), fx, fy, 0], [S("unit"), 1],
                          [S("exclude_from_sim"), S("no")], [S("in_bom"), S("yes")], [S("on_board"), S("yes")],
                          [S("dnp"), S("no")], [S("uuid"), uid(d.name, "flag", net)],
                          [S("property"), "Reference", ref, [S("at"), fx, snap(fy + 2.54), 0], _effects(hide=True)],
                          [S("property"), "Value", "PWR_FLAG", [S("at"), fx, snap(fy - 3.81), 0], _effects()],
                          [S("property"), "Footprint", "", [S("at"), fx, fy, 0], _effects(hide=True)],
                          [S("property"), "Datasheet", "~", [S("at"), fx, fy, 0], _effects(hide=True)],
                          [S("property"), "Description", "", [S("at"), fx, fy, 0], _effects(hide=True)],
                          [S("pin"), "1", [S("uuid"), uid(d.name, "flagpin", net)]],
                          [S("instances"), [S("project"), d.name, [S("path"), f"/{root}",
                           [S("reference"), ref], [S("unit"), 1]]]]])
            items.append([S("wire"), [S("pts"), [S("xy"), fx, fy], [S("xy"), fx, snap(fy + 2.54)]],
                          [S("stroke"), [S("width"), 0], [S("type"), S("default")]], [S("uuid"), uid(d.name, "flagw", net)]])
            items.append([S("label"), net, [S("at"), fx, snap(fy + 2.54), 270], [S("fields_autoplaced"), S("yes")],
                          _effects("right bottom"), [S("uuid"), uid(d.name, "flagl", net)]])

    tb = [S("title_block"), [S("title"), d.raw.get("title", d.name)], [S("date"), d.raw.get("date", "")],
          [S("rev"), str(d.raw.get("rev", "A"))], [S("company"), d.raw.get("company", "")],
          [S("comment"), 1, "Generated by aipcb (Claude-driven EDA)"]]
    tree = [S("kicad_sch"), [S("version"), 20250114], [S("generator"), "eeschema"], [S("generator_version"), "9.0"],
            [S("uuid"), root], [S("paper"), paper], tb,
            [S("lib_symbols")] + list(lib_syms.values())] + items + [
            [S("sheet_instances"), [S("path"), "/", [S("page"), "1"]]], [S("embedded_fonts"), S("no")]]
    out_path.write_text(dumps(tree) + "\n", encoding="utf-8")
    return out_path


def symbol_uuid(d: Design, ref: str) -> str:
    return uid(d.name, "sym", ref, 1)


def root_uuid(d: Design) -> str:
    return uid(d.name, "root")
