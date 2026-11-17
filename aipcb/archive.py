"""Project export / import: one self-contained folder (+ .zip) that can be archived and imported again later.

Export layout  (<name>_rev<rev>_<YYYYmmdd-HHMM>/):
  README.md           what is inside and how to import it again
  manifest.json       project, title, rev, export time, aipcb commit, source path, verdict, sha256 of every file
  project/            the complete project folder (design.yaml, sim/, lib/, KiCad files, docs/, out/) — what import uses
  documents/          human-readable copies: requirements / spec / description (HTML), schematic PDF+SVG, BOM,
                      wiring list, AI review, check summary, PCB renders, fab output (if exported)

Import accepts the export folder, its .zip, its manifest.json, or a bare project folder / its design.yaml.
Every file is verified against the manifest checksums; an existing project is backed up (never deleted) before it
is overwritten; absolute paths recorded in out/ are rewritten to the new location; a new name renames the KiCad
files and design.yaml `project:`.
"""
from __future__ import annotations

import csv
import hashlib
import html
import json
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

import yaml

from . import config

EXPORTS = config.EXPORTS
FORMAT = "aipcb-export/1"
SKIP_DIRS = {"__pycache__", ".history"}
SKIP_FILES = {".DS_Store", "fp-info-cache"}
TEXT_SUFFIX = {".json", ".net", ".md", ".log", ".csv", ".txt", ".html", ".svg"}


