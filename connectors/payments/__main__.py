"""Entrypoint for running the Payments MCP server directly with python -m connectors.payments."""

from .server import mcp

if __name__ == "__main__":
    mcp.run(transport="stdio")
