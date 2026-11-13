"""Physics-based component placement engine.

Pipeline:
  1. constrained parts (fixed / edge) are pinned
  2. force-directed relaxation: net springs (star model), courtyard repulsion,
     board-boundary walls, "near" springs (decoupling caps to IC pins), block cohesion
  3. simulated annealing (translate / rotate / swap) on a cost of
     weighted HPWL + overlap + out-of-board + near-constraint + congestion (RUDY)
  4. legalisation: greedy spiral search removes any residual overlap

Coordinates: KiCad board frame, mm, y down, board origin at (ORIGIN, ORIGIN).
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

from .spec import Design

ORIGIN = 100.0   # board top-left in KiCad coordinates


def rot_pt(x: float, y: float, deg: float) -> tuple[float, float]:
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return x * c + y * s, -x * s + y * c


@dataclass
class Comp:
    ref: str
    box: tuple[float, float, float, float]          # local courtyard
    pins: list[tuple[float, float, int]]            # local x, y, net index
    block: str
    x: float = 0.0
    y: float = 0.0
    rot: int = 0
    fixed: bool = False
    rot_fixed: bool = False
    edge: str | None = None
    edge_offset: float = 0.0
    near: tuple[str, str] | None = None
    near_dist: float = 2.0
    nets: set = field(default_factory=set)
    local_rects: list = field(default_factory=list)

    def rects(self, x=None, y=None, rot=None) -> list[tuple[float, float, float, float]]:
        x = self.x if x is None else x
        y = self.y if y is None else y
        rot = self.rot if rot is None else rot
        out = []
        for x0, y0, x1, y1 in (self.local_rects or [self.box]):
            pts = [rot_pt(px, py, rot) for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
            xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
            out.append((x + min(xs), y + min(ys), x + max(xs), y + max(ys)))
        return out

    def rect(self, x=None, y=None, rot=None) -> tuple[float, float, float, float]:
        x = self.x if x is None else x
        y = self.y if y is None else y
        rot = self.rot if rot is None else rot
        x0, y0, x1, y1 = self.box
        pts = [rot_pt(px, py, rot) for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        return (x + min(xs), y + min(ys), x + max(xs), y + max(ys))

    def pin_abs(self, i: int) -> tuple[float, float]:
        px, py, _ = self.pins[i]
        dx, dy = rot_pt(px, py, self.rot)
        return self.x + dx, self.y + dy

    @property
    def area(self) -> float:
        x0, y0, x1, y1 = self.box
        return (x1 - x0) * (y1 - y0)


class Placer:
    def __init__(self, d: Design, seed: int = 1):
        self.d = d
        self.rng = random.Random(seed)
        b = d.board
        self.W, self.H = float(b["width"]), float(b["height"])
        self.spacing = d.rules["part_spacing"]
        self.edge_clear = 0.5
        self.net_names = list(d.nets)
        self.net_idx = {n: i for i, n in enumerate(self.net_names)}
        pin_net = d.pin_net()
        gnd = {n for n in self.net_names if n.upper() in ("GND", "AGND", "PGND", "DGND")}
        pw = set(d.rules["power_nets"])
        self.net_w = [0.15 if n in gnd else (0.6 if n in pw else 1.0) for n in self.net_names]
        self.keepouts = [(k["x"], k["y"], k["x"] + k["w"], k["y"] + k["h"]) for k in b.get("keepouts", [])]
        self.comps: dict[str, Comp] = {}
        for ref, part in d.parts.items():
            fp = part.footprint
            if fp is None:
                continue
            pins = []
            for pad in fp.pads:
                net = pin_net.get((ref, pad.number))
                if net is not None:
                    pins.append((pad.x, pad.y, self.net_idx[net]))
            pl = part.place
            c = Comp(ref=ref, box=fp.courtyard, pins=pins, block=part.block, local_rects=list(fp.court_rects))
            c.nets = {p[2] for p in pins}
            if "rot" in pl:
                c.rot = int(pl["rot"]) % 360
                c.rot_fixed = True
            c.edge = pl.get("edge")
            # place.x / place.y are board-relative (mm from the top-left corner)
            if pl.get("fixed") or ("x" in pl and "y" in pl) or (c.edge and ("x" in pl or "y" in pl)):
                c.x = ORIGIN + float(pl.get("x", self.W / 2))
                c.y = ORIGIN + float(pl.get("y", self.H / 2))
                c.fixed = True
                c.rot_fixed = True
            c.edge_offset = float(pl.get("edge_offset", 0.0))
            if pl.get("near"):
                r, p = str(pl["near"]).split(".", 1)
                c.near = (r, p)
                c.near_dist = float(pl.get("near_dist", 2.0))
            self.comps[ref] = c
        self.net_members: list[list[tuple[str, int]]] = [[] for _ in self.net_names]
        for ref, c in self.comps.items():
            for i, (_, _, ni) in enumerate(c.pins):
                self.net_members[ni].append((ref, i))
        # resolve near targets to (comp, pin index)
        self.near_target: dict[str, tuple[str, int]] = {}
        for ref, c in self.comps.items():
            if c.near:
                tr, tp = c.near
                tc = self.comps.get(tr)
                if tc is None:
                    continue
                part = d.parts[tr]
                nums = [p.number for p in part.symbol.pins if p.number == tp or p.name == tp]
                for i, pad in enumerate(d.parts[tr].footprint.pads):
                    if pad.number in nums:
                        idx = next((k for k, pp in enumerate(tc.pins)
                                    if abs(pp[0] - pad.x) < 1e-6 and abs(pp[1] - pad.y) < 1e-6), None)
                        if idx is not None:
                            self.near_target[ref] = (tr, idx)
                            break
        self.movable = [c for c in self.comps.values() if not c.fixed]

    # -------------------------------------------------------------- edges
    def apply_edge(self, c: Comp) -> None:
        if not c.edge:
            return
        x0, y0, x1, y1 = c.rect(0, 0)
        off = c.edge_offset
        if c.edge == "top":
            c.y = ORIGIN - y0 - off
        elif c.edge == "bottom":
            c.y = ORIGIN + self.H - y1 + off
        elif c.edge == "left":
            c.x = ORIGIN - x0 - off
        elif c.edge == "right":
            c.x = ORIGIN + self.W - x1 + off

    # -------------------------------------------------------------- costs
    def net_hpwl(self, ni: int) -> float:
        mem = self.net_members[ni]
        if len(mem) < 2:
            return 0.0
        xs, ys = [], []
        for ref, i in mem:
            x, y = self.comps[ref].pin_abs(i)
            xs.append(x); ys.append(y)
        return (max(xs) - min(xs) + max(ys) - min(ys)) * self.net_w[ni]

    def overlap(self, a: Comp, b: Comp) -> float:
        s = self.spacing
        ab, bb = a.rect(), b.rect()
        if ab[0] - s > bb[2] or bb[0] - s > ab[2] or ab[1] - s > bb[3] or bb[1] - s > ab[3]:
            return 0.0
        tot = 0.0
        for ax0, ay0, ax1, ay1 in a.rects():
            for bx0, by0, bx1, by1 in b.rects():
                ox = min(ax1, bx1) - max(ax0, bx0) + s
                oy = min(ay1, by1) - max(ay0, by0) + s
                if ox > 0 and oy > 0:
                    tot += ox * oy
        return tot

    def outside(self, c: Comp) -> float:
        x0, y0, x1, y1 = c.rect()
        e = 0.0 if c.edge else self.edge_clear
        m = 0.0
        m += max(0.0, ORIGIN + e - x0) + max(0.0, x1 - (ORIGIN + self.W - e))
        m += max(0.0, ORIGIN + e - y0) + max(0.0, y1 - (ORIGIN + self.H - e))
        if c.edge:   # edge parts may overhang only along their edge axis by their offset
            m = 0.0
            if c.edge in ("top", "bottom"):
                m += max(0.0, ORIGIN - x0) + max(0.0, x1 - ORIGIN - self.W)
            else:
                m += max(0.0, ORIGIN - y0) + max(0.0, y1 - ORIGIN - self.H)
        for kx0, ky0, kx1, ky1 in self.keepouts:
            ox = min(x1, ORIGIN + kx1) - max(x0, ORIGIN + kx0)
            oy = min(y1, ORIGIN + ky1) - max(y0, ORIGIN + ky0)
            if ox > 0 and oy > 0:
                m += ox * oy
        return m

    def near_cost(self, c: Comp) -> float:
        t = self.near_target.get(c.ref)
        if not t:
            return 0.0
        tx, ty = self.comps[t[0]].pin_abs(t[1])
        # nearest own pin to the target pin
        best = min(math.hypot(*(v - w for v, w in zip(c.pin_abs(i), (tx, ty)))) for i in range(len(c.pins))) \
            if c.pins else math.hypot(c.x - tx, c.y - ty)
        return max(0.0, best - c.near_dist) ** 2 * 4.0

    def comp_cost(self, c: Comp) -> float:
        cost = 60.0 * self.outside(c) + self.near_cost(c)
        for o in self.comps.values():
            if o is not c:
                cost += 40.0 * self.overlap(c, o)
        # parts that are "near" this one also depend on its position
        for ref, (tr, _) in self.near_target.items():
            if tr == c.ref:
                cost += self.near_cost(self.comps[ref])
        return cost

    def total_cost(self) -> dict:
        wl = sum(self.net_hpwl(i) for i in range(len(self.net_names)))
        ov = 0.0
        cl = list(self.comps.values())
        for i, a in enumerate(cl):
            for b in cl[i + 1:]:
                ov += self.overlap(a, b)
        out = sum(self.outside(c) for c in cl)
        near = sum(self.near_cost(c) for c in cl)
        return {"wirelength": wl, "overlap": ov, "outside": out, "near": near,
                "total": wl + 40 * ov + 60 * out + near}

    # -------------------------------------------------------------- phases
    def init_positions(self) -> None:
        blocks = sorted({c.block for c in self.movable})
        anchors = {}
        n = max(1, len(blocks))
        cols = math.ceil(math.sqrt(n * self.W / self.H))
        rows = math.ceil(n / cols)
        for i, b in enumerate(blocks):
            anchors[b] = (ORIGIN + (i % cols + 0.5) * self.W / cols, ORIGIN + (i // cols + 0.5) * self.H / rows)
        for c in self.comps.values():
            if c.fixed:
                continue
            ax, ay = anchors[c.block]
            c.x = ax + self.rng.uniform(-3, 3)
            c.y = ay + self.rng.uniform(-3, 3)
            self.apply_edge(c)

    def force_directed(self, iters: int = 400) -> None:
        step = max(self.W, self.H) / 20
        for it in range(iters):
            temp = step * (1 - it / iters) + 0.05
            forces = {c.ref: [0.0, 0.0] for c in self.movable}
            # net springs (star model to net centroid)
            for ni, mem in enumerate(self.net_members):
                if len(mem) < 2:
                    continue
                pts = [self.comps[r].pin_abs(i) for r, i in mem]
                cx = sum(p[0] for p in pts) / len(pts)
                cy = sum(p[1] for p in pts) / len(pts)
                k = self.net_w[ni] * 0.08 / (len(mem) - 1) ** 0.5
                for (r, _), (px, py) in zip(mem, pts):
                    if r in forces:
                        forces[r][0] += (cx - px) * k
                        forces[r][1] += (cy - py) * k
            # near springs
            for ref, (tr, ti) in self.near_target.items():
                c = self.comps[ref]
                if ref in forces:
                    tx, ty = self.comps[tr].pin_abs(ti)
                    forces[ref][0] += (tx - c.x) * 0.5
                    forces[ref][1] += (ty - c.y) * 0.5
            # courtyard repulsion (per courtyard rectangle pair)
            cl = list(self.comps.values())
            rl = {c.ref: c.rects() for c in cl}
            bb = {c.ref: c.rect() for c in cl}
            s = self.spacing
            for i, a in enumerate(cl):
                A = bb[a.ref]
                for b in cl[i + 1:]:
                    B = bb[b.ref]
                    if A[0] - s > B[2] or B[0] - s > A[2] or A[1] - s > B[3] or B[1] - s > A[3]:
                        continue
                    for ar in rl[a.ref]:
                        for br in rl[b.ref]:
                            ox = min(ar[2], br[2]) - max(ar[0], br[0]) + s
                            oy = min(ar[3], br[3]) - max(ar[1], br[1]) + s
                            if ox <= 0 or oy <= 0:
                                continue
                            dx = (ar[0] + ar[2] - br[0] - br[2]) / 2
                            dy = (ar[1] + ar[3] - br[1] - br[3]) / 2
                            if ox < oy:
                                fv = ((ox * 0.6) * (1 if dx >= 0 else -1), 0.0)
                            else:
                                fv = (0.0, (oy * 0.6) * (1 if dy >= 0 else -1))
                            wa = 0.0 if a.fixed else (1.0 if b.fixed else 0.5)
                            wb = 0.0 if b.fixed else (1.0 if a.fixed else 0.5)
                            if a.ref in forces:
                                forces[a.ref][0] += fv[0] * wa * 2; forces[a.ref][1] += fv[1] * wa * 2
                            if b.ref in forces:
                                forces[b.ref][0] -= fv[0] * wb * 2; forces[b.ref][1] -= fv[1] * wb * 2
            for c in self.movable:
                fx, fy = forces[c.ref]
                mag = math.hypot(fx, fy)
                if mag > temp:
                    fx, fy = fx / mag * temp, fy / mag * temp
                c.x += fx; c.y += fy
                # walls
                x0, y0, x1, y1 = c.rect()
                e = self.edge_clear
                if x0 < ORIGIN + e: c.x += ORIGIN + e - x0
                if x1 > ORIGIN + self.W - e: c.x -= x1 - (ORIGIN + self.W - e)
                if y0 < ORIGIN + e: c.y += ORIGIN + e - y0
                if y1 > ORIGIN + self.H - e: c.y -= y1 - (ORIGIN + self.H - e)
                self.apply_edge(c)
            if it % 50 == 25:
                self._best_rotations()

    def _best_rotations(self) -> None:
        for c in self.movable:
            if c.rot_fixed or not c.pins:
                continue
            best, best_r = None, c.rot
            for r in (0, 90, 180, 270):
                c.rot = r
                self.apply_edge(c)
                cost = sum(self.net_hpwl(n) for n in c.nets) + self.near_cost(c)
                if best is None or cost < best - 1e-9:
                    best, best_r = cost, r
            c.rot = best_r
            self.apply_edge(c)

    def anneal(self, moves: int = 30000) -> None:
        if not self.movable:
            return
        t0 = max(self.W, self.H) / 4
        cur = self.total_cost()["total"]
        temp = cur / max(1, len(self.comps)) * 0.2 + 1.0
        for k in range(moves):
            frac = k / moves
            T = temp * (1 - frac) ** 2 + 1e-3
            radius = max(0.2, t0 * (1 - frac))
            c = self.rng.choice(self.movable)
            mv = self.rng.random()
            nets = set(c.nets)
            saved = [(c, c.x, c.y, c.rot)]
            other = None
            if mv < 0.12 and not c.rot_fixed:
                before = sum(self.net_hpwl(n) for n in nets) + self.comp_cost(c)
                c.rot = self.rng.choice([r for r in (0, 90, 180, 270) if r != c.rot])
                self.apply_edge(c)
            elif mv < 0.2:
                cands = [o for o in self.movable if o is not c and not o.edge and not c.edge
                         and abs(o.area - c.area) / max(o.area, c.area) < 0.3]
                if not cands:
                    continue
                other = self.rng.choice(cands)
                nets |= other.nets
                saved.append((other, other.x, other.y, other.rot))
                before = sum(self.net_hpwl(n) for n in nets) + self.comp_cost(c) + self.comp_cost(other)
                c.x, other.x = other.x, c.x
                c.y, other.y = other.y, c.y
            else:
                before = sum(self.net_hpwl(n) for n in nets) + self.comp_cost(c)
                c.x += self.rng.gauss(0, radius / 2)
                c.y += self.rng.gauss(0, radius / 2)
                self.apply_edge(c)
            after = sum(self.net_hpwl(n) for n in nets) + self.comp_cost(c)
            if other is not None:
                after += self.comp_cost(other)
            delta = after - before
            if delta > 0 and self.rng.random() > math.exp(-delta / T):
                for o, x, y, r in saved:
                    o.x, o.y, o.rot = x, y, r

    def legalize(self, step: float = 0.25) -> list[str]:
        placed: list[Comp] = [c for c in self.comps.values() if c.fixed]
        problems = []
        for c in sorted(self.movable, key=lambda c: (not c.edge, -c.area)):
            c.x, c.y = round(c.x / 0.05) * 0.05, round(c.y / 0.05) * 0.05
            self.apply_edge(c)
            if self._legal(c, placed):
                placed.append(c); continue
            ox, oy = c.x, c.y
            found = False
            for ring in range(1, 400):
                pts = []
                for i in range(-ring, ring + 1):
                    pts += [(i, -ring), (i, ring), (-ring, i), (ring, i)]
                pts.sort(key=lambda p: p[0] ** 2 + p[1] ** 2)
                for dx, dy in pts:
                    c.x, c.y = ox + dx * step, oy + dy * step
                    self.apply_edge(c)
                    if self._legal(c, placed):
                        found = True; break
                if found:
                    break
            if not found:
                c.x, c.y = ox, oy
                problems.append(f"{c.ref}: no legal position found (board too small?)")
            placed.append(c)
        return problems

    def snap_pads(self, g: float) -> None:
        """Shift each part by < 1 grid step so its finest-pitch pads sit exactly on routing-grid points
        (lets the maze router escape 0.5 mm pitch pads)."""
        steps = [k * g / 4 for k in range(4)]
        for c in self.comps.values():
            fp = self.d.parts[c.ref].footprint
            pads = [p for p in fp.pads if p.kind == "smd"]
            if not pads:
                continue
            bx = round(c.x / g) * g
            by = round(c.y / g) * g
            cands = []
            for ox in steps:
                for oy in steps:
                    score = 0.0
                    for p in pads:
                        dx, dy = rot_pt(p.x, p.y, c.rot)
                        ex = abs(((bx + ox + dx - ORIGIN) / g + 0.5) % 1 - 0.5)
                        ey = abs(((by + oy + dy - ORIGIN) / g + 0.5) % 1 - 0.5)
                        score += (ex + ey) / max(0.1, min(p.w, p.h))      # narrow pads matter most
                    cands.append((score, ox, oy))
            ox0, oy0 = c.x, c.y
            before = sum(self.overlap(c, o) for o in self.comps.values() if o is not c) + self.outside(c)
            for _, ox, oy in sorted(cands):
                c.x, c.y = round(bx + ox, 4), round(by + oy, 4)
                after = sum(self.overlap(c, o) for o in self.comps.values() if o is not c) + self.outside(c)
                if after <= before + 1e-9:
                    break
            else:
                c.x, c.y = ox0, oy0

    def _legal(self, c: Comp, placed: list[Comp]) -> bool:
        if self.outside(c) > 1e-6:
            return False
        return all(self.overlap(c, o) <= 1e-6 for o in placed if o is not c)

    def run(self, iters: int = 400, moves: int = 30000) -> dict:
        for c in self.comps.values():
            if c.fixed:
                self.apply_edge(c)
        self.init_positions()
        self.force_directed(iters)
        self._best_rotations()
        self.anneal(moves)
        problems = self.legalize()
        self.snap_pads(float(self.d.rules["grid"]))
        res = self.total_cost()
        res["problems"] = problems
        res["positions"] = {r: {"x": round(c.x, 3), "y": round(c.y, 3), "rot": c.rot} for r, c in self.comps.items()}
        return res


def place(d: Design, seed: int = 1, iters: int = 400, moves: int = 30000, tries: int = 3,
          out: Path | None = None) -> dict:
    best = None
    for t in range(tries):
        p = Placer(d, seed=seed + t)
        r = p.run(iters, moves)
        score = r["total"] + 1000 * len(r["problems"])
        if best is None or score < best[0]:
            best = (score, r)
    res = best[1]
    res["board"] = {"width": d.board["width"], "height": d.board["height"], "origin": ORIGIN}
    out = Path(out or d.project_dir / "out" / "placement.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1))
    return res
