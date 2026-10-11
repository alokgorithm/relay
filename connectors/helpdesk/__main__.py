"""Entrypoint for running the Helpdesk MCP server directly with python -m connectors.helpdesk."""

from .server import mcp

if __name__ == "__main__":
    mcp.run(transport="stdio")
