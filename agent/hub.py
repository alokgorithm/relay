from __future__ import annotations
import asyncio
import json
import logging
import yaml
import sys
from pathlib import Path
from typing import Any
from dataclasses import dataclass, field

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent


@dataclass
class ConnectorConfig:
    """Configuration for a single connector from connectors.yaml."""
    name: str
    command: list[str]
    scopes: list[str]  # read, write
    approval_required: dict[str, dict] = field(default_factory=dict)
    timeout_s: int = 30
    retry: dict[str, Any] = field(default_factory=lambda: {"max": 1, "on": []})


@dataclass
class ToolInfo:
    """Information about a discovered MCP tool."""
    name: str
    description: str
    input_schema: dict[str, Any]
    connector_name: str
    annotations: dict[str, Any] = field(default_factory=dict)
    
    @property
    def is_read_only(self) -> bool:
        if "readOnlyHint" in self.annotations:
            return bool(self.annotations["readOnlyHint"])
        desc = (self.description or "").upper()
        if "[READ ONLY]" in desc or "[READ]" in desc:
            return True
        if self.name.startswith(("get_", "list_", "search_", "check_")):
            return True
        return False
    
    @property
    def is_destructive(self) -> bool:
        return self.annotations.get("destructiveHint", False)
    
    @property
    def is_idempotent(self) -> bool:
        return self.annotations.get("idempotentHint", True)
    
    def to_openai_tool(self) -> dict[str, Any]:
        """Convert to OpenAI function calling format."""
        return {
            "type": "function",
            "function": {
                "name": f"{self.connector_name}__{self.name}",
                "description": f"[{self.connector_name}] {self.description}",
                "parameters": self.input_schema,
            }
        }


