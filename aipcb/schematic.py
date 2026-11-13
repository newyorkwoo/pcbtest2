"""Generate a wired KiCad schematic (.kicad_sch) from a Design.

Drawing rules (what a human would draw):
  * every part with 3+ pins is a *hub*; the 2-pin parts (R, C, L, D, LED, crystal...) that hang off one of its
    pins are placed next to that pin and joined to it with real wires, chains like R -> LED -> GND continue in
    the same column;
  * supply rails (GND, +3V3, VBUS, VSYS...) are drawn with KiCad power symbols, never with wires across the sheet;
  * decoupling capacitors (both pins on supply rails) sit side by side on a shared pair of rail wires;
  * a net label is used only where a connection leaves the drawn group (other block / other hub);
  * every placement is collision checked (parts, text, labels, wires of other nets never touch).

After writing, `verify()` exports KiCad's own netlist of the drawing and compares it pin by pin with design.yaml,
so a misplaced wire cannot slip through.
"""
from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path

from . import config, library
from .sexpr import Sym, dumps, find, parse
from .spec import Design

GRID = 1.27
P = 2.54
NS = uuid.UUID("6f1c1f7e-8a40-4b8f-9d1c-3b8f2a8e0c11")
CH = 1.2             # approx char width (mm) at 1.27 font
TXT_H = 1.6

# sheet-direction vectors for y-up angles (0 right, 90 up, 180 left, 270 down); sheet y grows downward
DIR = {0: (1, 0), 90: (0, -1), 180: (-1, 0), 270: (0, 1)}


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


def supply_nets(d: Design) -> set[str]:
    """Nets drawn with power symbols (global nets named without the '/' sheet prefix).
    `rules.supply_nets` overrides; otherwise GND*, +rails, and power_nets that look like rails (VBUS, AMP_5V)."""
    if d.rules.get("supply_nets") is not None:
        return set(d.rules["supply_nets"]) & set(d.nets)
    out = set()
    for n in d.nets:
        if re.match(r"^[ADP]?GND", n) or n.startswith("+"):
            out.add(n)
        elif n in d.rules.get("power_nets", []) and re.match(r"^(V[A-Z0-9]*|[A-Z0-9]+_\d+V\d*)$", n):
            out.add(n)
    return out


def is_ground(net: str) -> bool:
    return bool(re.match(r"^[ADP]?GND", net))


def _rot(x, y, r):
    """rotate symbol coords (y up) counter-clockwise by r degrees"""
    r %= 360
    if r == 0:
        return x, y
    if r == 90:
        return -y, x
    if r == 180:
        return -x, -y
    return y, -x


class Rects:
    """Occupancy of one cluster: solid boxes plus wires (which may touch wires of the same net)."""

    def __init__(self):
        self.solid = []     # (x0, y0, x1, y1)
        self.wires = []     # (x0, y0, x1, y1, net)
        self.points = []    # connection points (x, y, net): another net's wire must not pass over them

    @staticmethod
    def _hit(a, b, m=0.0):
        return a[0] < b[2] + m and b[0] < a[2] + m and a[1] < b[3] + m and b[1] < a[3] + m

    def free(self, solids=(), wires=()):
        for s in solids:
            if any(self._hit(s, o, 0.3) for o in self.solid):
                return False
            if any(s[0] + 0.05 < x < s[2] - 0.05 and s[1] + 0.05 < y < s[3] - 0.05 for x, y, _ in self.points):
                return False
            if any(self._hit(s, w[:4], 0.2) for w in self.wires):
                return False
        for w in wires:
            box = (min(w[0], w[2]), min(w[1], w[3]), max(w[0], w[2]), max(w[1], w[3]))
            if any(self._hit(box, o, 0.15) for o in self.solid):
                return False
            if any(o[4] != w[4] and self._hit(box, o[:4], 0.6) for o in self.wires):
                return False
            if any(n != w[4] and box[0] - 0.6 <= x <= box[2] + 0.6 and box[1] - 0.6 <= y <= box[3] + 0.6
                   for x, y, n in self.points):
                return False
        return True

    def add(self, solids=(), wires=(), points=()):
        self.solid += list(solids)
        for w in wires:
            self.wires.append((min(w[0], w[2]), min(w[1], w[3]), max(w[0], w[2]), max(w[1], w[3]), w[4]))
        self.points += list(points)

    def bbox(self):
        xs = [r[0] for r in self.solid + self.wires] + [r[2] for r in self.solid + self.wires]
        ys = [r[1] for r in self.solid + self.wires] + [r[3] for r in self.solid + self.wires]
        return (min(xs), min(ys), max(xs), max(ys)) if xs else (0, 0, 0, 0)


class Cluster:
    """A group drawn together: symbols, wires, labels, power symbols, no-connects (local coordinates)."""

    def __init__(self, name):
        self.name = name
        self.syms = []      # dict(inst, x, y, rot, text)
        self.wires = []     # (x1, y1, x2, y2, net)
        self.labels = []    # (net, x, y, dir)
        self.power = []     # (net, x, y, dir)
        self.nc = []        # (x, y)
        self.occ = Rects()


