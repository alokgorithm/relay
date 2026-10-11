"""Entrypoint for running the Inventory MCP server module directly with python -m connectors.inventory."""

from .server import mcp

if __name__ == "__main__":
    mcp.run(transport="stdio")
