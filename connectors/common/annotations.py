"""MCP tool annotation helpers.

Each tool declares hints: readOnlyHint, destructiveHint, idempotentHint.
The policy layer uses these to determine if idempotency keys are required
and if actions should be logged.
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class ToolAnnotations:
    """Annotations for an MCP tool."""

    read_only: bool = True
    destructive: bool = False
    idempotent: bool = True

    def to_mcp_annotations(self) -> dict[str, Any]:
        return {
            "readOnlyHint": self.read_only,
            "destructiveHint": self.destructive,
            "idempotentHint": self.idempotent,
        }


# Common annotation presets
READ_ONLY = ToolAnnotations(read_only=True, destructive=False, idempotent=True)
WRITE_SAFE = ToolAnnotations(read_only=False, destructive=False, idempotent=True)
WRITE_DESTRUCTIVE = ToolAnnotations(read_only=False, destructive=True, idempotent=False)
WRITE_IDEMPOTENT = ToolAnnotations(read_only=False, destructive=True, idempotent=True)