class _Inst:
    def __init__(self, ref, part, unit, sym):
        self.ref, self.part, self.unit, self.sym = ref, part, unit, sym
        self.pins = [p for p in sym.pins if p.unit in (0, unit)]
        self.box = sym.bbox(unit)

    def pin_at(self, p, x, y, rot):
        """(sheet x, sheet y, outward dir) of pin p for the symbol placed at (x, y) rotated rot"""
        dx, dy = _rot(p.x, p.y, rot)
        return snap(x + dx), snap(y - dy), (p.angle + 180 + rot) % 360

    def body(self, x, y, rot):
        xs, ys = [], []
        for cx, cy in ((self.box[0], self.box[1]), (self.box[2], self.box[3]),
                       (self.box[0], self.box[3]), (self.box[2], self.box[1])):
            rx, ry = _rot(cx, cy, rot)
            xs.append(x + rx); ys.append(y - ry)
        return min(xs), min(ys), max(xs), max(ys)


def _inline(part) -> bool:
    """a 2-pin part drawn as a straight element (R, C, L, D, LED, crystal): pins at opposite ends"""
    sym = part.symbol
    if not sym or sym.units != 1 or len(sym.pins) != 2:
        return False
    a, b = sym.pins
    return (a.angle - b.angle) % 360 == 180


def _text_w(s) -> float:
    return len(str(s)) * CH