class ConnectorHub:
    """MCP client host that manages connector processes and routes tool calls.
    
    Responsibilities:
    - Load connectors.yaml and start MCP server processes
    - Discover tools from each connector via MCP protocol
    - Enforce scope-based access control (read/write)
    - Convert MCP tool schemas to OpenAI format
    - Route tool calls to the right connector
    - Handle retries and timeouts per connector config
    - Filter tools based on playbook requirements
    """
    
    def __init__(self, config_path: Path | None = None):
        self.config_path = config_path or (ROOT_DIR / "connectors.yaml")
        self.connectors: dict[str, ConnectorConfig] = {}
        self.tools: dict[str, ToolInfo] = {}  # keyed by "connector__tool"
        self._sessions: dict[str, ClientSession] = {}
        self._cleanup_fns: list[Any] = []
    
    def load_config(self) -> None:
        """Load connector configurations from connectors.yaml."""
        with open(self.config_path) as f:
            raw = yaml.safe_load(f)
        
        for name, cfg in raw.get("connectors", {}).items():
            self.connectors[name] = ConnectorConfig(
                name=name,
                command=cfg["command"],
                scopes=cfg.get("scopes", ["read"]),
                approval_required=cfg.get("approval_required", {}),
                timeout_s=cfg.get("timeout_s", 30),
                retry=cfg.get("retry", {"max": 1, "on": []}),
            )
        logger.info(f"Loaded {len(self.connectors)} connectors: {list(self.connectors.keys())}")
    
    async def start_all(self) -> None:
        """Start all connector MCP server processes and discover tools."""
        for name, config in self.connectors.items():
            try:
                await self._start_connector(name, config)
            except Exception as e:
                logger.error(f"Failed to start connector '{name}': {e}")
    
    async def _start_connector(self, name: str, config: ConnectorConfig) -> None:
        """Start a single connector and discover its tools."""
        cmd = config.command[0]
        if cmd == "python":
            cmd = sys.executable
        server_params = StdioServerParameters(
            command=cmd,
            args=config.command[1:],
            cwd=str(ROOT_DIR),
        )
        
        # Create the stdio client context managers - we need to keep them alive
        read_stream, write_stream = await self._create_streams(name, server_params)
        session = ClientSession(read_stream, write_stream)
        await session.__aenter__()
        self._cleanup_fns.append(session.__aexit__)
        await session.initialize()
        self._sessions[name] = session
        
        # Discover tools
        tools_result = await session.list_tools()
        for tool in tools_result.tools:
            qualified_name = f"{name}__{tool.name}"
            annotations = {}
            if hasattr(tool, 'annotations') and tool.annotations:
                ann = tool.annotations
                if isinstance(ann, dict):
                    if 'readOnlyHint' in ann:
                        annotations['readOnlyHint'] = ann['readOnlyHint']
                    if 'destructiveHint' in ann:
                        annotations['destructiveHint'] = ann['destructiveHint']
                    if 'idempotentHint' in ann:
                        annotations['idempotentHint'] = ann['idempotentHint']
                else:
                    if hasattr(ann, 'readOnlyHint'):
                        annotations['readOnlyHint'] = ann.readOnlyHint
                    if hasattr(ann, 'destructiveHint'):
                        annotations['destructiveHint'] = ann.destructiveHint  
                    if hasattr(ann, 'idempotentHint'):
                        annotations['idempotentHint'] = ann.idempotentHint
            
            # Enforce scopes: don't expose write tools on read-only connectors
            if "readOnlyHint" in annotations:
                is_read_only = annotations["readOnlyHint"]
            else:
                desc_upper = (tool.description or "").upper()
                is_read_only = (
                    "[READ ONLY]" in desc_upper
                    or "[READ]" in desc_upper
                    or tool.name.startswith(("get_", "list_", "search_", "check_"))
                )
                annotations["readOnlyHint"] = is_read_only

            if not is_read_only and "write" not in config.scopes:
                logger.info(f"Skipping write tool '{tool.name}' on read-scoped connector '{name}'")
                continue
            
            self.tools[qualified_name] = ToolInfo(
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.inputSchema if hasattr(tool, 'inputSchema') else {},
                connector_name=name,
                annotations=annotations,
            )
        
        logger.info(f"Connector '{name}': {len([t for t in self.tools.values() if t.connector_name == name])} tools")
    
    async def _create_streams(self, name: str, server_params: StdioServerParameters):
        """Create stdio streams to an MCP server process.
        
        This is a simplified approach - in production, you'd use the full
        context manager lifecycle. Here we manage it manually for the hub.
        """
        from mcp.client.stdio import stdio_client
        
        # We need to use the context manager but keep it alive
        cm = stdio_client(server_params)
        streams = await cm.__aenter__()
        self._cleanup_fns.append(cm.__aexit__)
        return streams
    
    def get_tools_for_playbook(self, playbook_connectors: list[str]) -> list[dict[str, Any]]:
        """Get OpenAI-format tools filtered by playbook's required connectors.
        
        This keeps prompts small for free/local models by only including
        tools from connectors named in the selected playbook.
        """
        result = []
        for qualified_name, tool_info in self.tools.items():
            if tool_info.connector_name in playbook_connectors:
                result.append(tool_info.to_openai_tool())
        return result
    
    def get_all_tools(self) -> list[dict[str, Any]]:
        """Get all available tools in OpenAI format."""
        return [t.to_openai_tool() for t in self.tools.values()]
    
    def get_tool_info(self, qualified_name: str) -> ToolInfo | None:
        """Get info about a specific tool."""
        return self.tools.get(qualified_name)
    
    def parse_tool_name(self, qualified_name: str) -> tuple[str, str]:
        """Parse 'connector__tool' into (connector, tool)."""
        parts = qualified_name.split("__", 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid tool name: {qualified_name}")
        return parts[0], parts[1]
    
    async def call_tool(
        self,
        qualified_name: str,
        args: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Call a tool on the appropriate connector with retry logic.
        
        Args:
            qualified_name: 'connector__tool' format
            args: tool arguments
            idempotency_key: for write operations, pass to the tool if it accepts one
        """
        connector_name, tool_name = self.parse_tool_name(qualified_name)
        
        if connector_name not in self._sessions:
            raise RuntimeError(f"Connector '{connector_name}' not connected")
        
        config = self.connectors[connector_name]
        session = self._sessions[connector_name]
        
        # Add idempotency key to args if provided and tool accepts it
        if idempotency_key:
            tool_info = self.tools.get(qualified_name)
            if tool_info and "idempotency_key" in tool_info.input_schema.get("properties", {}):
                args = {**args, "idempotency_key": idempotency_key}
        
        # Retry logic
        max_retries = config.retry.get("max", 1)
        retry_on = config.retry.get("on", [])
        last_error = None
        
        for attempt in range(1, max_retries + 1):
            try:
                result = await asyncio.wait_for(
                    session.call_tool(tool_name, args),
                    timeout=config.timeout_s,
                )
                
                # Parse the result
                if result.content:
                    for content_item in result.content:
                        if hasattr(content_item, 'text'):
                            try:
                                return json.loads(content_item.text)
                            except json.JSONDecodeError:
                                return {"result": content_item.text}
                return {"result": "success", "content": str(result.content)}
                
            except asyncio.TimeoutError as e:
                last_error = e
                if "timeout" in retry_on and attempt < max_retries:
                    logger.warning(f"{qualified_name} timed out (attempt {attempt}/{max_retries})")
                    continue
                raise TimeoutError(f"{qualified_name} timed out after {config.timeout_s}s - action may have succeeded") from e
            
            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                if ("unavailable" in error_str or "503" in error_str) and "unavailable" in retry_on and attempt < max_retries:
                    logger.warning(f"{qualified_name} unavailable (attempt {attempt}/{max_retries})")
                    await asyncio.sleep(min(2 ** attempt, 10))
                    continue
                raise
        
        raise RuntimeError(f"{qualified_name} failed after {max_retries} retries: {last_error}")
    
    def check_approval_required(self, qualified_name: str, args: dict[str, Any]) -> dict | None:
        """Check if a tool call requires approval.
        
        Returns the approval rule if approval is needed, None otherwise.
        """
        connector_name, tool_name = self.parse_tool_name(qualified_name)
        config = self.connectors.get(connector_name)
        if not config:
            return None
        
        rules = config.approval_required.get(tool_name)
        if not rules:
            return None
        
        # Check amount threshold
        if "amount_gt" in rules:
            amount = args.get("amount", 0)
            if amount > rules["amount_gt"]:
                return {"reason": f"Amount {amount} exceeds auto-approve limit {rules['amount_gt']}", "rule": rules}
        
        return None
    
    async def shutdown(self) -> None:
        """Shut down all connector processes."""
        for cleanup_fn in reversed(self._cleanup_fns):
            try:
                await cleanup_fn(None, None, None)
            except Exception as e:
                logger.debug(f"Cleanup error: {e}")
        self._sessions.clear()
        self.tools.clear()
        self._cleanup_fns.clear()
    
    def list_connectors(self) -> list[dict[str, Any]]:
        """List all configured connectors and their tools."""
        result = []
        for name, config in self.connectors.items():
            tools = [t for t in self.tools.values() if t.connector_name == name]
            result.append({
                "name": name,
                "scopes": config.scopes,
                "tools": [t.name for t in tools],
                "connected": name in self._sessions,
            })
        return result
