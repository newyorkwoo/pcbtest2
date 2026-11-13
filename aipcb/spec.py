"""Design specification: the single source of truth Claude writes (design.yaml).

Schema (abridged):
  project: name
  title: "..."
  board: {width, height, corner_radius, fab: jlcpcb, layers: 2, thickness: 1.6}
  rules: {track, power_track, clearance, via: [dia, drill], power_nets: [...], grid,
          net_widths: {NET: mm}, route_passes: 4, stitch_pitch: 4.0}
  parts:
    U1: {symbol: Lib:Sym, footprint: Lib:Fp, value, mpn, lcsc, manufacturer, block,
         description, dnp: false, place: {x, y, rot, edge, fixed, near, side}}
  nets:
    NETNAME: [U1.3, R1.1, U2.GND]        # REF.PIN  (pin number or unique pin name;
                                          #  a name shared by several pins selects all of them)
  nc: [U1.IO35, ...]                      # pins intentionally unconnected
  notes: free text describing design intent (used for AI review)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import library
from .library import Footprint, Symbol

FAB_PROFILES = {
    "jlcpcb": {"min_track": 0.127, "min_clearance": 0.127, "min_via_drill": 0.3, "min_via_dia": 0.5,
               "min_annular": 0.13, "min_hole": 0.2, "edge_clearance": 0.3, "max_w": 400, "max_h": 500,
               "min_silk_h": 0.8, "min_silk_w": 0.15, "min_drill_to_edge": 0.4},
    "pcbway": {"min_track": 0.1, "min_clearance": 0.1, "min_via_drill": 0.2, "min_via_dia": 0.45,
               "min_annular": 0.15, "min_hole": 0.2, "edge_clearance": 0.25, "max_w": 500, "max_h": 1100,
               "min_silk_h": 0.8, "min_silk_w": 0.15, "min_drill_to_edge": 0.4},
}

DEFAULT_RULES = {"track": 0.2, "power_track": 0.4, "clearance": 0.2, "via": [0.6, 0.3],
                 "power_nets": [], "grid": 0.1, "edge_clearance": 0.4, "part_spacing": 0.3}


@dataclass
class Part:
    ref: str
    symbol_id: str
    footprint_id: str
    value: str
    data: dict
    symbol: Symbol | None = None
    footprint: Footprint | None = None

    @property
    def place(self) -> dict:
        return self.data.get("place") or {}

    @property
    def block(self) -> str:
        return self.data.get("block", "misc")


@dataclass
class Design:
    path: Path
    raw: dict
    parts: dict[str, Part] = field(default_factory=dict)
    nets: dict[str, list[tuple[str, str]]] = field(default_factory=dict)   # net -> [(ref, pin_number)]
    nc: set[tuple[str, str]] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def project_dir(self) -> Path:
        return self.path.parent

    @property
    def name(self) -> str:
        return self.raw.get("project") or self.project_dir.name

    @property
    def board(self) -> dict:
        b = {"width": 60, "height": 40, "corner_radius": 2, "fab": "jlcpcb", "layers": 2, "thickness": 1.6}
        b.update(self.raw.get("board") or {})
        return b

    @property
    def rules(self) -> dict:
        r = dict(DEFAULT_RULES)
        r.update(self.raw.get("rules") or {})
        return r

    @property
    def fab(self) -> dict:
        return FAB_PROFILES.get(self.board["fab"], FAB_PROFILES["jlcpcb"])

    def pin_net(self) -> dict[tuple[str, str], str]:
        out = {}
        for n, pins in self.nets.items():
            for rp in pins:
                out[rp] = n
        return out

    def net_width(self, net: str) -> float:
        r = self.rules
        nw = r.get("net_widths") or {}
        if net in nw:                       # per-net override (e.g. a fine-pitch power pin that 0.4 mm cannot enter)
            return float(nw[net])
        return r["power_track"] if net in r["power_nets"] else r["track"]

    def ok(self) -> bool:
        return not self.errors


def _resolve_pin(part: Part, token: str) -> list[str]:
    sym = part.symbol
    nums = {p.number for p in sym.pins}
    if token in nums:
        return [token]
    by_name = [p.number for p in sym.pins if p.name == token]
    if by_name:
        return sorted(set(by_name), key=lambda s: (len(s), s))
    by_name_ci = [p.number for p in sym.pins if p.name.lower() == token.lower()]
    if by_name_ci:
        return sorted(set(by_name_ci))
    raise KeyError(token)


def load(path: str | Path, check: bool = True) -> Design:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    d = Design(path=path, raw=raw)
    library.set_project_libs(path.parent)

    for ref, pd in (raw.get("parts") or {}).items():
        pd = pd or {}
        part = Part(ref=str(ref), symbol_id=pd.get("symbol", ""), footprint_id=pd.get("footprint", ""),
                    value=str(pd.get("value", "")), data=pd)
        try:
            part.symbol = library.get_symbol(part.symbol_id)
        except Exception as e:  # noqa: BLE001
            d.errors.append(f"{ref}: symbol '{part.symbol_id}': {e}")
        if part.footprint_id:
            try:
                part.footprint = library.get_footprint(part.footprint_id)
            except Exception as e:  # noqa: BLE001
                d.errors.append(f"{ref}: footprint '{part.footprint_id}': {e}")
        elif not (part.symbol and part.symbol.is_power):
            d.errors.append(f"{ref}: no footprint assigned")
        if not part.value:
            part.value = part.symbol.props.get("Value", "") if part.symbol else ""
        d.parts[part.ref] = part

    seen: dict[tuple[str, str], str] = {}

    def resolve(token: str, ctx: str) -> list[tuple[str, str]]:
        if "." not in token:
            d.errors.append(f"{ctx}: '{token}' must be REF.PIN")
            return []
        ref, pin = token.split(".", 1)
        part = d.parts.get(ref)
        if part is None:
            d.errors.append(f"{ctx}: unknown part '{ref}'")
            return []
        if part.symbol is None:
            return []
        try:
            return [(ref, n) for n in _resolve_pin(part, pin)]
        except KeyError:
            names = sorted({f"{p.number}:{p.name}" for p in part.symbol.pins})
            d.errors.append(f"{ctx}: {ref} has no pin '{pin}'. Pins: {names}")
            return []

    for net, members in (raw.get("nets") or {}).items():
        net = str(net)
        pins = []
        for tok in members or []:
            for rp in resolve(str(tok), f"net {net}"):
                if rp in seen and seen[rp] != net:
                    d.errors.append(f"pin {rp[0]}.{rp[1]} is in both '{seen[rp]}' and '{net}' (short!)")
                seen[rp] = net
                if rp not in pins:
                    pins.append(rp)
        d.nets[net] = pins
    for tok in raw.get("nc") or []:
        for rp in resolve(str(tok), "nc"):
            if rp in seen:
                d.errors.append(f"pin {rp[0]}.{rp[1]} listed as nc but is on net '{seen[rp]}'")
            d.nc.add(rp)

    if check:
        _validate(d, seen)
    return d


def _validate(d: Design, seen: dict) -> None:
    for ref, part in d.parts.items():
        if not part.symbol:
            continue
        for p in part.symbol.pins:
            key = (ref, p.number)
            if key not in seen and key not in d.nc:
                d.errors.append(f"{ref}.{p.number} ({p.name}, {p.etype}) is not connected and not marked nc")
        if part.footprint:
            pads = part.footprint.pad_numbers()
            spins = {p.number for p in part.symbol.pins}
            missing = spins - pads
            if missing:
                d.errors.append(f"{ref}: symbol pins {sorted(missing)} have no pad in footprint {part.footprint_id}")
            extra = {x for x in pads - spins if x and not x.upper().startswith(("MP", "SH", "S"))}
            if extra:
                d.warnings.append(f"{ref}: footprint pads {sorted(extra)} not in symbol (left unconnected)")
        if part.symbol.is_power:
            d.errors.append(f"{ref}: power symbols are added automatically; reference nets by name instead")
        bom = part.data
        if part.footprint and not bom.get("dnp") and not part.footprint_id.startswith("MountingHole"):
            if not (bom.get("mpn") or bom.get("lcsc")):
                d.warnings.append(f"{ref}: no MPN/LCSC part number (needed for assembly BOM)")
    for net, pins in d.nets.items():
        if len(pins) == 1:
            d.warnings.append(f"net '{net}' has a single pin ({pins[0][0]}.{pins[0][1]})")
        if not pins:
            d.errors.append(f"net '{net}' is empty")
    b = d.board
    fab = d.fab
    if b["width"] > fab["max_w"] or b["height"] > fab["max_h"]:
        d.errors.append(f"board {b['width']}x{b['height']} exceeds fab max")
    r = d.rules
    if r["track"] < fab["min_track"] or r["clearance"] < fab["min_clearance"]:
        d.errors.append("track/clearance below fab capability")
    if r["via"][1] < fab["min_via_drill"] or (r["via"][0] - r["via"][1]) / 2 < fab["min_annular"]:
        d.errors.append("via below fab capability")
    for n in (r.get("net_widths") or {}):
        if n not in d.nets:
            d.warnings.append(f"rules.net_widths lists unknown net '{n}'")
    for n in r["power_nets"]:
        if n not in d.nets:
            d.warnings.append(f"rules.power_nets lists unknown net '{n}'")


def summary(d: Design) -> dict:
    return {
        "project": d.name,
        "parts": len(d.parts),
        "nets": len(d.nets),
        "pins_connected": sum(len(v) for v in d.nets.values()),
        "nc_pins": len(d.nc),
        "errors": d.errors,
        "warnings": d.warnings,
        "ok": d.ok(),
    }