class Builder:
    def __init__(self, d: Design):
        self.d = d
        self.pin_net = d.pin_net()
        self.supply = supply_nets(d)
        self.placed: set[str] = set()       # refs drawn (single-unit parts) / ref:unit
        self.wired: dict[str, set] = {}     # net -> pins drawn in the cluster that owns the net's wiring

    # ------------------------------------------------------------- terminal at a pin end
    def terminal(self, cl: Cluster, net, x, y, dirn, key, need_label=True, commit=True):
        """What sits at a free pin end: power symbol, label, or no-connect. Returns the solids it occupies."""
        vx, vy = DIR[dirn]
        if net is None:
            if key in self.d.nc:
                if commit:
                    cl.nc.append((x, y))
            return []
        if net in self.supply:
            L = 3.6
            tw = _text_w(net)
            if dirn in (90, 270):
                tip = y + vy * (L + TXT_H)
                box = (x - max(1.6, tw / 2), min(y + vy * 0.4, tip), x + max(1.6, tw / 2), max(y + vy * 0.4, tip))
            else:
                tip = x + vx * (L + tw + 0.6)
                box = (min(x + vx * 0.4, tip), y - 1.6, max(x + vx * 0.4, tip), y + 1.6)
            if commit:
                cl.power.append((net, x, y, dirn))
            return [box]
        if not need_label:
            return []
        tw = _text_w(net) + 1.0
        if dirn == 0:
            box = (x + 0.3, y - TXT_H, x + tw, y + 0.3)
        elif dirn == 180:
            box = (x - tw, y - TXT_H, x - 0.3, y + 0.3)
        elif dirn == 90:
            box = (x - TXT_H, y - tw, x + 0.3, y - 0.3)
        else:
            box = (x - TXT_H, y + 0.3, x + 0.3, y + tw)
        if commit:
            cl.labels.append((net, x, y, dirn))
        return [box]

    def text_box(self, inst, x, y, rot, side):
        b = inst.body(x, y, rot)
        tw = max(_text_w(inst.ref), _text_w(inst.part.value or "")) + 0.5
        cy = (b[1] + b[3]) / 2
        if side == "r":
            return (b[2] + 0.8, cy - 2.6, b[2] + 1.2 + tw, cy + 2.4)
        return (b[0] - 1.2 - tw, cy - 2.6, b[0] - 0.8, cy + 2.4)

    # ------------------------------------------------------------- hubs
    def satellites(self, block, net, exclude):
        """unplaced 2-pin parts of `block` with a pin on `net`"""
        out = []
        if net is None or net in self.supply:
            return out
        for ref, pin in self.d.nets.get(net, []):
            if ref in exclude or ref in self.placed:
                continue
            part = self.d.parts[ref]
            if part.block != block or not _inline(part):
                continue
            if ref not in [o[0] for o in out]:
                out.append((ref, pin))
        return out

    def place_chain(self, cl, ref, pin_num, x, y, hang, side, net, depth=0):
        """Plan a vertical 2-pin part whose pin `pin_num` ends at (x, y), body toward `hang` (270 down / 90 up),
        followed by whatever sits at its far pin. Returns (solids, wires, plan) or None."""
        part = self.d.parts[ref]
        inst = _Inst(ref, part, 1, part.symbol)
        pa = next(p for p in inst.pins if p.number == pin_num)
        pb = next(p for p in inst.pins if p.number != pin_num)
        # outward of pin a must point back at (x, y)'s wire: opposite of hang
        want = (hang + 180) % 360
        rot = (want - (pa.angle + 180)) % 360
        dx, dy = _rot(pa.x, pa.y, rot)
        sx, sy = snap(x - dx), snap(y + dy)
        bx, by, bdir = inst.pin_at(pb, sx, sy, rot)
        body = inst.body(sx, sy, rot)
        body = (body[0], min(body[1], body[3]) + (0.4 if hang == 270 else 0),
                body[2], max(body[1], body[3]) - (0.4 if hang == 90 else 0))
        solids = [body, self.text_box(inst, sx, sy, rot, side)]
        plan = [("sym", inst, sx, sy, rot, side)]
        points = [(x, y, net)]
        nb = self.pin_net.get((ref, pb.number))
        points.append((bx, by, nb))
        wires = []
        # far pin
        nxt = None
        if nb is not None and nb not in self.supply and depth < 2:
            others = [(r, p) for r, p in self.d.nets[nb] if r != ref]
            if len(others) == 1:
                r2, p2 = others[0]
                p2t = self.d.parts[r2]
                if r2 not in self.placed and p2t.block == part.block and _inline(p2t):
                    nxt = (r2, p2)
        if nxt:
            # one grid of wire between the two parts keeps the pin ends apart
            ex, ey = bx + DIR[hang][0] * 2 * P, by + DIR[hang][1] * 2 * P
            mx, my = bx + DIR[hang][0] * P, by + DIR[hang][1] * P
            sub = self.place_chain(cl, nxt[0], nxt[1], ex, ey, hang, side, nb, depth + 1)
            if sub:
                s2, w2, p2, pts2 = sub
                ldir = 0 if side == "l" else 180          # name the net on the side away from the part text
                lsol = self.terminal(cl, nb, mx, my, ldir, None, commit=False)
                return (solids + s2 + lsol, wires + [(bx, by, ex, ey, nb)] + w2,
                        plan + [("wire", bx, by, ex, ey, nb), ("term", nb, mx, my, ldir, None, True)] + p2,
                        points + pts2 + [(mx, my, nb)])
        need = nb is not None and nb not in self.supply
        solids += self.terminal(cl, nb, bx, by, bdir, (ref, pb.number), need_label=need, commit=False)
        plan.append(("term", nb, bx, by, bdir, (ref, pb.number), need))
        return solids, wires, plan, points

    def commit(self, cl, plan, solids, wires, points):
        for step in plan:
            if step[0] == "sym":
                _, inst, sx, sy, rot, side = step
                cl.syms.append({"inst": inst, "x": sx, "y": sy, "rot": rot, "text": side})
                self.placed.add(inst.ref)
                for p in inst.pins:
                    n = self.pin_net.get((inst.ref, p.number))
                    if n:
                        self.wired.setdefault((cl.name, n), set()).add((inst.ref, p.number))
            elif step[0] == "wire":
                cl.wires.append(step[1:])
            elif step[0] == "term":
                _, net, x, y, dirn, key, need = step
                self.terminal(cl, net, x, y, dirn, key, need_label=need, commit=True)
        cl.occ.add(solids, wires, points)

    def hub(self, inst: _Inst) -> Cluster:
        cl = Cluster(f"{inst.ref}:{inst.unit}")
        block = inst.part.block
        hx, hy = 0.0, 0.0
        b = inst.body(hx, hy, 0)
        cl.syms.append({"inst": inst, "x": hx, "y": hy, "rot": 0, "text": "c"})
        self.placed.add(inst.ref if inst.part.symbol.units == 1 else f"{inst.ref}:{inst.unit}")
        pins = []
        for p in inst.pins:
            x, y, dirn = inst.pin_at(p, hx, hy, 0)
            n = self.pin_net.get((inst.ref, p.number))
            pins.append((p, x, y, dirn, n))
            if n:
                self.wired.setdefault((cl.name, n), set()).add((inst.ref, p.number))
        # body without the pin stubs: pin ends must stay reachable
        core = (b[0] + 0.5, b[1] + 0.5, b[2] - 0.5, b[3] - 0.5)
        cl.occ.add([core], [], [(x, y, n) for _, x, y, _, n in pins])
        # hidden pins stacked on one spot (e.g. several GND pins) are drawn once
        seen = set()
        pending = []
        for p, x, y, dirn, n in sorted(pins, key=lambda t: (t[3], t[2], t[1])):
            if (x, y) in seen:
                continue
            seen.add((x, y))
            cands = self.satellites(block, n, {inst.ref}) if dirn in (0, 180) else []
            if cands and len(cands) and all(r in self.placed for r, _ in cands):
                cands = []
            if cands:
                pending.append((p, x, y, dirn, n))
                continue
            # neighbouring pins' symbols/labels collide: push this one out on a stub wire
            vx, vy = DIR[dirn]
            for k in range(0, 8):
                tx, ty = snap(x + vx * k * P), snap(y + vy * k * P)
                sol = self.terminal(cl, n, tx, ty, dirn, (inst.ref, p.number), need_label=True, commit=False)
                wire = [(x, y, tx, ty, n)] if k else []
                if n is None or cl.occ.free(sol, wire) or k == 7:
                    break
            if wire:
                cl.wires.append(wire[0])
            self.terminal(cl, n, tx, ty, dirn, (inst.ref, p.number), need_label=True, commit=True)
            cl.occ.add(sol, wire, [(tx, ty, n)])
        # 1) the net name sits on the wire right at the pin (KiCad style), parts go beyond it
        far = {}
        for p, x, y, dirn, n in pending:
            tw = _text_w(n) + 1.0
            box = (x + 0.3, y - 1.9, x + tw, y - 0.35) if dirn == 0 else (x - tw, y - 1.9, x - 0.3, y - 0.35)
            cl.labels.append((n, x, y, dirn))
            cl.occ.add([box])
            far[(x, y)] = (x, int(tw / P) + 2)
        # 2) parts hanging down are placed from the bottom pin upward (an upper chain goes further out and its
        #    wire runs above the lower ones), parts hanging up (to a supply) from the top pin downward
        pairs = []
        for p, x, y, dirn, n in pending:
            for ref, pin in self.satellites(block, n, {inst.ref}):
                other = next(pp for pp in self.d.parts[ref].symbol.pins if pp.number != pin)
                onet = self.pin_net.get((ref, other.number))
                hang = 90 if (onet in self.supply and not is_ground(onet)) else 270
                pairs.append((hang, x, y, dirn, n, ref, pin))
        pairs.sort(key=lambda t: (t[0] == 90, -t[2] if t[0] == 270 else t[2], t[5]))
        for hang, x, y, dirn, n, ref, pin in pairs:
            if ref in self.placed:
                continue
            side = "l" if dirn == 180 else "r"
            vx = DIR[dirn][0]
            fx, k0 = far[(x, y)]
            for k in range(k0, k0 + 28):
                cx = snap(x + vx * k * P)
                res = self.place_chain(cl, ref, pin, cx, y, hang, side, n)
                if not res:
                    continue
                sol, wir, plan, pts = res
                wire = (fx, y, cx, y, n)
                if cl.occ.free(sol, wir + [wire]):
                    tmp = Rects(); tmp.add(sol)          # the wire must not cut through the new part either
                    if not tmp.free([], [wire]):
                        continue
                    self.commit(cl, [("wire",) + wire] + plan, sol, wir + [wire], pts)
                    far[(x, y)] = (cx, k + 2)
                    break
        # reference / value text: above, below, or beside
        tw = max(_text_w(inst.ref), _text_w(inst.part.value or "")) + 1
        cx = (b[0] + b[2]) / 2
        bx0, by0, bx1, by1 = cl.occ.bbox()
        for tx, ty in ((cx, b[1] - 4.0), (cx, b[3] + 1.5), (cx, by0 - 4.6), (cx, by1 + 1.5)):
            box = (tx - tw / 2, ty - 0.4, tx + tw / 2, ty + 4.0)
            if cl.occ.free([box]):
                break
        cl.syms[0]["text"] = ("c", snap(tx), snap(ty + 0.4), snap(ty + 2.9))
        cl.occ.add([box])
        return cl

    # ------------------------------------------------------------- leftovers
    def rail_group(self, name, refs, top, bot) -> Cluster:
        """2-pin parts across two supply rails, side by side on shared rail wires."""
        cl = Cluster(name)
        x = 0.0
        tops, bots = [], []
        for ref in refs:
            part = self.d.parts[ref]
            inst = _Inst(ref, part, 1, part.symbol)
            pa = next(p for p in inst.pins if self.pin_net.get((ref, p.number)) == top)
            pb = next(p for p in inst.pins if p is not pa)
            rot = (90 - (pa.angle + 180)) % 360
            dx, dy = _rot(pa.x, pa.y, rot)
            sx, sy = snap(x - dx), snap(0 + dy)
            ax, ay, _ = inst.pin_at(pa, sx, sy, rot)
            bx, by, _ = inst.pin_at(pb, sx, sy, rot)
            cl.syms.append({"inst": inst, "x": sx, "y": sy, "rot": rot, "text": "r"})
            self.placed.add(ref)
            tops.append((ax, ay)); bots.append((bx, by))
            body = inst.body(sx, sy, rot)
            tb = self.text_box(inst, sx, sy, rot, "r")
            cl.occ.add([body, tb])
            x = snap(tb[2] + 2.0 + P)
        top_y = min(y for _, y in tops) - P
        bot_y = max(y for _, y in bots) + P
        for (ax, ay), (bx, by) in zip(tops, bots):
            cl.wires.append((ax, ay, ax, top_y, top))
            cl.wires.append((bx, by, bx, bot_y, bot))
        for a, b2 in zip(tops, tops[1:]):
            cl.wires.append((a[0], top_y, b2[0], top_y, top))
        for a, b2 in zip(bots, bots[1:]):
            cl.wires.append((a[0], bot_y, b2[0], bot_y, bot))
        for net, (px, py), dirn in ((top, (tops[0][0], top_y), 90), (bot, (bots[0][0], bot_y), 270)):
            cl.occ.add(self.terminal(cl, net, px, py, dirn, None))
        cl.occ.add([], [(w[0], w[1], w[2], w[3], w[4]) for w in cl.wires])
        return cl

    def star(self, name, net, members) -> Cluster:
        """parts hanging off one horizontal wire of `net` (down, or up when their far pin is a positive rail)"""
        cl = Cluster(name)
        x = 0.0
        xs = []
        for inst, pa in sorted(members, key=lambda t: _refkey(t[0].ref)):
            pb = next(p for p in inst.pins if p is not pa)
            nb = self.pin_net.get((inst.ref, pb.number))
            hang = 90 if (nb in self.supply and not is_ground(nb)) else 270
            want = (hang + 180) % 360
            rot = (want - (pa.angle + 180)) % 360
            # first pass at x to measure the body, then shift so the text clears the previous part
            dx, dy = _rot(pa.x, pa.y, rot)
            b = inst.body(snap(x - dx), snap(dy), rot)
            x = snap(x + max(0.0, -(b[0] - x)) + 0.0)
            sx, sy = snap(x - dx), snap(dy)
            ax, ay, _ = inst.pin_at(pa, sx, sy, rot)
            bx, by, bdir = inst.pin_at(pb, sx, sy, rot)
            cl.syms.append({"inst": inst, "x": sx, "y": sy, "rot": rot, "text": "r"})
            self.placed.add(inst.ref)
            body = inst.body(sx, sy, rot)
            tb = self.text_box(inst, sx, sy, rot, "r")
            cl.occ.add([body, tb])
            need = nb is not None and nb not in self.supply
            cl.occ.add(self.terminal(cl, nb, bx, by, bdir, (inst.ref, pb.number), need_label=need))
            xs.append(ax)
            term_w = _text_w(nb) / 2 + 1 if nb in self.supply else 0
            x = snap(max(tb[2], body[2] + term_w) + 3 * GRID + P)
        for a, b2 in zip(xs, xs[1:]):
            cl.wires.append((a, 0.0, b2, 0.0, net))
        lx = snap(xs[0] - 2 * P)
        cl.wires.append((lx, 0.0, xs[0], 0.0, net))
        cl.occ.add(self.terminal(cl, net, lx, 0.0, 180, None))
        cl.occ.add([], list(cl.wires))
        return cl

    def single(self, inst: _Inst) -> Cluster:
        """A part that hangs off nothing: vertical (2-pin) or as-is, every pin gets its own terminal."""
        cl = Cluster(f"{inst.ref}:{inst.unit}")
        rot = 0
        if _inline(inst.part):
            nets = [self.pin_net.get((inst.ref, p.number)) for p in inst.pins]
            # ground / lower-potential pin at the bottom
            first = 1 if (nets[0] is not None and is_ground(nets[0])) else 0
            pa = inst.pins[first]
            rot = (90 - (pa.angle + 180)) % 360
        cl.syms.append({"inst": inst, "x": 0.0, "y": 0.0, "rot": rot, "text": "r"})
        self.placed.add(inst.ref)
        b = inst.body(0, 0, rot)
        tb = self.text_box(inst, 0, 0, rot, "r")
        cl.occ.add([(b[0] + 0.5, b[1] + 0.5, b[2] - 0.5, b[3] - 0.5), tb])
        if not _inline(inst.part):
            cl.occ.solid.pop()
            tw = max(_text_w(inst.ref), _text_w(inst.part.value or "")) + 1
            cx = (b[0] + b[2]) / 2
            cl.syms[0]["text"] = ("c", snap(cx), snap(b[1] - 3.0), snap(b[3] + 2.6))
            cl.occ.add([(cx - tw / 2, b[1] - 4.4, cx + tw / 2, b[1] - 0.5), (cx - tw / 2, b[3] + 0.5, cx + tw / 2, b[3] + 3.6)])
        for p in inst.pins:
            x, y, dirn = inst.pin_at(p, 0, 0, rot)
            n = self.pin_net.get((inst.ref, p.number))
            cl.occ.add(self.terminal(cl, n, x, y, dirn, (inst.ref, p.number)))
        return cl

    # ------------------------------------------------------------- blocks
    def build(self):
        blocks: dict[str, list] = {}
        insts: dict[str, list[_Inst]] = {}
        for ref, part in self.d.parts.items():
            if not part.symbol:
                continue
            for u in range(1, part.symbol.units + 1):
                inst = _Inst(ref, part, u, part.symbol)
                if part.symbol.units > 1 and not inst.pins:
                    continue
                insts.setdefault(part.block, []).append(inst)
        for block, lst in insts.items():
            cls = []
            hubs = sorted([i for i in lst if len(i.pins) >= 2 and not _inline(i.part)],
                          key=lambda i: (-len(i.pins), i.ref))
            for h in hubs:
                cls.append(self.hub(h))
            rest = [i for i in lst if i.ref not in self.placed and i not in hubs]
            # leftover straight parts that share a signal net (dividers, RC filters...) hang off one wire
            while True:
                share: dict[str, list] = {}
                for i in rest:
                    if i.ref in self.placed or not _inline(i.part):
                        continue
                    for pp in i.pins:
                        n = self.pin_net.get((i.ref, pp.number))
                        if n and n not in self.supply:
                            share.setdefault(n, []).append((i, pp))
                share = {n: v for n, v in share.items() if len(v) >= 2}
                if not share:
                    break
                n = max(share, key=lambda k: (len(share[k]), k))
                cls.append(self.star(f"{block}:{n}", n, share[n]))
            # decoupling-style parts: both pins on supply rails, grouped per rail pair
            rails: dict[tuple, list] = {}
            for i in rest:
                if not _inline(i.part):
                    continue
                nets = [self.pin_net.get((i.ref, p.number)) for p in i.pins]
                if all(n in self.supply for n in nets) and nets[0] != nets[1]:
                    top, bot = sorted(nets, key=lambda n: (is_ground(n), n))
                    rails.setdefault((top, bot), []).append(i.ref)
            for (top, bot), refs in rails.items():
                cls.append(self.rail_group(f"{block}:{top}:{bot}", sorted(refs, key=_refkey), top, bot))
            for i in sorted(rest, key=lambda i: _refkey(i.ref)):
                if i.ref not in self.placed:
                    cls.append(self.single(i))
            blocks[block] = cls
        return blocks


