"""Claude Desktop extension entry point: run the aipcb MCP server (stdio)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from aipcb.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    main()
