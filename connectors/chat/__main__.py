"""Entrypoint for running the Chat MCP server module directly with python -m connectors.chat."""

from .server import mcp

if __name__ == "__main__":
    mcp.run(transport="stdio")