def _refkey(ref):
    m = re.match(r"([A-Za-z#]+)(\d*)", ref)
    return (m.group(1), int(m.group(2) or 0)) if m else (ref, 0)


# ================================================================== emit
def generate(d: Design, out_path: Path | None = None) -> Path:
    library.set_project_libs(d.project_dir)
    out_path = Path(out_path or d.project_dir / f"{d.name}.kicad_sch")
    root = uid(d.name, "root")
    bld = Builder(d)
    blocks = bld.build()

    # power flags: nets with power_in pins but no power_out driver (power symbols are power_in too)
    flags = []
    for net, pins in d.nets.items():
        types = set()
        for ref, num in pins:
            for p in d.parts[ref].symbol.pins:
                if p.number == num:
                    types.add(p.etype)
        if net in bld.supply:
            types.add("power_in")
        if "power_in" in types and "power_out" not in types:
            flags.append(net)

    # ---------------- layout: shelf-pack clusters inside blocks, then blocks on the sheet
    block_rects = []
    for name, cls in blocks.items():
        boxes = []
        for cl in cls:
            x0, y0, x1, y1 = cl.occ.bbox()
            boxes.append((cl, x0, y0, x1 - x0, y1 - y0))
        boxes.sort(key=lambda t: -t[4])
        total = sum(w * h for _, _, _, w, h in boxes)
        max_row_w = max(max(w for *_, w, _ in boxes) + 6, (total ** 0.5) * 1.6, 120.0)
        x = y = 0.0
        row_h = bw = 0.0
        placed = []
        for cl, x0, y0, w, h in boxes:
            if x > 0 and x + w > max_row_w:
                x = 0.0; y += row_h + 5; row_h = 0.0
            placed.append((cl, snap(x + 4 - x0), snap(y + 8 - y0)))
            x += w + 7
            bw = max(bw, x)
            row_h = max(row_h, h)
        block_rects.append([name, placed, bw + 1, y + row_h + 12])

    margin = 20.0
    papers = (("A4", (297, 210)), ("A3", (420, 297)), ("A2", (594, 420)), ("A1", (841, 594)), ("A0", (1189, 841)))

    def pack(sheet_w):
        bx = by = 0.0
        shelf_h = 0.0
        out = []
        for name, placed, w, h in sorted(block_rects, key=lambda b: -b[3]):
            if bx > 0 and bx + w > sheet_w:
                bx = 0.0; by += shelf_h + 8; shelf_h = 0.0
            out.append((name, placed, margin + bx, margin + by, w, h))
            bx += w + 8
            shelf_h = max(shelf_h, h)
        uw = max(p[2] + p[4] for p in out) + margin
        uh = by + shelf_h + margin * 2 + 20
        k = next((i for i, (_, (pw, ph)) in enumerate(papers) if uw <= pw and uh <= ph), len(papers) - 1)
        pw, ph = papers[k][1]
        return (k, max(uw / pw, uh / ph)), out, uw, uh, k

    widest = max(w for _, _, w, _ in block_rects)
    best = min((pack(sw) for sw in [widest] + [float(v) for v in range(200, 1180, 20) if v > widest]),
               key=lambda r: r[0])
    _, layout, used_w, used_h, k = best
    paper = papers[k][0]

    # ---------------- emit
    lib_syms = {}
    items = []
    wires = []          # sheet coords (x1, y1, x2, y2)
    pin_pts = set()     # every pin end on the sheet (symbols + power symbols)
    pwr_n = [0]

    def prop(k, v, x, y, just=None, hide=False, ang=0):
        return [S("property"), k, str(v), [S("at"), snap(x), snap(y), ang], _effects(just, hide)]

    def power_symbol(net, x, y, dirn, tag):
        lib = "power:GND" if is_ground(net) else "power:VCC"
        sym = library.get_symbol(lib)
        lib_syms[lib] = sym.node
        natural = 270 if is_ground(net) else 90
        rot = (dirn - natural) % 360
        pwr_n[0] += 1
        ref = f"#PWR{pwr_n[0]:03d}"
        vx, vy = DIR[dirn]
        if dirn in (90, 270):
            vpos, just = (x, y + vy * (4.4 if dirn == 270 else 3.6)), None
        else:
            vpos, just = (x + vx * (4.2 + _text_w(net) / 2), y), None
        fang = 90 if rot in (90, 270) else 0      # field angles are relative to the symbol's own rotation
        items.append([S("symbol"), [S("lib_id"), lib], [S("at"), x, y, rot], [S("unit"), 1],
                      [S("exclude_from_sim"), S("no")], [S("in_bom"), S("yes")], [S("on_board"), S("yes")],
                      [S("dnp"), S("no")], [S("uuid"), uid(d.name, "pwr", tag)],
                      prop("Reference", ref, x, y, hide=True, ang=fang),
                      prop("Value", net, vpos[0], vpos[1], just, ang=fang),
                      prop("Footprint", "", x, y, hide=True), prop("Datasheet", "~", x, y, hide=True),
                      prop("Description", "", x, y, hide=True),
                      [S("pin"), sym.pins[0].number, [S("uuid"), uid(d.name, "pwrpin", tag)]],
                      [S("instances"), [S("project"), d.name, [S("path"), f"/{root}",
                       [S("reference"), ref], [S("unit"), 1]]]]])
        pin_pts.add((snap(x), snap(y)))

    def label(net, x, y, dirn, tag):
        just = "right bottom" if dirn in (180, 270) else "left bottom"
        items.append([S("label"), net, [S("at"), x, y, dirn], [S("fields_autoplaced"), S("yes")],
                      _effects(just), [S("uuid"), uid(d.name, "lbl", tag)]])

    for name, placed, ox, oy, w, h in layout:
        items.append([S("rectangle"), [S("start"), snap(ox), snap(oy)], [S("end"), snap(ox + w), snap(oy + h)],
                      [S("stroke"), [S("width"), 0.2], [S("type"), S("dash")]], [S("fill"), [S("type"), S("none")]],
                      [S("uuid"), uid(d.name, "blk", name)]])
        items.append([S("text"), name.upper().replace("_", " "), [S("exclude_from_sim"), S("no")],
                      [S("at"), snap(ox + 2), snap(oy + 4), 0],
                      _effects("left bottom", size=2.0), [S("uuid"), uid(d.name, "blkt", name)]])
        for cl, cx, cy in placed:
            X = lambda v: snap(ox + cx + v)
            Y = lambda v: snap(oy + cy + v)
            for s in cl.syms:
                inst, rot = s["inst"], s["rot"]
                sx, sy = X(s["x"]), Y(s["y"])
                part, sym = inst.part, inst.sym
                lib_syms[sym.lib_id] = sym.node
                u = uid(d.name, "sym", inst.ref, inst.unit)
                ib = find(sym.node, "in_bom")
                fp_attr = find(part.footprint.node, "attr") if part.footprint else None
                fp_excl = fp_attr is not None and any(str(a) == "exclude_from_bom" for a in fp_attr[1:])
                in_bom = "no" if fp_excl or (ib is not None and str(ib[1]) == "no") else "yes"
                node = [S("symbol"), [S("lib_id"), sym.lib_id], [S("at"), sx, sy, rot], [S("unit"), inst.unit],
                        [S("exclude_from_sim"), S("no")], [S("in_bom"), S(in_bom)], [S("on_board"), S("yes")],
                        [S("dnp"), S("yes" if part.data.get("dnp") else "no")], [S("uuid"), u]]
                fang = 90 if rot in (90, 270) else 0
                b = inst.body(sx, sy, rot)
                if isinstance(s["text"], tuple):
                    _, tx, ry, vy = s["text"]
                    node.append(prop("Reference", inst.ref, ox + cx + tx, oy + cy + ry, ang=fang))
                    node.append(prop("Value", part.value, ox + cx + tx, oy + cy + vy, ang=fang))
                else:
                    # centred text: a justification would be mirrored by the symbol's rotation
                    mid = (b[1] + b[3]) / 2
                    for kk, txt, ty in (("Reference", inst.ref, mid - 1.27), ("Value", part.value, mid + 1.27)):
                        hw = _text_w(txt) / 2
                        tx = b[0] - 1.2 - hw if s["text"] == "l" else b[2] + 1.2 + hw
                        node.append(prop(kk, txt, tx, ty, ang=fang))
                node.append(prop("Footprint", part.footprint_id, sx, sy, hide=True))
                node.append(prop("Datasheet", part.data.get("datasheet", sym.props.get("Datasheet", "~")), sx, sy, hide=True))
                node.append(prop("Description", part.data.get("description", sym.props.get("Description", "")), sx, sy, hide=True))
                for kk, key in (("MPN", "mpn"), ("LCSC", "lcsc"), ("Manufacturer", "manufacturer")):
                    if part.data.get(key):
                        node.append(prop(kk, part.data[key], sx, sy, hide=True))
                for p in sym.pins:
                    node.append([S("pin"), p.number, [S("uuid"), uid(d.name, "pin", inst.ref, p.number)]])
                node.append([S("instances"), [S("project"), d.name, [S("path"), f"/{root}",
                             [S("reference"), inst.ref], [S("unit"), inst.unit]]]])
                items.append(node)
                for p in inst.pins:
                    px, py, _ = inst.pin_at(p, sx, sy, rot)
                    pin_pts.add((px, py))
            for i, (x1, y1, x2, y2, _n) in enumerate(cl.wires):
                if (X(x1), Y(y1)) != (X(x2), Y(y2)):
                    wires.append((X(x1), Y(y1), X(x2), Y(y2)))
            for i, (net, x, y, dirn) in enumerate(cl.labels):
                label(net, X(x), Y(y), dirn, f"{cl.name}/{i}")
            for i, (net, x, y, dirn) in enumerate(cl.power):
                power_symbol(net, X(x), Y(y), dirn, f"{cl.name}/{i}")
            for i, (x, y) in enumerate(cl.nc):
                items.append([S("no_connect"), [S("at"), X(x), Y(y)], [S("uuid"), uid(d.name, "nc", cl.name, i)]])

    # PWR_FLAGs along the bottom
    if flags:
        flag_sym = library.get_symbol("power:PWR_FLAG")
        lib_syms[flag_sym.lib_id] = flag_sym.node
        fy = snap(used_h - margin - 10)
        fx = snap(margin + 10)
        for i, net in enumerate(flags):
            ref = f"#FLG{i + 1:02d}"
            items.append([S("symbol"), [S("lib_id"), "power:PWR_FLAG"], [S("at"), fx, fy, 0], [S("unit"), 1],
                          [S("exclude_from_sim"), S("no")], [S("in_bom"), S("yes")], [S("on_board"), S("yes")],
                          [S("dnp"), S("no")], [S("uuid"), uid(d.name, "flag", net)],
                          prop("Reference", ref, fx, snap(fy + 2.54), hide=True),
                          prop("Value", "PWR_FLAG", fx, snap(fy - 3.81)),
                          prop("Footprint", "", fx, fy, hide=True), prop("Datasheet", "~", fx, fy, hide=True),
                          prop("Description", "", fx, fy, hide=True),
                          [S("pin"), "1", [S("uuid"), uid(d.name, "flagpin", net)]],
                          [S("instances"), [S("project"), d.name, [S("path"), f"/{root}",
                           [S("reference"), ref], [S("unit"), 1]]]]])
            pin_pts.add((fx, fy))
            wires.append((fx, fy, fx, snap(fy + 2.54)))
            if net in bld.supply:
                power_symbol(net, fx, snap(fy + 2.54), 270 if is_ground(net) else 90, f"flag/{net}")
                if not is_ground(net):     # VCC symbol points up into the flag: move it below on its own stub
                    items.pop(); pin_pts.discard((fx, snap(fy + 2.54)))
                    wires.append((fx, snap(fy + 2.54), fx, snap(fy + 5.08)))
                    power_symbol(net, fx, snap(fy + 5.08), 270, f"flag/{net}")
            else:
                label(net, fx, snap(fy + 2.54), 270, f"flag/{net}")
            fx = snap(fx + max(15.0, _text_w(net) + 8))

    items += _wire_items(d, wires, pin_pts)

    tb = [S("title_block"), [S("title"), d.raw.get("title", d.name)], [S("date"), d.raw.get("date", "")],
          [S("rev"), str(d.raw.get("rev", "A"))], [S("company"), d.raw.get("company", "")],
          [S("comment"), 1, "Generated by aipcb (Claude-driven EDA)"]]
    tree = [S("kicad_sch"), [S("version"), 20250114], [S("generator"), "eeschema"], [S("generator_version"), "9.0"],
            [S("uuid"), root], [S("paper"), paper], tb,
            [S("lib_symbols")] + list(lib_syms.values())] + items + [
            [S("sheet_instances"), [S("path"), "/", [S("page"), "1"]]], [S("embedded_fonts"), S("no")]]
    out_path.write_text(dumps(tree) + "\n", encoding="utf-8")
    return out_path


