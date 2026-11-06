"""Compatibility and diagnostics for original OpenGameEval checks.

Only lifecycle state, execution context and diagnostic output are adapted.
Assertion conditions and model-visible task prompts are kept unchanged.
"""

import re
import struct

import lz4.block


LOG_SNAPSHOT = r"""(function()
    local ok, entries = pcall(function() return game:GetService("LogService"):GetLogHistory() end)
    if not ok then return {{message = tostring(entries), type = "capture_error"}} end
    local output = {}
    for i = math.max(1, #entries - 99), #entries do
        local entry = entries[i]
        table.insert(output, {message = string.sub(entry.message, 1, 4000),
                              type = tostring(entry.messageType), timestamp = entry.timestamp})
    end
    return output
end)()"""


STATE_HELPERS = r"""
local __smolRun = game:GetService("RunService")
local __smolHttp = game:GetService("HttpService")
local function __smolRequire(value, message)
    if not value then error("Evaluator compatibility: " .. message) end
    return value
end
local function __smolStateFolder()
    local storage = game:GetService("ReplicatedStorage")
    return storage:FindFirstChild("_SmolEvalState")
end
local function __smolEncode(value)
    if typeof(value) == "Instance" then
        local id = value:GetAttribute("_SmolEvalIdentity")
        if not id then
            id = __smolHttp:GenerateGUID(false)
            value:SetAttribute("_SmolEvalIdentity", id)
        end
        return {kind = "Instance", id = id, path = value:GetFullName()}
    elseif type(value) == "table" then
        local entries = {}
        for k, v in pairs(value) do
            table.insert(entries, {__smolEncode(k), __smolEncode(v)})
        end
        return {kind = "Table", entries = entries}
    elseif value == nil then
        return {kind = "Nil"}
    elseif type(value) == "number" or type(value) == "boolean" or type(value) == "string" then
        return {kind = "Scalar", value = value}
    end
    error("Unsupported evaluation state type: " .. typeof(value))
end
local function __smolSave(name, value)
    local folder = __smolStateFolder()
    if not folder then
        folder = Instance.new("Folder")
        folder.Name = "_SmolEvalState"
        folder.Parent = game:GetService("ReplicatedStorage")
    end
    local binding = Instance.new("Folder")
    binding.Name = name
    binding.Parent = folder
    local payload = __smolHttp:JSONEncode(__smolEncode(value))
    for offset = 1, #payload, 50000 do
        local chunk = Instance.new("StringValue")
        chunk.Name = tostring(math.floor((offset - 1) / 50000) + 1)
        chunk.Value = string.sub(payload, offset, offset + 49999)
        chunk.Parent = binding
    end
end
local function __smolRestore(name)
    local folder = __smolStateFolder()
    __smolRequire(folder, "Missing pre-agent evaluation state")
    local binding = folder:FindFirstChild(name)
    __smolRequire(binding, "Missing evaluation state binding: " .. name)
    local chunks = binding:GetChildren()
    table.sort(chunks, function(a, b) return tonumber(a.Name) < tonumber(b.Name) end)
    local parts = {}
    for _, chunk in ipairs(chunks) do table.insert(parts, chunk.Value) end
    local instances = {}
    for _, instance in ipairs(game:GetDescendants()) do
        local id = instance:GetAttribute("_SmolEvalIdentity")
        if id then
            instances[id] = instances[id] or {}
            table.insert(instances[id], instance)
        end
    end
    local function decode(node)
        if node.kind == "Instance" then
            local candidates = instances[node.id] or {}
            for _, instance in ipairs(candidates) do
                if instance:GetFullName() == node.path then return instance end
            end
            __smolRequire(#candidates <= 1, "Ambiguous cloned evaluation identity: " .. node.path)
            return candidates[1]
        end
        if node.kind == "Nil" then return nil end
        if node.kind == "Scalar" then return node.value end
        local result = {}
        for _, entry in ipairs(node.entries) do
            local key, value = decode(entry[1]), decode(entry[2])
            if key ~= nil and value ~= nil then result[key] = value end
        end
        -- Removed/unreplicated objects must not leave holes in instance arrays.
        if #node.entries > 0 and node.entries[1][1].kind == "Scalar"
            and type(node.entries[1][1].value) == "number" then
            local keys, dense = {}, {}
            for k in pairs(result) do table.insert(keys, k) end
            table.sort(keys)
            for _, k in ipairs(keys) do table.insert(dense, result[k]) end
            -- Edit-mode snapshots predate engine-created player characters.
            -- They are baseline objects, not new NPCs/scripts authored by the agent.
            if node.entries[1][2].kind == "Instance" then
                local seen = {}
                for _, item in ipairs(dense) do seen[item] = true end
                for _, player in ipairs(game:GetService("Players"):GetPlayers()) do
                    local character = player.Character
                    if character then
                        if not seen[character] then table.insert(dense, character) end
                        for _, item in ipairs(character:GetDescendants()) do
                            if not seen[item] then table.insert(dense, item) end
                        end
                    end
                end
            end
            return dense
        end
        return result
    end
    return decode(__smolHttp:JSONDecode(table.concat(parts)))
end
local function __smolReloadCharacter(player)
    if __smolRun:IsClient() then
        local previous = player.Character
        game:GetService("ReplicatedStorage"):WaitForChild("_SmolEvalReloadCharacter", 10):InvokeServer()
        if player.Character == previous or not player.Character then player.CharacterAdded:Wait() end
        return true
    end
    player:LoadCharacter()
end
local function __smolReadSource(object)
    if not __smolRun:IsRunning() then return object.Source end
    local cache = game:GetService("ReplicatedStorage"):WaitForChild("_SmolEvalSources", 10)
    local id = object:GetAttribute("_SmolEvalSourceIdentity")
    local saved = id and cache:FindFirstChild(id)
    __smolRequire(saved, "Missing captured script source: " .. object:GetFullName())
    return saved.Value
end
"""

