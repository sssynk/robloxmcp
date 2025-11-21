--!strict
--[[
    Roblox <> MCP bridge plugin
    Drop this script into a Roblox Studio plugin (e.g. a single Script inside a toolbar button).
    It maintains a websocket connection to the Python MCP server so that tools can query the
    live data model (services, instances, and script sources) without granting write access yet.
--]]

local HttpService = game:GetService("HttpService")
local RunService = game:GetService("RunService")

local DEFAULT_URL = "ws://127.0.0.1:9090"
local BRIDGE_URL = (plugin and plugin:GetSetting("RobloxMCPBridgeUrl")) or DEFAULT_URL
local RECONNECT_DELAY = 2

local wsClient: WebStreamClient?
local reconnectScheduled = false
local closing = false

local function log(message: string)
    print("[MCP Bridge] " .. message)
end

local function warnf(message: string)
    warn("[MCP Bridge] " .. message)
end

local function encode(payload: any): string?
    local ok, result = pcall(HttpService.JSONEncode, HttpService, payload)
    if ok then
        return result
    end
    warnf("Failed to encode payload: " .. tostring(result))
    return nil
end

local function send(payload: any)
    if not wsClient then
        return
    end
    local encoded = encode(payload)
    if not encoded then
        return
    end
    local ok, err = pcall(function()
        wsClient:Send(encoded)
    end)
    if not ok then
        warnf("Send failed: " .. tostring(err))
        scheduleReconnect()
    end
end

local function sendEvent(eventName: string, data: any?)
    send({
        type = "event",
        event = eventName,
        data = data,
    })
end

local function sendResponse(requestId: string?, success: boolean, data: any?, errorMessage: string?)
    if not requestId then
        return
    end
    send({
        type = "response",
        requestId = requestId,
        success = success,
        data = data,
        error = errorMessage,
    })
end

local function splitPath(rawPath: string?): {string}
    local segments = {}
    if not rawPath or rawPath == "" then
        return segments
    end
    for segment in string.gmatch(rawPath, "[^/]+") do
        if segment ~= "" then
            table.insert(segments, segment)
        end
    end
    return segments
end

local function resolvePath(rawPath: string?): Instance?
    if not rawPath or rawPath == "" then
        return game
    end
    local segments = splitPath(rawPath)
    local current: Instance = game
    for _, segment in ipairs(segments) do
        if segment == "game" or segment == "Game" then
            current = game
        elseif current == game then
            local ok, service = pcall(function()
                return game:GetService(segment)
            end)
            if ok and service then
                current = service
            else
                local found = current:FindFirstChild(segment)
                if not found then
                    return nil
                end
                current = found
            end
        else
            local child = current:FindFirstChild(segment)
            if not child then
                return nil
            end
            current = child
        end
    end
    return current
end

local function buildChildPath(parentPath: string?, childName: string): string
    if not parentPath or parentPath == "" then
        return childName
    end
    return parentPath .. "/" .. childName
end

local function serializeInstance(instance: Instance, parentPath: string?): {[string]: any}
    local children = instance:GetChildren()
    return {
        name = instance.Name,
        className = instance.ClassName,
        path = buildChildPath(parentPath, instance.Name),
        isService = instance:IsA("Service"),
        hasChildren = #children > 0,
        isScript = instance:IsA("LuaSourceContainer"),
    }
end

local function convertPropertyValue(value: any): any
    -- If it's a string, try to parse it as JSON first
    if type(value) == "string" then
        local ok, parsed = pcall(HttpService.JSONDecode, HttpService, value)
        if ok and type(parsed) == "table" then
            value = parsed
        else
            -- Not JSON or failed to parse, return the string as-is
            return value
        end
    end

    -- If it's not a table, return as-is
    if type(value) ~= "table" then
        return value
    end

    -- Try to detect and convert common Roblox data types

    -- Vector2
    if value.X ~= nil and value.Y ~= nil and value.Z == nil then
        return Vector2.new(tonumber(value.X) or 0, tonumber(value.Y) or 0)
    end

    -- Vector3
    if value.X ~= nil and value.Y ~= nil and value.Z ~= nil then
        return Vector3.new(tonumber(value.X) or 0, tonumber(value.Y) or 0, tonumber(value.Z) or 0)
    end

    -- Color3 (R, G, B)
    if value.R ~= nil and value.G ~= nil and value.B ~= nil then
        return Color3.new(tonumber(value.R) or 0, tonumber(value.G) or 0, tonumber(value.B) or 0)
    end

    -- UDim2 (xScale, xOffset, yScale, yOffset)
    if value.xScale ~= nil or value.xOffset ~= nil or value.yScale ~= nil or value.yOffset ~= nil then
        return UDim2.new(
            tonumber(value.xScale) or 0,
            tonumber(value.xOffset) or 0,
            tonumber(value.yScale) or 0,
            tonumber(value.yOffset) or 0
        )
    end

    -- UDim (Scale, Offset)
    if value.Scale ~= nil and value.Offset ~= nil then
        return UDim.new(tonumber(value.Scale) or 0, tonumber(value.Offset) or 0)
    end

    -- CFrame (Position only for simplicity)
    if value.Position ~= nil and type(value.Position) == "table" then
        local pos = convertPropertyValue(value.Position)
        return CFrame.new(pos)
    end

    -- Return the table as-is if we can't convert it
    return value