def _wire_items(d, wires, pin_pts):
    """Split wires at every connection point lying inside them and drop junction dots where 3+ items meet."""
    pts = set(pin_pts)
    for x1, y1, x2, y2 in wires:
        pts.add((x1, y1)); pts.add((x2, y2))
    segs = []
    for x1, y1, x2, y2 in wires:
        inner = [(x, y) for x, y in pts if (x, y) not in ((x1, y1), (x2, y2))
                 and min(x1, x2) - 1e-6 <= x <= max(x1, x2) + 1e-6 and min(y1, y2) - 1e-6 <= y <= max(y1, y2) + 1e-6
                 and abs((x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)) < 1e-6]
        inner.sort(key=lambda p: abs(p[0] - x1) + abs(p[1] - y1))
        chain = [(x1, y1)] + inner + [(x2, y2)]
        segs += [(a, b) for a, b in zip(chain, chain[1:]) if a != b]
    segs = list(dict.fromkeys(tuple(sorted(s)) for s in segs))
    deg = {}
    for a, b in segs:
        deg[a] = deg.get(a, 0) + 1
        deg[b] = deg.get(b, 0) + 1
    for p in pin_pts:
        if p in deg:
            deg[p] += 1
    out = []
    for i, (a, b) in enumerate(segs):
        out.append([S("wire"), [S("pts"), [S("xy"), a[0], a[1]], [S("xy"), b[0], b[1]]],
                    [S("stroke"), [S("width"), 0], [S("type"), S("default")]], [S("uuid"), uid(d.name, "w", a, b)]])
    for p, n in sorted(deg.items()):
        if n >= 3:
            out.append([S("junction"), [S("at"), p[0], p[1]], [S("diameter"), 0], [S("color"), 0, 0, 0, 0],
                        [S("uuid"), uid(d.name, "j", p)]])
    return out


