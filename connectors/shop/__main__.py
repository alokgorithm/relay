"""Entrypoint for running the Shop MCP server directly with python -m connectors.shop."""

from .server import mcp

if __name__ == "__main__":
    mcp.run(transport="stdio")
