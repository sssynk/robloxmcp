# Roblox MCP Bridge

A bridge server that connects AI assistants to Roblox Studio through the Model Context Protocol (MCP). This allows AI tools like Claude or other LLM-powered coding assistants to read and modify your Roblox game data in real-time.

## What Does This Do?

This project creates a two-way communication channel between your AI assistant and Roblox Studio. Once connected, your AI can:

- Browse your game's object hierarchy
- Read and modify script source code
- Search for specific scripts or objects
- Create new instances (parts, folders, scripts, etc.)
- Delete objects and modify their properties
- Navigate through services like Workspace, ReplicatedStorage, and ServerScriptService

Think of it as giving your AI assistant a direct line into Roblox Studio, so it can help you debug, refactor, or build features without you having to manually copy-paste code back and forth.

## Architecture

The bridge consists of two main components:

**Python Server** (`roblox_mcp_server.py`)
- Runs a websocket server that the Roblox plugin connects to
- Exposes MCP tools that AI assistants can call
- Handles request routing and timeout management
- Supports multiple transport protocols (stdio, SSE, streamable-http)

**Roblox Plugin** (`RobloxBridgePlugin.lua`)
- Runs inside Roblox Studio as a plugin
- Connects to the Python server via websocket
- Executes commands against the live data model
- Automatically reconnects if the connection drops

## Setup

### Requirements

- Python 3.10 or newer
- Roblox Studio
- The following Python packages:
  - `mcp` (FastMCP)
  - `websockets`

### Installation

1. Install the Python dependencies:

```bash
pip install mcp websockets
```

2. Install the Roblox plugin:
   - Open Roblox Studio
   - Open the Plugins folder (File > Open > Plugins Folder, or `%LOCALAPPDATA%\Roblox\Plugins` on Windows)
   - Copy `RobloxBridgePlugin.lua` into the Plugins folder
   - Restart Roblox Studio if needed

3. Start the bridge server:

```bash
python roblox_mcp_server.py
```

The server will start two listeners:
- Websocket bridge on `ws://127.0.0.1:9090` (for Roblox Studio)
- MCP HTTP endpoint on `http://127.0.0.1:8000` (for AI assistants)

4. The Roblox plugin should automatically connect when Studio starts. Look for connection messages in the Output window.

## Configuration

You can customize the bridge behavior using environment variables:

```bash
# Websocket settings
export ROBLOX_BRIDGE_HOST=127.0.0.1
export ROBLOX_BRIDGE_PORT=9090
export ROBLOX_BRIDGE_TIMEOUT=12

# MCP server settings
export MCP_HTTP_HOST=127.0.0.1
export MCP_HTTP_PORT=8000
export MCP_TRANSPORT=streamable-http

# Logging
export ROBLOX_BRIDGE_LOG=DEBUG
```

You can also pass the transport mode as a command-line argument:

```bash
python roblox_mcp_server.py --transport stdio
python roblox_mcp_server.py --transport sse --mount-path /mcp
```

## Available Operations

### Read Operations

- **wait_for_roblox**: Wait until the Roblox plugin is connected
- **list_children**: Get immediate children of any object in the game hierarchy
- **read_script**: Fetch source code from Scripts, LocalScripts, or ModuleScripts
- **search_for_string**: Search all scripts in your game for a specific string
- **search_for_object**: Find objects by name (fuzzy search, case-insensitive)

### Write Operations

- **write_script**: Overwrite the source code of a script
- **create_instance**: Create new objects (Parts, Folders, Scripts, etc.)
- **delete_instance**: Permanently delete an object and all its children
- **set_property**: Modify properties like Name, Position, Color, etc.

All write operations take effect immediately in Roblox Studio, and you can undo them with Ctrl+Z.

## Path Format

Objects in Roblox are referenced using slash-separated paths:

```
game/Workspace/Part
game/ReplicatedStorage/RemoteEvents/PlayerJoined
game/ServerScriptService/GameManager
```

The plugin automatically resolves services, so `game/Workspace` will correctly find the Workspace service even though it's technically a child of `game`.

## Usage Example

Once everything is running, your AI assistant can interact with your Roblox game. For example:

"Find all scripts that reference 'PlayerAdded' and show me the code"

The AI will:
1. Call `search_for_string` with "PlayerAdded"
2. Call `read_script` for each result
3. Show you the relevant code

Or: "Create a new ModuleScript called 'Config' in ReplicatedStorage"

The AI will:
1. Call `create_instance` with className="ModuleScript", parentPath="game/ReplicatedStorage", name="Config"
2. Confirm the instance was created

## Troubleshooting

**Plugin not connecting:**
- Make sure the Python server is running first
- Check that port 9090 isn't blocked by a firewall
- Look for error messages in Roblox Studio's Output window
- Try clicking the "ReconnectMCPBridge" button in the Plugins toolbar

**Timeout errors:**
- Increase the timeout with `ROBLOX_BRIDGE_TIMEOUT=30`
- Large search operations may take longer than the default 12 seconds

**Permission errors:**
- Make sure Roblox Studio has permission to access network resources
- Some corporate networks may block websocket connections

**Write operations not working:**
- Check that you're not in Play mode (write operations only work in Edit mode)
- Verify the path exists and is correct

## Security Notes

This bridge runs locally on your machine and only accepts connections from localhost by default. The Roblox plugin can only interact with the currently open Studio session.

If you need to expose the bridge over a network, you can change the host settings, but be aware that this gives anyone who can reach that port full read/write access to your Roblox game.

## Extending the Bridge

The bridge is designed to be extensible. To add new operations:

1. Add a new command handler in `RobloxBridgePlugin.lua` to the `CommandHandlers` table
2. Add a corresponding method to the `RobloxBridge` class in `roblox_mcp_server.py`
3. Expose it as an MCP tool using the `@mcp_app.tool()` decorator

The plugin supports type conversion for common Roblox data types like Vector3, Color3, and UDim2, so you can pass them as JSON objects from the Python side.

## License

This project is provided as-is for educational and development purposes.