end

local CommandHandlers: {[string]: (payload: {[string]: any}) -> ()} = {}

function CommandHandlers.GET_CHILDREN(payload)
    local requestId = payload.requestId
    local targetPath = payload.path
    local instance = resolvePath(targetPath)
    if not instance then
        sendResponse(requestId, false, nil, "Path not found: " .. tostring(targetPath))
        return
    end

    local serializedChildren = {}
    for _, child in ipairs(instance:GetChildren()) do
        table.insert(serializedChildren, serializeInstance(child, instance == game and "game" or targetPath))
    end

    sendResponse(requestId, true, {
        path = targetPath or "game",
        children = serializedChildren,
    })
end

function CommandHandlers.READ_SCRIPT(payload)
    local requestId = payload.requestId
    local targetPath = payload.path
    local instance = resolvePath(targetPath)
    if not instance then
        sendResponse(requestId, false, nil, "Path not found: " .. tostring(targetPath))
        return
    end

    if not instance:IsA("LuaSourceContainer") then
        sendResponse(requestId, false, nil, "Instance is not a Script/ModuleScript/LocalScript")
        return
    end

    local ok, source = pcall(function()
        return instance.Source
    end)
    if not ok then
        sendResponse(requestId, false, nil, "Unable to read script source")
        return
    end

    sendResponse(requestId, true, {
        path = targetPath,
        name = instance.Name,
        className = instance.ClassName,
        source = source,
    })
end

function CommandHandlers.PING(payload)
    sendResponse(payload.requestId, true, {
        msg = "pong",
        clientTime = os.clock(),
    })
end

function CommandHandlers.SEARCH_SCRIPTS(payload)
    local requestId = payload.requestId
    local searchString = payload.searchString

    if not searchString or searchString == "" then
        sendResponse(requestId, false, nil, "searchString is required")
        return
    end

    local results = {}
    local function searchInInstance(instance, parentPath)
        if instance:IsA("LuaSourceContainer") then
            local ok, source = pcall(function()
                return instance.Source
            end)
            if ok and source and string.find(source:lower(), searchString:lower()) then
                table.insert(results, {
                    path = buildChildPath(parentPath, instance.Name),
                    name = instance.Name,
                    className = instance.ClassName,
                })
            end
        end

        for _, child in ipairs(instance:GetChildren()) do
            searchInInstance(child, instance == game and "game" or buildChildPath(parentPath, instance.Name))
        end
    end

    searchInInstance(game, "game")

    sendResponse(requestId, true, {
        searchString = searchString,
        results = results,
        count = #results,
    })
end

function CommandHandlers.SEARCH_OBJECTS(payload)
    local requestId = payload.requestId
    local searchString = payload.searchString
    local searchRoot = payload.searchRoot or "game/Workspace"

    if not searchString or searchString == "" then
        sendResponse(requestId, false, nil, "searchString is required")
        return
    end

    local rootInstance = resolvePath(searchRoot)
    if not rootInstance then
        sendResponse(requestId, false, nil, "Search root path not found: " .. tostring(searchRoot))
        return
    end

    local results = {}
    local lowerSearch = searchString:lower()

    local function searchInInstance(instance, parentPath)
        if string.find(instance.Name:lower(), lowerSearch) then
            table.insert(results, serializeInstance(instance, parentPath))
        end

        for _, child in ipairs(instance:GetChildren()) do
            searchInInstance(child, instance == game and "game" or buildChildPath(parentPath, instance.Name))
        end
    end

    searchInInstance(rootInstance, searchRoot == "game" and "" or searchRoot)

    sendResponse(requestId, true, {
        searchString = searchString,
        searchRoot = searchRoot,
        results = results,
        count = #results,
    })
end

function CommandHandlers.WRITE_SCRIPT(payload)
    local requestId = payload.requestId
    local targetPath = payload.path
    local newSource = payload.source

    if not newSource then
        sendResponse(requestId, false, nil, "source is required")
        return
    end

    local instance = resolvePath(targetPath)
    if not instance then
        sendResponse(requestId, false, nil, "Path not found: " .. tostring(targetPath))
        return
    end

    if not instance:IsA("LuaSourceContainer") then
        sendResponse(requestId, false, nil, "Instance is not a Script/ModuleScript/LocalScript")
        return
    end

    local ok, err = pcall(function()
        instance.Source = newSource
    end)

    if not ok then
        sendResponse(requestId, false, nil, "Failed to write script: " .. tostring(err))
        return
    end

    sendResponse(requestId, true, {
        path = targetPath,
        name = instance.Name,
        bytesWritten = #newSource,
    })