def _skip(rel: Path) -> bool:
    return (any(p in SKIP_DIRS or p.endswith("-backups") for p in rel.parts[:-1])
            or rel.name in SKIP_FILES or rel.name.endswith(".lck") or rel.name.startswith(("_autosave-", "#auto_saved")))


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _raw(pd: Path) -> dict:
    try:
        return yaml.safe_load((pd / "design.yaml").read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _commit() -> str:
    try:
        return subprocess.run(["git", "-C", str(config.ROOT), "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def fingerprint(pd: Path) -> str:
    """cheap change detector for a project: path + size + mtime of every file that would be archived
    (KiCad's local view state *.kicad_prl is ignored — opening KiCad must not count as a change)"""
    pd = Path(pd)
    h = hashlib.sha256()
    for f in sorted(pd.rglob("*")):
        rel = f.relative_to(pd)
        if f.is_file() and not _skip(rel) and f.suffix != ".kicad_prl":
            st = f.stat()
            h.update(f"{rel}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


# ------------------------------------------------------------------------------------------------ export
def _html_page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1'><title>{html.escape(title)}</title>"
            "<style>body{font-family:-apple-system,'PingFang TC','Noto Sans TC',sans-serif;max-width:1100px;"
            "margin:24px auto;padding:0 16px;line-height:1.5}</style></head><body>" + body + "</body></html>")


def _documents(pd: Path, docs: Path, raw: dict) -> list[str]:
    """human-readable copies; each step is best effort and reported"""
    notes = []
    docs.mkdir(parents=True, exist_ok=True)
    out = pd / "out"
    title = raw.get("title", pd.name)
    try:
        from .gui import design_info as di
        for fn, label, body in (("requirements.html", "設計需求書", di.requirements(raw, out)),
                                ("spec.html", "規格書", di.spec_sheet(raw, out)),
                                ("description.html", "電路說明", di.describe(raw))):
            (docs / fn).write_text(_html_page(f"{label} — {title}", body), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        notes.append(f"requirements/spec HTML: {e}")
    try:
        from . import fab, spec
        rows = fab.bom_rows(spec.load(pd / "design.yaml", check=False))
        if rows:
            with (docs / "bom.csv").open("w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    except Exception as e:  # noqa: BLE001
        notes.append(f"BOM: {e}")
    copies = [(pd / "docs", "renders"), (out / "wiring_list.csv", "wiring_list.csv"), (out / "review.md", "review.md"),
              (pd / "README.md", "project_README.md"), (out / "fab", "fab")]
    for src, dst in copies:
        if src.is_dir() and any(src.iterdir()):
            shutil.copytree(src, docs / dst, dirs_exist_ok=True)
        elif src.is_file():
            shutil.copy2(src, docs / dst)
    chk = _json(out / "checks.json")
    if chk:
        lines = [f"# 設計檢查 Checks — {title}", "",
                 f"自動檢查：{'通過' if chk.get('auto_pass') else '未通過'}；counts {chk.get('counts')}；DRC {chk.get('drc')}", ""]
        lines += [f"- [{x['severity']}] {x['category']}: {x['message']}" for x in chk.get("findings", [])]
        (docs / "checks.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    sims = _json(out / "sim" / "summary.json")
    if sims:
        lines = [f"# SPICE 模擬 — {title}", ""]
        for s in sims:
            lines.append(f"## {'PASS' if s.get('ok') else 'FAIL'} {s['name']} — {s.get('title', '')}")
            lines += [f"- {c['meas']} = {c['value']:.5g}  (expect {c['min']} … {c['max']})  "
                      f"{'PASS' if c['pass'] else 'FAIL'}" for c in s.get("checks", [])]
            lines.append("")
        (docs / "simulation.md").write_text("\n".join(lines), encoding="utf-8")
    return notes


def export_project(pd: Path, dest: Path | None = None, make_zip: bool = True) -> dict:
    pd = Path(pd).resolve()
    if not (pd / "design.yaml").exists():
        raise FileNotFoundError(f"{pd} 不是 aipcb 專案（找不到 design.yaml）")
    raw = _raw(pd)
    name = pd.name
    stamp = time.strftime("%Y%m%d-%H%M")
    dest = Path(dest or EXPORTS).expanduser().resolve()
    root = dest / f"{name}_rev{raw.get('rev', 'A')}_{stamp}"
    n = 2
    while root.exists() or root.with_suffix(".zip").exists():
        root = dest / f"{name}_rev{raw.get('rev', 'A')}_{stamp}_{n}"; n += 1
    proj = root / "project"
    proj.mkdir(parents=True)
    for f in sorted(pd.rglob("*")):
        rel = f.relative_to(pd)
        if f.is_file() and not _skip(rel):
            (proj / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, proj / rel)
    notes = _documents(proj, root / "documents", raw)

    st = _json(pd / "out" / "status.json") or {}
    rv = _json(pd / "out" / "review.json") or {}
    chk = _json(pd / "out" / "checks.json") or {}
    sims = _json(pd / "out" / "sim" / "summary.json") or []
    summary = {
        "verdict": rv.get("verdict"), "review_time": rv.get("time"),
        "drc": (chk.get("drc") or {}), "auto_pass": chk.get("auto_pass"),
        "sims": f"{sum(1 for s in sims if s.get('ok'))}/{len(sims)}" if sims else None,
        "stages": {k: (v or {}).get("state") for k, v in st.items() if isinstance(v, dict)},
    }
    readme = [
        f"# {raw.get('title', name)} — aipcb 專案匯出", "",
        f"- 專案名稱：`{name}`，版次 rev {raw.get('rev', 'A')}",
        f"- 匯出時間：{time.strftime('%Y-%m-%d %H:%M')}；aipcb 版本 {_commit() or '?'}",
        f"- AI 審核：{summary['verdict'] or '尚未審核'}；DRC {summary['drc'] or '—'}；SPICE 模擬 {summary['sims'] or '—'}", "",
        "## 內容", "",
        "| 路徑 | 內容 |", "|---|---|",
        "| `project/` | 完整專案（design.yaml、SPICE、元件庫、KiCad 檔、所有產出）— **匯入時使用這個** |",
        "| `documents/requirements.html` | 設計需求書 |",
        "| `documents/spec.html` | 規格書 |",
        "| `documents/description.html` | 電路說明（各功能區塊） |",
        "| `documents/renders/` | 原理圖 PDF / SVG、PCB 圖 |",
        "| `documents/bom.csv`、`wiring_list.csv` | 物料清單、實體接線表 |",
        "| `documents/review.md`、`checks.md`、`simulation.md` | AI 審核、設計檢查、模擬結果 |",
        "| `documents/fab/` | 送廠檔（若已輸出） |",
        "| `manifest.json` | 每個檔案的 SHA-256，匯入時用來驗證完整性 |", "",
        "## 如何再匯入", "",
        "- 桌面程式：左側「📥 匯入專案」→ 選這個資料夾裡的 `manifest.json`，或整個 `.zip`",
        f"- 指令列：`.venv/bin/python -m aipcb import \"{root.name}.zip\"`（加 `--name 新名稱` 另存、`--overwrite` 覆蓋）",
        "- 匯入後執行「全流程 Build」即可在本機重新產生所有檔案", "",
    ]
    if notes:
        readme += ["## 匯出備註", ""] + [f"- {x}" for x in notes] + [""]
    (root / "README.md").write_text("\n".join(readme), encoding="utf-8")

    files = {str(f.relative_to(root)): _sha(f) for f in sorted(root.rglob("*")) if f.is_file()}
    manifest = {"format": FORMAT, "project": name, "title": raw.get("title"), "rev": raw.get("rev", "A"),
                "exported": time.strftime("%Y-%m-%dT%H:%M:%S"), "aipcb_commit": _commit(), "source_dir": str(pd),
                "summary": summary, "notes": notes, "files": files}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")

    zpath = None
    if make_zip:
        zpath = root.with_suffix(".zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(root.rglob("*")):
                if f.is_file():
                    z.write(f, Path(root.name) / f.relative_to(root))
    size = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
    return {"ok": True, "folder": str(root), "zip": str(zpath) if zpath else None, "files": len(files) + 1,
            "bytes": size, "notes": notes}


# ------------------------------------------------------------------------------------------------ new / duplicate
NAME_RE = r"[A-Za-z0-9_][A-Za-z0-9_.-]*"


def new_project(name: str, title: str = "", width_mm: float = 60, height_mm: float = 40) -> Path:
    """projects/<name>/ with a skeleton design.yaml (same skeleton the MCP create_project tool writes)"""
    if not re.fullmatch(NAME_RE, name or ""):
        raise ValueError(f"專案名稱只能用英數字、底線、點、減號：{name!r}")
    pd = config.PROJECTS / name
    if (pd / "design.yaml").exists():
        raise FileExistsError(f"專案 {name} 已存在")
    (pd / "sim").mkdir(parents=True, exist_ok=True)
    skel = {"project": name, "title": title or name, "rev": "A", "notes": "",
            "board": {"width": width_mm, "height": height_mm, "corner_radius": 2, "layers": 2, "fab": "jlcpcb"},
            "rules": {"track": 0.2, "power_track": 0.4, "clearance": 0.2, "via": [0.6, 0.3], "power_nets": []},
            "parts": {}, "nets": {}, "nc": []}
    (pd / "design.yaml").write_text(yaml.safe_dump(skel, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return pd


def duplicate_project(pd: Path, new: str) -> dict:
    """Save As: copy the project (with its outputs) under a new name"""
    return import_project(Path(pd), name=new, overwrite=False, verify=False)


# ------------------------------------------------------------------------------------------------ import
def _locate(src: Path, tmp: Path) -> tuple[Path, Path, dict | None]:
    """-> (export root or None-equivalent, project folder, manifest)"""
    src = Path(src).expanduser().resolve()
    if src.is_file() and src.suffix.lower() == ".zip":
        with zipfile.ZipFile(src) as z:
            for m in z.namelist():
                if m.startswith("/") or ".." in Path(m).parts:
                    raise ValueError(f"zip 內含不安全的路徑：{m}")
            z.extractall(tmp)
        hits = list(tmp.rglob("manifest.json")) or list(tmp.rglob("design.yaml"))
        if not hits:
            raise FileNotFoundError("zip 內找不到 manifest.json 或 design.yaml")
        src = min(hits, key=lambda p: len(p.parts))
    if src.is_file():
        src = src.parent
    if (src / "manifest.json").exists() and (src / "project" / "design.yaml").exists():
        return src, src / "project", _json(src / "manifest.json")
    if (src / "design.yaml").exists():
        return src, src, None
    raise FileNotFoundError(f"{src} 不是 aipcb 匯出資料夾或專案（需要 manifest.json + project/，或 design.yaml）")


def _verify(root: Path, manifest: dict) -> list[str]:
    bad = []
    for rel, h in (manifest.get("files") or {}).items():
        p = root / rel
        if not p.exists():
            bad.append(f"缺少 {rel}")
        elif _sha(p) != h:
            bad.append(f"內容不符 {rel}")
    return bad


def inspect(src: Path) -> dict:
    """what an import of `src` would do (for the GUI's confirm dialog)"""
    with tempfile.TemporaryDirectory() as t:
        root, proj, man = _locate(Path(src), Path(t))
        raw = _raw(proj)
        name = (man or {}).get("project") or proj.name
        return {"name": name, "title": raw.get("title"), "rev": raw.get("rev"), "exported": (man or {}).get("exported"),
                "exists": (config.PROJECTS / name).exists(), "summary": (man or {}).get("summary")}


def import_project(src: Path, name: str | None = None, overwrite: bool = False, verify: bool = True) -> dict:
    with tempfile.TemporaryDirectory() as t:
        root, proj, man = _locate(Path(src), Path(t))
        if man and verify:
            bad = _verify(root, man)
            if bad:
                raise ValueError("匯出資料不完整或已被修改：\n" + "\n".join(bad[:20]) +
                                 (f"\n…共 {len(bad)} 項" if len(bad) > 20 else ""))
        old = (man or {}).get("project") or proj.name
        new = name or old
        if not re.fullmatch(NAME_RE, new):
            raise ValueError(f"專案名稱只能用英數字、底線、點、減號：{new!r}")
        target = config.PROJECTS / new
        backup = None
        if target.exists():
            if not overwrite:
                raise FileExistsError(f"專案 {new} 已存在：請指定新名稱（--name）或覆蓋（--overwrite）")
            EXPORTS.mkdir(parents=True, exist_ok=True)
            backup = EXPORTS / f"{new}_before_import_{time.strftime('%Y%m%d-%H%M%S')}"
            shutil.move(str(target), str(backup))
        shutil.copytree(proj, target, ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))

        # rename: KiCad files carry the project name, design.yaml `project:` too
        if new != old:
            for f in list(target.iterdir()):
                if f.name.startswith(old + ".") or f.name.startswith(old + "-"):
                    f.rename(target / (new + f.name[len(old):]))
            dy = target / "design.yaml"
            dy.write_text(re.sub(r"(?m)^project:\s*\S+", f"project: {new}", dy.read_text(encoding="utf-8"), count=1),
                          encoding="utf-8")
        # absolute paths recorded by the pipeline (status, previews, sims, schematic map) -> new location
        olddir = (man or {}).get("source_dir") if man else str(proj)
        fixed = 0
        if olddir and olddir != str(target):
            for f in (target / "out").rglob("*"):
                if f.is_file() and f.suffix.lower() in TEXT_SUFFIX and f.stat().st_size < 20_000_000:
                    try:
                        s = f.read_text(encoding="utf-8")
                    except UnicodeDecodeError:
                        continue
                    s2 = s.replace(olddir, str(target))
                    if new != old:
                        s2 = s2.replace(f"{target}/{old}.", f"{target}/{new}.")
                    if s2 != s:
                        f.write_text(s2, encoding="utf-8"); fixed += 1
        return {"ok": True, "project": new, "path": str(target), "renamed_from": old if new != old else None,
                "backup_of_previous": str(backup) if backup else None, "verified": bool(man and verify),
                "paths_rewritten": fixed}