RELOAD_SERVER = r"""
local remote = game:GetService("ReplicatedStorage"):WaitForChild("_SmolEvalReloadCharacter")
remote.OnServerInvoke = function(player)
    player:LoadCharacter()
    local character = player.Character or player.CharacterAdded:Wait()
    character:WaitForChild("HumanoidRootPart", 10)
    return true
end
"""


class RuntimeSource:
    @staticmethod
    def mask_literals(source: str) -> str:
        pattern = r"""--\[(?P<ceq>=*)\[.*?\](?P=ceq)\]|--[^\n]*|\[(?P<eq>=*)\[.*?\](?P=eq)\]|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' """.strip()
        return re.sub(pattern, lambda match: re.sub(r"[^\n]", " ", match[0]), source, flags=re.DOTALL)

    @classmethod
    def checker_region(cls, source: str) -> tuple[int, int] | None:
        masked = cls.mask_literals(source)
        start = re.search(r"^eval\.check_game\s*=\s*function\b", masked, re.MULTILINE)
        if not start:
            return None
        first_line = masked[start.end() :].split("\n", 1)[0]
        inline_end = re.search(r"\bend\s*$", first_line)
        if inline_end:
            return start.end(), start.end() + inline_end.end()
        end = re.search(r"^end\b", masked[start.end() :], re.MULTILINE)
        if not end:
            raise ValueError("Cannot identify legacy check_game boundary")
        return start.end(), start.end() + end.end()

    @classmethod
    def replace_code(cls, source: str, pattern: str, replacement: str) -> tuple[str, int]:
        matches = list(re.finditer(pattern, cls.mask_literals(source)))
        for match in reversed(matches):
            source = source[: match.start()] + match.expand(replacement) + source[match.end() :]
        return source, len(matches)

    @classmethod
    def bindings(cls, source: str) -> list[str]:
        """Select legacy snapshot locals used by a play-mode checker."""
        region = cls.checker_region(source)
        if not region:
            return []
        checker = cls.mask_literals(source[region[0] : region[1]])
        declarations = re.findall(r"^local\s+(\w+)\s*(?:=|$)", cls.mask_literals(source), re.MULTILINE)
        return [
            name
            for name in declarations
            if re.match(r"(?i)(original|old|pre|global)", name) and re.search(rf"\b{re.escape(name)}\b", checker)
        ]

    @classmethod
    def needs_client(cls, source: str) -> bool:
        region = cls.checker_region(source)
        if not region:
            return False
        checker = cls.mask_literals(source[region[0] : region[1]])
        return bool(re.search(r"\bLocalPlayer\b|:FireServer\(|:InvokeServer\(", checker))

    @classmethod
    def adapt(cls, original: str) -> tuple[str, dict]:
        bindings = cls.bindings(original)
        client = cls.needs_client(original)
        source = original
        reloads = 0
        source_reads = 0
        region = cls.checker_region(source)
        if region:
            start, end = region
            checker = source[start:end]
            checker, source_reads = cls.replace_code(checker, r"\b(\w+)\.Source\b", r"__smolReadSource(\1)")
            if client:
                checker, reloads = cls.replace_code(
                    checker, r"\b(\w+):LoadCharacter\(\)", r"__smolReloadCharacter(\1)"
                )
            source = source[:start] + checker + source[end:]
        footer = []
        if bindings:
            footer.append("if __smolRun:IsRunning() then")
            footer.extend(f'{name} = __smolRestore("{name}")' for name in bindings)
            footer.append("else\nlocal __smolSetup = eval.setup\neval.setup = function(...)")
            footer.append("if __smolSetup then __smolSetup(...) end")
            footer.extend(f'__smolSave("{name}", {name})' for name in bindings)
            footer.append("end\nend")
        if client:
            footer.extend(
                [
                    'assert(eval.runConfig == nil, "Legacy client routing conflicts with runConfig")',
                    "eval.runConfig = {clientChecks = {eval.check_game}}",
                    "eval.check_game = nil",
                ]
            )
        if footer or reloads or source_reads:
            head, separator, tail = source.rpartition("return eval")
            if not separator or tail.strip():
                raise ValueError("Unsupported eval module return; refusing to alter checks")
            source = STATE_HELPERS + head + "\n" + "\n".join(footer) + "\nreturn eval" + tail
        return source, {
            "check_game_context": "client" if client else "original",
            "state_bindings": bindings,
            "server_reload_calls": reloads,
            "source_read_calls": source_reads,
        }