end

function CommandHandlers.CREATE_INSTANCE(payload)
    local requestId = payload.requestId
    local className = payload.className
    local parentPath = payload.parentPath
    local instanceName = payload.name
    local properties = payload.properties or {}

    if not className or className == "" then
        sendResponse(requestId, false, nil, "className is required")
        return
    end

    local parent = resolvePath(parentPath)
    if not parent then
        sendResponse(requestId, false, nil, "Parent path not found: " .. tostring(parentPath))
        return
    end

    local newInstance
    local ok, err = pcall(function()
        newInstance = Instance.new(className)
        if instanceName and instanceName ~= "" then
            newInstance.Name = instanceName
        end

        -- Set properties with type conversion
        for propName, propValue in pairs(properties) do
            if propName ~= "Name" and propName ~= "Parent" then
                newInstance[propName] = convertPropertyValue(propValue)
            end
        end

        newInstance.Parent = parent
    end)

    if not ok then
        sendResponse(requestId, false, nil, "Failed to create instance: " .. tostring(err))
        return
    end

    sendResponse(requestId, true, {
        path = buildChildPath(parentPath, newInstance.Name),
        name = newInstance.Name,
        className = newInstance.ClassName,
    })
end

function CommandHandlers.DELETE_INSTANCE(payload)
    local requestId = payload.requestId
    local targetPath = payload.path

    local instance = resolvePath(targetPath)
    if not instance then
        sendResponse(requestId, false, nil, "Path not found: " .. tostring(targetPath))
        return
    end

    local instanceName = instance.Name
    local instanceClass = instance.ClassName

    local ok, err = pcall(function()
        instance:Destroy()
    end)

    if not ok then
        sendResponse(requestId, false, nil, "Failed to delete instance: " .. tostring(err))
        return
    end

    sendResponse(requestId, true, {
        path = targetPath,
        name = instanceName,
        className = instanceClass,
        deleted = true,
    })
end

function CommandHandlers.SET_PROPERTY(payload)
    local requestId = payload.requestId
    local targetPath = payload.path
    local propertyName = payload.propertyName
    local propertyValue = payload.propertyValue

    if not propertyName or propertyName == "" then
        sendResponse(requestId, false, nil, "propertyName is required")
        return
    end

    local instance = resolvePath(targetPath)
    if not instance then
        sendResponse(requestId, false, nil, "Path not found: " .. tostring(targetPath))
        return
    end

    -- Convert the property value to the appropriate Roblox type
    local convertedValue = convertPropertyValue(propertyValue)

    local ok, err = pcall(function()
        instance[propertyName] = convertedValue
    end)

    if not ok then
        sendResponse(requestId, false, nil, "Failed to set property: " .. tostring(err))
        return
    end

    sendResponse(requestId, true, {
        path = targetPath,
        propertyName = propertyName,
        propertyValue = tostring(convertedValue),
    })
end

local function handleMessage(message: string)
    local ok, payload = pcall(HttpService.JSONDecode, HttpService, message)
    if not ok then
        warnf("Malformed JSON from server")
        return
    end

    local messageType = payload.type
    local handler = CommandHandlers[messageType]
    if handler then
        handler(payload)
    else
        warnf("No handler for message type " .. tostring(messageType))
    end
end

local function scheduleReconnect()
    if reconnectScheduled or closing then
        return
    end
    reconnectScheduled = true
    task.delay(RECONNECT_DELAY, function()
        reconnectScheduled = false
        if wsClient then
            pcall(function()
                wsClient:Close()
            end)
        end
        wsClient = nil
        connect()
    end)
end

local function connect(force: boolean?)
    if wsClient and not force then
        return
    end

    local success, clientOrErr = pcall(function()
        return HttpService:CreateWebStreamClient(Enum.WebStreamClientType.WebSocket, {
            Url = BRIDGE_URL,
        })
    end)

    if not success then
        warnf("Failed to create WebStreamClient: " .. tostring(clientOrErr))
        scheduleReconnect()
        return
    end

    wsClient = clientOrErr
    log("Connected to MCP server at " .. BRIDGE_URL)

    wsClient.MessageReceived:Connect(handleMessage)

    send({
        type = "hello",
        version = plugin and plugin.Name or "roblox-plugin",
        placeId = game.PlaceId,
        placeName = game.Name,
        isPlaySolo = RunService:IsRunning(),
    })
end

if plugin then
    local toolbar = plugin:CreateToolbar("MCP Bridge")
    local button = toolbar:CreateButton("ReconnectMCPBridge", "Reconnect MCP bridge", "", "Reconnect Bridge")
    button.Click:Connect(function()
        log("Manual reconnect triggered")
        connect(true)
    end)
    plugin.Unloading:Connect(function()
        closing = true
        if wsClient then
            pcall(function()
                wsClient:Close()
            end)
        end
    end)
end

connect()