# ================================================================== verification
def verify(d: Design, sch: Path | None = None) -> dict:
    """Export KiCad's netlist of the drawn schematic and compare its connectivity with design.yaml."""
    sch = Path(sch or d.project_dir / f"{d.name}.kicad_sch")
    out = d.project_dir / "out" / "check_sch.net"
    out.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run([str(config.KICAD_CLI), "sch", "export", "netlist", "--format", "kicadsexpr", "-o", str(out),
                        str(sch)], capture_output=True, text=True, timeout=300)
    if not out.exists():
        return {"ok": False, "errors": ["netlist export failed: " + p.stderr[-300:]]}
    tree = parse(out.read_text())
    drawn = []
    for net in find(tree, "nets")[1:]:
        if not isinstance(net, list):
            continue
        name = str(find(net, "name")[1])
        nodes = {(str(find(n, "ref")[1]), str(find(n, "pin")[1])) for n in net[1:]
                 if isinstance(n, list) and str(n[0]) == "node"}
        nodes = {nd for nd in nodes if not nd[0].startswith("#")}
        if nodes:
            drawn.append((name, nodes))
    by_pin = {}
    for name, nodes in drawn:
        for nd in nodes:
            by_pin[nd] = name
    errors = []
    for net, pins in d.nets.items():
        want = set(map(tuple, pins))
        got_names = {by_pin.get(pn) for pn in want}
        if len(got_names) != 1 or None in got_names:
            errors.append(f"net {net}: pins split over {sorted(map(str, got_names))}")
            continue
        gname = got_names.pop()
        got = next(nodes for name, nodes in drawn if name == gname)
        extra = got - want
        if extra:
            errors.append(f"net {net}: also connects {sorted(extra)}")
        if gname.lstrip("/") != net:
            errors.append(f"net {net}: drawn as '{gname}'")
    return {"ok": not errors, "errors": errors[:40], "nets": len(drawn)}


def symbol_uuid(d: Design, ref: str) -> str:
    return uid(d.name, "sym", ref, 1)


def root_uuid(d: Design) -> str:
    return uid(d.name, "root")
