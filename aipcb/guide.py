"""Designer / reviewer rules sent to Claude through the MCP server (instructions + get_design_guide).

Claude Desktop does not read the repo's CLAUDE.md, so the parts of it that govern *how* to design and review
live here. Keep this in step with CLAUDE.md.
"""

SHORT = (
    "Local EDA engine (KiCad 10 + physics placer + maze router + ngspice), fully offline. Claude is the circuit "
    "designer AND the design reviewer. Workflow: create_project -> search_symbols/get_symbol_pins/"
    "search_footprints/get_footprint_pads -> write design.yaml (write_design) -> validate_design -> build_board "
    "(schematic, ERC, placement, routing, PCB, DRC, sims, checks, previews) -> fix design.yaml and rebuild until "
    "DRC has 0 errors / 0 unconnected -> get_review_package -> submit_review -> export_fab. "
    "design.yaml is the single source of truth: never edit generated .kicad_* files. Net syntax REF.PIN "
    "(pin number or name). Call get_design_guide once before designing or reviewing for the full rules."
)

GUIDE = """# aipcb design & review guide

## Designing (Claude as designer)
1. Pick parts with `search_symbols`, `get_symbol_pins`, `search_footprints`, `get_footprint_pads`. Prefer KiCad
   stock symbols/footprints. A missing chip goes in `custom_symbols:` (pins `[number, name, type, side]`) with a
   stock footprint where possible — and its pinout must be cross-checked against the datasheet in the review.
2. Nets are `REF.PIN` (pin number, or a pin name — a name shared by several pins selects all, e.g. `U1.GND`).
   Quote tokens containing braces: `"U2.V_{DD}"`. Every pin must be on a net or listed in `nc:`.
3. Placement hints in `place:` — `lock: true` keeps the part where the last placement put it;
   `edge: top|bottom|left|right` (+`edge_offset`), `x`/`y` (board-relative mm), `rot`, `near: REF.PIN`
   (+`near_dist`) for decoupling / crystals / ESD. Use `block:` to group functions.
   `build_board(keep_placement="all")` keeps placed parts and only places new ones; `unlock="R5"` re-places R5.
4. `rules.power_nets` get `power_track` width; `route_first` for critical nets. Supply rails (GND*, `+…`, and
   rail-like power_nets such as VBUS / AMP_5V; override with `rules.supply_nets`) are drawn as power symbols.
   The `schematic` stage fails unless KiCad's netlist of the sheet equals design.yaml pin for pin.
5. Describe the device in `spec:` (`summary`, `blocks:` one plain-language explanation per schematic block,
   `sections:` spec-sheet tables). Write product requirements in `requirements:` (purpose, users, scenarios,
   `functional` / `non_functional` items `{id, text, priority: 必要|建議|選配, design, verify}`, constraints,
   out_of_scope, acceptance); a `verify` naming `sim NN` links that sim's PASS/FAIL. Keep both in step with
   the circuit whenever it changes.
6. Write SPICE decks (`write_simulation`) that mirror design.yaml exactly (same topology/values) and state
   expectations with `* @expect`. Derive expected values by hand first; when a sim disagrees, decide whether the
   design or the expectation is wrong and say which.
7. `build_board` (1–4 min) -> read `get_report` checks / erc / drc; fix design.yaml (or placement hints) and
   rebuild until DRC has 0 errors / 0 unconnected.

## Reviewing (Claude as reviewer) — before any fab export
Call `get_review_package`, go through every item of `review_checklist`, verify each IC pin against datasheet
knowledge, then `submit_review` with PASS / CONDITIONAL / FAIL and concrete findings. Only PASS unlocks
`export_fab` (`force=True` is for prototypes and is recorded in the manifest). Be honest: unverifiable items
(e.g. AI-generated pinouts, LCSC numbers) are CONDITIONAL, not PASS.

## Files
- `<projects>/<name>/design.yaml` — the single source of truth (read_design / write_design).
- `<projects>/<name>/sim/*.cir` — SPICE decks; `out/` — every generated report, preview and fab file.
- `export_project` / `import_project` — archive with project/, documents/ and a sha256 manifest.
- `open_in_kicad` opens the schematic or board in the local KiCad GUI.
"""