def instrument_source(source: str) -> str:
    """Record diagnostics in original checker outputs without changing verdicts."""
    if "local function mockAssert" in source and "utils.runChecks" in source:
        source = source.replace('utils.warning = ""', 'utils.warning = ""\nutils.failures = {}', 1)
        source = source.replace(
            "utils.fails += 1",
            "utils.fails += 1\n\t\ttable.insert(utils.failures, {check = utils.checks, message = tostring(message)})",
        )
    if "local function generateServerScript" in source:
        source = source.replace('utils_checks.warning = ""', 'utils_checks.warning = ""\nutils_checks.failures = {}')
        source = source.replace(
            "fails = utils_checks.fails,",
            "fails = utils_checks.fails,\n failures = utils_checks.failures,"
            "\n interruptions = utils_checks.interruptions,"
            f"\n runtime_logs = {LOG_SNAPSHOT},",
        )
    return source


def patch_plugin(data: bytes, bridge: str) -> bytes:
    chunks, position = [data[:32]], 32
    while position < len(data):
        tag, compressed, size, reserved = struct.unpack_from("<4sIII", data, position)
        position += 16
        raw = data[position : position + (compressed or size)]
        position += len(raw)
        decoded = lz4.block.decompress(raw, uncompressed_size=size) if compressed else raw
        if tag == b"PROP":
            name_size = struct.unpack_from("<I", decoded, 4)[0]
            if decoded[8 : 8 + name_size] == b"Source":
                offset = 9 + name_size
                prefix, sources = decoded[:offset], []
                while offset < len(decoded):
                    length = struct.unpack_from("<I", decoded, offset)[0]
                    offset += 4
                    source = decoded[offset : offset + length].decode("utf-8")
                    offset += length
                    source = instrument_source(source)
                    if struct.unpack_from("<I", decoded)[0] == 0:
                        source += bridge
                    encoded = source.encode("utf-8")
                    sources.append(struct.pack("<I", len(encoded)) + encoded)
                decoded = prefix + b"".join(sources)
                raw = lz4.block.compress(decoded, store_size=False)
                compressed, size = len(raw), len(decoded)
        chunks.append(struct.pack("<4sIII", tag, compressed, size, reserved) + raw)
    return b"".join(chunks)
