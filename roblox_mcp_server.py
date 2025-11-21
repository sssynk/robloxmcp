"""Roblox MCP bridge server.

This file spins up two pieces of infrastructure:
- A websocket listener that Roblox Studio connects to via the WebStreamClient API.
- A Model Context Protocol (MCP) server that exposes Roblox-aware tools to LLM runtimes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import threading
import uuid
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Dict, Optional

from mcp.server.fastmcp import Context, FastMCP
from websockets.exceptions import ConnectionClosed
from websockets.server import WebSocketServerProtocol, serve

LOG_LEVEL = os.getenv("ROBLOX_BRIDGE_LOG", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))


class RobloxBridgeError(RuntimeError):
    """Base exception for bridge failures."""


class RobloxBridgeTimeout(RobloxBridgeError):
    """Raised when the Studio plugin does not answer in time."""


@dataclass(slots=True)
class PendingRequest:
    future: asyncio.Future
    command: str


class RobloxBridge:
    """Manages the websocket backchannel to Roblox Studio."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._connected = asyncio.Event()
        self._stop = asyncio.Event()
        self._ready = threading.Event()  # Thread-safe event for cross-thread signaling
        self._websocket: WebSocketServerProtocol | None = None
        self._pending: dict[str, PendingRequest] = {}
        self._server: Serve | None = None

    async def serve_forever(self) -> None:
        """Start the websocket server and block until shutdown is requested."""
        if self._server is not None:
            raise RuntimeError("Bridge server already running")

        async with serve(
            self._handle_connection,
            self.host,
            self.port,
            ping_interval=None,  # Disable pings in case Roblox doesn't support them
            ping_timeout=None,
        ) as server:
            self._server = server
            logging.info("Roblox bridge listening on ws://%s:%s", self.host, self.port)
            self._ready.set()
            try:
                await self._stop.wait()
            finally:
                self._server = None
                logging.info("Roblox bridge listener closed")

    async def shutdown(self) -> None:
        """Stop accepting new clients and close the active connection."""
        self._stop.set()
        if self._websocket and not self._websocket.closed:
            await self._websocket.close(code=4000, reason="Server shutdown")

    async def wait_until_ready(self, timeout: float | None = 10.0) -> None:
        """Block until a Roblox plugin has connected (or until timeout)."""
        if self._connected.is_set():
            return
        if timeout is None:
            await self._connected.wait()
            return
        await asyncio.wait_for(self._connected.wait(), timeout=timeout)

    async def get_children(self, path: str, timeout: float | None = 10.0) -> dict[str, Any]:
        data = await self._request("GET_CHILDREN", {"path": path}, timeout=timeout)
        return data or {}

    async def read_script(self, path: str, timeout: float | None = 10.0) -> dict[str, Any]:
        data = await self._request("READ_SCRIPT", {"path": path}, timeout=timeout)
        return data or {}

    async def search_scripts(self, search_string: str, timeout: float | None = 30.0) -> dict[str, Any]:
        data = await self._request("SEARCH_SCRIPTS", {"searchString": search_string}, timeout=timeout)
        return data or {}

    async def search_objects(self, search_string: str, search_root: str = "game/Workspace", timeout: float | None = 30.0) -> dict[str, Any]:
        data = await self._request("SEARCH_OBJECTS", {"searchString": search_string, "searchRoot": search_root}, timeout=timeout)
        return data or {}

    async def write_script(self, path: str, source: str, timeout: float | None = 10.0) -> dict[str, Any]:
        data = await self._request("WRITE_SCRIPT", {"path": path, "source": source}, timeout=timeout)
        return data or {}

    async def create_instance(self, class_name: str, parent_path: str, name: str | None = None, properties: dict[str, Any] | None = None, timeout: float | None = 10.0) -> dict[str, Any]:
        payload = {"className": class_name, "parentPath": parent_path}
        if name:
            payload["name"] = name
        if properties:
            payload["properties"] = properties
        data = await self._request("CREATE_INSTANCE", payload, timeout=timeout)
        return data or {}

    async def delete_instance(self, path: str, timeout: float | None = 10.0) -> dict[str, Any]:
        data = await self._request("DELETE_INSTANCE", {"path": path}, timeout=timeout)
        return data or {}

    async def set_property(self, path: str, property_name: str, property_value: Any, timeout: float | None = 10.0) -> dict[str, Any]:
        data = await self._request("SET_PROPERTY", {"path": path, "propertyName": property_name, "propertyValue": property_value}, timeout=timeout)
        return data or {}

    async def _request(
        self,
        command: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = 10.0,
    ) -> Any:
        await self.wait_until_ready(timeout=timeout)
        if not self._websocket or self._websocket.closed:
            raise RobloxBridgeError("Roblox Studio plugin is not connected")

        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[request_id] = PendingRequest(future=future, command=command)
        message: Dict[str, Any] = {"type": command, "requestId": request_id}
        if payload:
            message.update(payload)
        encoded = json.dumps(message)
        await self._websocket.send(encoded)
        logging.debug("-> Roblox %s %s", command, payload)

        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            if not future.done():
                future.set_exception(RobloxBridgeTimeout(f"Timed out waiting for {command}"))
            self._pending.pop(request_id, None)
            raise RobloxBridgeTimeout(f"Timed out waiting for {command}") from exc

    async def _handle_connection(self, websocket: WebSocketServerProtocol) -> None:
        """Handle lifecycle of an individual Roblox Studio plugin connection."""
        if self._websocket:
            logging.warning("Replacing existing Roblox Studio connection")
            await self._websocket.close(code=4001, reason="Superseded")

        self._websocket = websocket
        self._connected.set()
        logging.info("Roblox plugin connected from %s", websocket.remote_address)

        try:
            async for raw_message in websocket:
                if isinstance(raw_message, bytes):
                    raw_text = raw_message.decode("utf-8", errors="ignore")
                else:
                    raw_text = raw_message
                await self._dispatch(raw_text)
        except ConnectionClosed:
            logging.warning("Roblox plugin websocket closed")
        finally:
            self._connected.clear()
            self._websocket = None
            self._fail_pending(RuntimeError("Roblox Studio disconnected"))

    async def _dispatch(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            logging.error("Received invalid JSON from Roblox: %s", raw)
            return

        msg_type = str(message.get("type"))
        if msg_type == "response":
            request_id = message.get("requestId")
            pending = self._pending.pop(request_id, None)
            if not pending:
                logging.warning("No pending call for request %s", request_id)
                return
            if message.get("success", True):
                pending.future.set_result(message.get("data"))
            else:
                error_text = message.get("error", "Roblox plugin reported failure")
                pending.future.set_exception(RobloxBridgeError(error_text))
            return

        if msg_type == "event":
            logging.info("Roblox event: %s", message.get("event"))
            return

        if msg_type == "hello":
            logging.info(
                "Roblox plugin ready (version=%s, placeId=%s)",
                message.get("version"),
                message.get("placeId"),
            )
            return

        logging.info("Unhandled Roblox message: %s", message)

    def _fail_pending(self, exc: Exception) -> None:
        for request_id, pending in list(self._pending.items()):
            if not pending.future.done():
                pending.future.set_exception(exc)
        self._pending.clear()


BRIDGE_HOST = os.getenv("ROBLOX_BRIDGE_HOST", "127.0.0.1")
BRIDGE_PORT = int(os.getenv("ROBLOX_BRIDGE_PORT", "9090"))
DEFAULT_TIMEOUT = float(os.getenv("ROBLOX_BRIDGE_TIMEOUT", "12"))
HTTP_HOST = os.getenv("MCP_HTTP_HOST", "127.0.0.1")
HTTP_PORT = int(os.getenv("MCP_HTTP_PORT", "8000"))
bridge = RobloxBridge(BRIDGE_HOST, BRIDGE_PORT)


mcp_app = FastMCP(
    name="roblox-mcp-bridge",
    instructions=(
        "This MCP server provides READ and WRITE access to live Roblox Studio data via a websocket bridge. "
        "Use these tools to query the Roblox game hierarchy, read/write script contents, create/delete objects, and search for code. "
        "\n\nIMPORTANT: When the user asks about Roblox code or objects, ALWAYS use these MCP tools. "
        "DO NOT attempt to read/write Roblox files from the local filesystem - the game data is only accessible through this bridge. "
        "\n\nWRITE OPERATIONS: When modifying scripts, ALWAYS read the current script first, then write the complete new version. "
        "Do not attempt to patch - provide the full script source. The user will see what you're writing and can undo if needed. "
        "\n\nMake sure the Roblox Studio plugin is connected before making requests (use wait_for_roblox if needed)."
    ),
    host=HTTP_HOST,
    port=HTTP_PORT,
)


@mcp_app.tool(
    name="wait_for_roblox",
    description="Block until the Roblox Studio plugin websocket is connected.",
)
async def wait_for_roblox(timeout_seconds: float = DEFAULT_TIMEOUT, ctx: Context | None = None) -> str:
    await bridge.wait_until_ready(timeout=None if timeout_seconds <= 0 else timeout_seconds)
    if ctx:
        ctx.info("Roblox plugin connected")
    return "Roblox Studio plugin is connected"


@mcp_app.tool(
    name="list_children",
    description="Return the immediate children for the object at the provided Roblox path.",
)
async def list_children(path: str, timeout_seconds: float = DEFAULT_TIMEOUT, ctx: Context | None = None) -> dict[str, Any]:
    data = await bridge.get_children(path.strip(), timeout=None if timeout_seconds <= 0 else timeout_seconds)
    if ctx:
        ctx.info(f"Fetched {len(data.get('children', []))} children from {data.get('path', path)}")
    return data


@mcp_app.tool(
    name="read_script",
    description="Fetch the source code for a Script/LocalScript/ModuleScript at the given Roblox path.",
)
async def read_script(path: str, timeout_seconds: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    return await bridge.read_script(path.strip(), timeout=None if timeout_seconds <= 0 else timeout_seconds)


@mcp_app.tool(
    name="search_for_string",
    description="Search all scripts in the Roblox game for a specific string (case-insensitive). Returns a list of scripts containing the string.",
)
async def search_for_string(search_string: str, timeout_seconds: float = 30.0, ctx: Context | None = None) -> dict[str, Any]:
    data = await bridge.search_scripts(search_string.strip(), timeout=None if timeout_seconds <= 0 else timeout_seconds)
    if ctx:
        ctx.info(f"Found {data.get('count', 0)} scripts containing '{search_string}'")
    return data


@mcp_app.tool(
    name="search_for_object",
    description="Search for objects in the Roblox game hierarchy by name (fuzzy/partial match, case-insensitive). Defaults to searching in Workspace.",
)
async def search_for_object(
    search_string: str,
    search_root: str = "game/Workspace",
    timeout_seconds: float = 30.0,
    ctx: Context | None = None
) -> dict[str, Any]:
    data = await bridge.search_objects(
        search_string.strip(),
        search_root.strip(),
        timeout=None if timeout_seconds <= 0 else timeout_seconds
    )
    if ctx:
        ctx.info(f"Found {data.get('count', 0)} objects matching '{search_string}' in {search_root}")
    return data


@mcp_app.tool(
    name="write_script",
    description="WRITE OPERATION: Overwrites the entire source code of a Script/LocalScript/ModuleScript. Always read the current script first, then provide the complete new source code.",
)
async def write_script(
    path: str,
    source: str,
    timeout_seconds: float = DEFAULT_TIMEOUT,
    ctx: Context | None = None
) -> dict[str, Any]:
    if ctx:
        ctx.info(f"Writing {len(source)} bytes to {path}")
    data = await bridge.write_script(
        path.strip(),
        source,
        timeout=None if timeout_seconds <= 0 else timeout_seconds
    )
    if ctx:
        ctx.info(f"Successfully wrote script at {path}")
    return data


@mcp_app.tool(
    name="create_instance",
    description=(
        "WRITE OPERATION: Creates a new Roblox instance (Part, Folder, Script, LocalScript, ModuleScript, etc.) at the specified parent path. "
        "You can optionally set properties during creation. Use JSON objects for special types (Vector3, Color3, etc.) - see set_property for format examples."
    ),
)
async def create_instance(
    class_name: str,
    parent_path: str,
    name: str = "",
    properties: dict[str, Any] | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT,
    ctx: Context | None = None
) -> dict[str, Any]:
    if ctx:
        ctx.info(f"Creating {class_name} named '{name}' in {parent_path}")
    data = await bridge.create_instance(
        class_name.strip(),
        parent_path.strip(),
        name.strip() if name else None,
        properties,
        timeout=None if timeout_seconds <= 0 else timeout_seconds
    )
    if ctx:
        ctx.info(f"Successfully created {class_name} at {data.get('path', 'unknown path')}")
    return data


@mcp_app.tool(
    name="delete_instance",
    description="WRITE OPERATION: Permanently deletes a Roblox instance and all its children. Use with caution - this cannot be undone!",
)
async def delete_instance(
    path: str,
    timeout_seconds: float = DEFAULT_TIMEOUT,
    ctx: Context | None = None
) -> dict[str, Any]:
    if ctx:
        ctx.info(f"Deleting instance at {path}")
    data = await bridge.delete_instance(
        path.strip(),
        timeout=None if timeout_seconds <= 0 else timeout_seconds
    )
    if ctx:
        ctx.info(f"Successfully deleted {data.get('className', 'instance')} at {path}")
    return data


@mcp_app.tool(
    name="set_property",
    description=(
        "WRITE OPERATION: Sets a property value on a Roblox instance. "
        "Common properties: Name (string), Transparency (number), Anchored (boolean), etc. "
        "For special types, use JSON objects: "
        "Vector2: {\"X\": 0.5, \"Y\": 0.5}, "
        "Vector3: {\"X\": 0, \"Y\": 5, \"Z\": 0}, "
        "Color3: {\"R\": 1, \"G\": 0, \"B\": 0}, "
        "UDim2: {\"xScale\": 0.5, \"xOffset\": 0, \"yScale\": 0.5, \"yOffset\": 0}"
    ),
)
async def set_property(
    path: str,
    property_name: str,
    property_value: Any,
    timeout_seconds: float = DEFAULT_TIMEOUT,
    ctx: Context | None = None
) -> dict[str, Any]:
    if ctx:
        ctx.info(f"Setting {property_name} on {path}")
    data = await bridge.set_property(
        path.strip(),
        property_name.strip(),
        property_value,
        timeout=None if timeout_seconds <= 0 else timeout_seconds
    )
    if ctx:
        ctx.info(f"Successfully set {property_name} to {property_value}")
    return data


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Roblox MCP bridge server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default=os.getenv("MCP_TRANSPORT", "streamable-http"),
        help="Transport protocol for MCP (default: streamable-http)",
    )
    parser.add_argument(
        "--mount-path",
        default=os.getenv("MCP_MOUNT_PATH"),
        help="Mount path when running in SSE mode",
    )
    return parser.parse_args()


def _run_bridge_in_thread() -> None:
    """Run the websocket bridge in a dedicated thread with its own event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(bridge.serve_forever())
    finally:
        loop.close()


def main() -> None:
    args = _parse_args()
    logging.info("Starting MCP server using %s transport", args.transport)
    logging.info("Roblox websocket bridge on ws://%s:%s", BRIDGE_HOST, BRIDGE_PORT)
    logging.info("MCP HTTP endpoint on http://%s:%s", HTTP_HOST, HTTP_PORT)

    # Start the websocket bridge in a background thread
    bridge_thread = threading.Thread(target=_run_bridge_in_thread, daemon=True)
    bridge_thread.start()

    # Wait for the bridge to be ready (thread-safe blocking call)
    bridge._ready.wait()

    # Run the MCP server (blocks until shutdown)
    try:
        mcp_app.run(transport=args.transport, mount_path=args.mount_path)
    finally:
        # Trigger bridge shutdown
        asyncio.run(bridge.shutdown())


if __name__ == "__main__":
    # Gracefully exit on Ctrl+C.
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, signal.SIG_DFL)
    main()
