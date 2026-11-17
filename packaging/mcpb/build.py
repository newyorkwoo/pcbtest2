"""Build the Claude Desktop extension: dist/aipcb.mcpb

    .venv/bin/python packaging/mcpb/build.py

Stages the aipcb engine (without the Qt GUI) next to server.py / pyproject.toml, writes manifest.json with the
tool list taken from the live MCP server, then validates and packs it with `npx @anthropic-ai/mcpb`.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
STAGE = HERE / "build" / "aipcb"
DIST = HERE / "dist"
MCPB = ["npx", "-y", "@anthropic-ai/mcpb@2.1.2"]


def tool_list() -> list[dict]:
    sys.path.insert(0, str(REPO))
    from aipcb.mcp_server import mcp
    tools = asyncio.run(mcp.list_tools())
    return [{"name": t.name, "description": (t.description or "").strip().split("\n")[0]} for t in tools]


def main() -> None:
    shutil.rmtree(STAGE, ignore_errors=True)
    STAGE.mkdir(parents=True)
    shutil.copytree(REPO / "aipcb", STAGE / "aipcb",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "gui"))
    for f in ("server.py", "pyproject.toml", ".mcpbignore", "icon.png"):
        shutil.copy2(HERE / f, STAGE / f)

    manifest = json.loads((HERE / "manifest.template.json").read_text(encoding="utf-8"))
    manifest["tools"] = tool_list()
    cfg = manifest["user_config"]
    cfg["projects_dir"]["default"] = str(REPO / "projects")
    cfg["exports_dir"]["default"] = str(REPO / "exports")
    (STAGE / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    subprocess.run([*MCPB, "validate", str(STAGE / "manifest.json")], check=True)
    DIST.mkdir(exist_ok=True)
    out = DIST / "aipcb.mcpb"
    subprocess.run([*MCPB, "pack", str(STAGE), str(out)], check=True)
    print(f"\nbuilt {out}  ({len(manifest['tools'])} tools)")


if __name__ == "__main__":
    main()
