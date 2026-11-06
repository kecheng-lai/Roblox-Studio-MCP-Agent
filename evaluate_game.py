"""OpenGameEval 本地评测：每题 5 次，Agent 300 秒，默认串行。

首次准备：pip install -e ".[mcp,openai]" lz4，在 Studio 登录并启用内置 MCP。
脚本自动安装缺少的 Eval Runner 插件、复制原始场景、启动 Studio 并等待连接。
每轮使用独立场景副本，不需要手动打开或恢复 racing.rbxl。
同一时间只运行一个实验，结束后自动关闭本轮 Studio，结果保存在 local_results。
启动/连接和评分各有独立的 180 秒预算；并行入口为 evaluate_game_parallel.py。

模型固定为 deepseek-chat，设置 DEEPSEEK_API_KEY 后运行：python evaluate_game.py
离线单元测试：python evaluate_game.py --self-test
执行 Evals 和 DebugEvals，自动排除多客户端题；原题保持不变，由本地适配层选择运行上下文。
中断后可用 python evaluate_game.py --resume 批次目录 接着运行，已完成项不重跑。
续跑要求脚本、题目和插件版本一致；旧协议批次需要重新运行，避免混合不同评分规则。
"""

import hashlib
import importlib.metadata
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from mcp import StdioServerParameters
from mcpadapt.core import MCPAdapt
from mcpadapt.smolagents_adapter import SmolAgentsAdapter

from game_eval_runtime import LOG_SNAPSHOT, RELOAD_SERVER, RuntimeSource, patch_plugin
from smolagents import OpenAIModel, ToolCallingAgent
from smolagents.tools import Tool


# Baseline：DeepSeek Chat，密钥读取 DEEPSEEK_API_KEY，30 步，工具串行执行。
ROOT = Path(__file__).resolve().parent
TASK = "001_make_cars_faster"
MAX_STEPS = 30
RUNS = 5
TIMEOUT = 300
PROTOCOL_VERSION = 4
STARTUP_TIMEOUT = 180
GRADE_TIMEOUT = 180
TOOL_NAMES = (
    "search_game_tree",
    "inspect_instance",
    "script_search",
    "script_read",
    "script_grep",
    "multi_edit",
    "execute_luau",
    "get_console_output",
)
MARKER = "__SMOL_GAME_EVAL__"

BRIDGE_SOURCE = r"""

-- Python 本地评测适配：复用原 utils_checks 和 PlayModeRunner。
-- 数据请求由插件自己的线程处理，MCP 不调用受限的插件函数。
local batchToken = "__BATCH_TOKEN__"
local activeEval, activeSource, activeName, activeStorage, activeModule
local batchChecks = require(loadedCode.EvalUtils.utils_checks)
local function newStorage()
    local data, list = {}, {}
    return {
        getAll = function() return data end,
        get = function(key) return data[key] end,
        set = function(key, value) data[key] = value end,
        setList = function(value) list = value end,
        getList = function() return list end,
    }
end
local function checkResult(fn, label, actions)
    batchChecks.checks, batchChecks.passes, batchChecks.fails = 0, 0, 0
    batchChecks.interruptions, batchChecks.warning, batchChecks.failures = {}, "", {}
    local ok, err = pcall(batchChecks.runChecks, fn, actions or {}, label, label, activeStorage)
    return {
        checks = batchChecks.checks, passes = batchChecks.passes, fails = batchChecks.fails,
        interruptions = batchChecks.interruptions, warning = batchChecks.warning,
        failures = batchChecks.failures,
        runtime_logs = __LOG_SNAPSHOT__,
        error = if ok then "" else tostring(err),
    }
end
local function batchCommand(request)
    local data = request.data or {}
    if request.operation == "prepare" then
        activeSource, activeName = data.source, data.name
        local module = Instance.new("ModuleScript")
        module.Name, module.Source = activeName, activeSource
        local ok, eval = pcall(require, module)
        module:Destroy()
        assert(ok, tostring(eval))
        activeEval = eval
        local count = if eval.runConfig and eval.runConfig.clientChecks then #eval.runConfig.clientChecks else 0
        if count > 1 then return {excluded = true, clients = count} end
        activeStorage = newStorage()
        activeStorage.set("isStudio", true)
        if data.legacyClient then
            local remote = Instance.new("RemoteFunction")
            remote.Name = "_SmolEvalReloadCharacter"
            remote.Parent = game:GetService("ReplicatedStorage")
            local helper = Instance.new("Script")
            helper.Name = "_SmolEvalReloadCharacterServer"
            helper.Source = data.reloadServer
            helper.Parent = game:GetService("ServerScriptService")
        end
        if eval.setup then eval.setup(activeStorage) end
        eval.reference = nil
        local first = eval.prompt[1]
        local prompt
        if type(first) == "string" then
            prompt = first
        else
            local parts = {}
            for _, message in first do
                if message.role == "user" then table.insert(parts, message.content) end
            end
            prompt = table.concat(parts, "\n")
        end
        assert(type(prompt) == "string" and #prompt > 0, "Missing user prompt")
        return {prompt = prompt, place = eval.place, clients = count,
                hasPlay = PlayModeRunner.hasPlayModeChecks(eval)}
    elseif request.operation == "scene" then
        return checkResult(activeEval.check_scene, "scene", data.actions)
    elseif request.operation == "play" then
        -- Capture Source with plugin permissions before ordinary test scripts run.
        local cache = Instance.new("Folder")
        cache.Name = "_SmolEvalSources"
        cache.Parent = game:GetService("ReplicatedStorage")
        for _, object in ipairs(game:GetDescendants()) do
            if object:IsA("LuaSourceContainer") and not object:IsDescendantOf(loadedCode) then
                local ok, source = pcall(function() return object.Source end)
                if ok then
                    local id = HttpService:GenerateGUID(false)
                    object:SetAttribute("_SmolEvalSourceIdentity", id)
                    local saved = Instance.new("StringValue")
                    saved.Name, saved.Value = id, source
                    saved.Parent = cache
                end
            end
        end
        -- 只在 agent 结束后将检查源码交给原始 PlayModeRunner。
        activeModule = Instance.new("ModuleScript")
        activeModule.Name, activeModule.Source = activeName, activeSource
        activeModule.Parent = ensureEvalsFolder()
        local results = PlayModeRunner.execute({name = activeName, module = activeModule})
        activeModule:Destroy()
        activeModule = nil
        assert(#results > 0, "PlayModeRunner returned no results")
        if activeEval.runConfig and activeEval.runConfig.clientChecks and #activeEval.runConfig.clientChecks == 1 then
            local found = false
            for _, result in results do
                if result.source == "client" then found = true end
            end
            assert(found, "PlayModeRunner did not return the required client result")
        end
        return results
    elseif request.operation == "cleanup" then
        if activeModule then activeModule:Destroy() end
        activeEval, activeSource, activeStorage, activeModule = nil, nil, nil, nil
        return {}
    end
    error("Unknown evaluation operation")
end
local batchStopped = false
plugin.Unloading:Connect(function() batchStopped = true end)
task.spawn(function()
    while not batchStopped do
        local raw = ServerStorage:GetAttribute("_SmolEvalRequest")
        if raw then
            ServerStorage:SetAttribute("_SmolEvalRequest", nil)
            local request = HttpService:JSONDecode(raw)
            if request.token == batchToken then
                local ok, result = pcall(batchCommand, request)
                ServerStorage:SetAttribute("_SmolEvalResponse", HttpService:JSONEncode({
                    id = request.id, ok = ok,
                    result = if ok then result else nil,
                    error = if ok then nil else tostring(result),
                }))
            end
        end
        task.wait(0.1)
    end
end)

"""


@contextmanager
def scoring_plugin(token: str, backup_dir: Path):
    """给现有插件加本地调度接口；原任务检查和 PlayModeRunner 不作修改。"""
    target = Path(os.environ["LOCALAPPDATA"]) / "Roblox" / "Plugins" / "EvalRunner.rbxm"
    original = target.read_bytes() if target.exists() else None
    if original is not None:
        (backup_dir / "plugin-original.rbxm").write_bytes(original)
    data = (ROOT / "open-game-eval" / "EvalPlugin" / "EvalRunner.rbxm").read_bytes()
    patched = patch_plugin(
        data, BRIDGE_SOURCE.replace("__BATCH_TOKEN__", token).replace("__LOG_SNAPSHOT__", LOG_SNAPSHOT)
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(patched)
    try:
        yield
    finally:
        if original is None:
            target.unlink()
        else:
            target.write_bytes(original)


def save_json(path: Path, value) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        payload = payload.replace(json.dumps(key, ensure_ascii=False)[1:-1], "[REDACTED]")
    temporary = path.with_name(f"{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        temporary.write_text(payload + "\n", encoding="utf-8")
        # Windows readers/antivirus may briefly hold the destination without
        # delete sharing. Keep the previous complete JSON until replacement
        # succeeds; never fall back to truncating the live result file.
        for attempt in range(8):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(min(0.05 * 2**attempt, 0.5))
        if path.name == "result.json" and isinstance(value, dict) and value.get("phase"):
            save_json(path.with_name("phase.json"), {"phase": value["phase"], "attempt_id": value.get("attempt_id")})
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except PermissionError:
            pass


def task_place(task: Path) -> Path:
    source = task.read_text(encoding="utf-8-sig")
    name = re.search(r"\bplace\s*=\s*[\"\x27]([^\"\x27]+)", source).group(1)
    return ROOT / "open-game-eval" / "Places" / name


def configure_mcp() -> StdioServerParameters:
    """内置原 mcp.bat 的定位逻辑，直接启动 StudioMCP.exe，不依赖外部启动脚本。"""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Roblox\RobloxStudio") as key:
        content_folder, _ = winreg.QueryValueEx(key, "ContentFolder")
    executable = Path(content_folder).parent / "StudioMCP.exe"
    if not executable.is_file() or not executable.with_name("RobloxStudioBeta.exe").is_file():
        # Studio's updater can remove the old version before refreshing the
        # ContentFolder registry entry. Use only complete installed versions.
        versions = Path(os.environ["LOCALAPPDATA"]) / "Roblox" / "Versions"
        candidates = [
            path
            for path in versions.glob("version-*/StudioMCP.exe")
            if path.with_name("RobloxStudioBeta.exe").is_file()
        ]
        if not candidates:
            raise FileNotFoundError("No complete Roblox Studio installation found after update")
        executable = max(candidates, key=lambda path: path.stat().st_mtime)
    # 继承当前终端环境，包括用户设置的代理。
    return StdioServerParameters(command=str(executable), env=dict(os.environ))


@contextmanager
def single_experiment():
    """Windows 文件锁随进程退出释放，防止重复启动造成实验并行。"""
    import msvcrt

    output_dir = ROOT / "local_results"
    output_dir.mkdir(exist_ok=True)
    with (output_dir / ".experiment.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            print("已有实验正在运行，本次不启动。", flush=True)
            yield False
            return
        try:
            yield True
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def launch_studio(
    server: StdioServerParameters, run_dir: Path, source_place: Path | None = None
) -> tuple[Path, subprocess.Popen]:
    """使用 Windows 文件关联相同的启动方式，打开本轮独立的初始场景。"""
    plugin = Path(os.environ["LOCALAPPDATA"]) / "Roblox" / "Plugins" / "EvalRunner.rbxm"
    if not plugin.exists():
        plugin.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "open-game-eval" / "EvalPlugin" / "EvalRunner.rbxm", plugin)
    run_dir.mkdir(parents=True, exist_ok=True)
    place = run_dir / f"eval-{secrets.token_hex(6)}.rbxl"
    shutil.copy2(source_place or ROOT / "open-game-eval" / "Places" / "racing.rbxl", place)
    executable = Path(server.command).with_name("RobloxStudioBeta.exe")
    env = dict(server.env)
    env["NO_PROXY"] = ",".join(filter(None, [env.get("NO_PROXY"), "localhost", "127.0.0.1", "::1"]))
    process = subprocess.Popen([str(executable), str(place)], env=env)
    print(f"正在启动 Studio：{place.name}", flush=True)
    return place, process


@contextmanager
def studio_session(server: StdioServerParameters, run_dir: Path, source_place: Path | None = None):
    place, process = launch_studio(server, run_dir, source_place)
    try:
        yield place
    finally:
        # 只回收本轮启动的进程；关闭时不保存临时场景，避免累积后台实例。
        terminate_process_tree(process)


def terminate_process_tree(process) -> None:
    """Reap Windows venv launchers, workers, MCP clients and Studio children."""
    if process.poll() is None:
        if os.name == "nt" and isinstance(process.pid, int):
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if process.poll() is None:
                process.kill()
        else:
            process.terminate()
    process.wait(timeout=15)


def lua_string(text: str) -> str:
    equals = ""
    while f"]{equals}]" in text:
        equals += "="
    return f"[{equals}[\n{text}]{equals}]"


def decode_reply(value) -> dict:
    """从 MCP 返回的文本或 JSON 包装中读取 Luau 结果。"""
    if isinstance(value, str):
        try:
            return decode_reply(json.loads(value))
        except (ValueError, TypeError):
            pass
        if MARKER in value:
            return json.JSONDecoder().raw_decode(value.split(MARKER, 1)[1])[0]
    elif isinstance(value, (dict, list)):
        for child in value.values() if isinstance(value, dict) else value:
            try:
                return decode_reply(child)
            except ValueError:
                pass
    raise ValueError(f"MCP 未返回评测结果：{value}")


class StudioTool(Tool):
    """固定 Studio 实例和 Edit 模式，模型只需填写实际工具参数。"""

    skip_forward_signature_validation = True

    def __init__(self, tool: Tool, studio_id: str):
        super().__init__()
        self.target, self.studio_id = tool, studio_id
        self.name, self.description, self.output_type = tool.name, tool.description, tool.output_type
        self.inputs = {k: v for k, v in tool.inputs.items() if k not in {"studio_id", "datamodel_type"}}

    def forward(self, **kwargs):
        kwargs = {key: value for key, value in kwargs.items() if value is not None}
        kwargs["studio_id"] = self.studio_id
        if "datamodel_type" in self.target.inputs:
            kwargs["datamodel_type"] = "Edit"
        return self.target(**kwargs)


class LocalRuntimeAdapter:
    """Route legacy checks without changing their bodies or the tracked sources.

    Explicit routing avoids mistaking LocalPlayer references inside generated
    gameplay scripts for client-side checks. Unsupported legacy checks remain
    checker errors; the adapter never invents assertions or changes a verdict.
    """

    @classmethod
    def adapt(cls, task: Path) -> tuple[str, dict]:
        original_bytes = task.read_bytes()
        original = original_bytes.decode("utf-8-sig")
        source, adaptation = RuntimeSource.adapt(original)
        return source, {
            **adaptation,
            "original_sha256": hashlib.sha256(original_bytes).hexdigest(),
            "runtime_source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        }

    @staticmethod
    def wrap_client_check(original: str) -> str:
        # The original function object is assigned directly to clientChecks.
        # PlayModeRunner already waits for a live character before invoking it.
        return (
            "local function loadOriginalEval()\n" + original + "\nend\n"
            "local eval = loadOriginalEval()\n"
            'assert(type(eval.check_game) == "function", "Missing legacy check_game")\n'
            'assert(eval.runConfig == nil, "Client routing conflicts with original runConfig")\n'
            "eval.runConfig = {clientChecks = {eval.check_game}}\n"
            "eval.check_game = nil\n"
            "return eval\n"
        )


class GameEvaluator:
    def __init__(self, execute: Tool, studio_id: str, token: str):
        self.execute_tool, self.studio_id = execute, studio_id
        self.token = token
        self.metadata = {}

    def execute(self, code: str) -> dict:
        return decode_reply(self.execute_tool(code=code, studio_id=self.studio_id, datamodel_type="Edit"))

    def call(self, operation: str, data: dict | None = None):
        request_id = secrets.token_hex(8)
        payload = json.dumps(
            {"id": request_id, "token": self.token, "operation": operation, "data": data or {}}, ensure_ascii=False
        )
        self.execute(f"""
game:GetService("ServerStorage"):SetAttribute("_SmolEvalResponse", nil)
game:GetService("ServerStorage"):SetAttribute("_SmolEvalRequest", {lua_string(payload)})
return {lua_string(MARKER)} .. '{{"sent":true}}'
""")
        deadline = time.monotonic() + (STARTUP_TIMEOUT if operation == "prepare" else GRADE_TIMEOUT)
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Evaluation bridge timed out during {operation}")
            raw = self.execute_tool(
                code=f"""
local raw = game:GetService("ServerStorage"):GetAttribute("_SmolEvalResponse")
return {lua_string(MARKER)} .. (raw or '{{"pending":true}}')
""",
                studio_id=self.studio_id,
                datamodel_type="Edit",
            )
            # Play 期间 Edit DataModel 不可访问，等待原 PlayModeRunner 自动结束。
            if "Edit datamodel is not available" in str(raw):
                time.sleep(0.5)
                continue
            result = decode_reply(raw)
            if result.get("id") == request_id:
                if not result["ok"]:
                    raise RuntimeError(result["error"])
                return result["result"]
            time.sleep(0.5)

    def prepare(self, task: Path) -> dict:
        source, routing = LocalRuntimeAdapter.adapt(task)
        self.metadata = self.call(
            "prepare",
            {
                "name": task.stem,
                "source": source,
                "legacyClient": routing["check_game_context"] == "client",
                "reloadServer": RELOAD_SERVER,
            },
        )
        self.metadata["runtime_adapter"] = routing
        return self.metadata

    def grade(self, actions: list, on_phase=None) -> dict:
        phases = []

        def record(phase):
            phases.append(phase)
            if on_phase:
                on_phase(self.combine_phases(phases))

        record({"source": "edit", **self.call("scene", {"actions": actions})})
        # A definite edit-mode failure is sufficient; do not run runtime checks
        # against missing or incorrectly typed objects.
        if self.metadata["hasPlay"] and not phases[0].get("fails") and not phases[0].get("error"):
            for phase in self.call("play"):
                record(phase)
        return self.combine_phases(phases)

    @staticmethod
    def combine_phases(phases: list[dict]) -> dict:
        errors = [p["error"] for p in phases if p.get("error")]
        return {
            "checks": sum(p.get("checks", 0) for p in phases),
            "passes": sum(p.get("passes", 0) for p in phases),
            "fails": sum(p.get("fails", 0) for p in phases),
            "error": "\n".join(errors),
            "phases": phases,
        }


def grade_status(grade: dict) -> str:
    if grade.get("fails", 0):
        return "failed"
    if grade.get("error") or grade["checks"] == 0:
        return "checker_error"
    return "passed"


def wait_for_studios(list_studios: Tool, place_name: str | None = None) -> list:
    # Studio 会周期性连接新启动的 MCP 服务，通常需要几秒才能出现在列表里。
    # 冷启动多等一会儿；只选择本轮场景，不误用之前打开的 Studio。
    for _ in range(120 if place_name else 10):
        studios = json.loads(list_studios())["studios"]
        if place_name:
            studios = [studio for studio in studios if Path(studio.get("name") or "").name == place_name]
        if studios:
            return studios
        time.sleep(1)
    return []


def main() -> None:
    with single_experiment() as acquired:
        if acquired:
            run_suite()


class RecordedAdapter(SmolAgentsAdapter):
    """复用 MCPAdapt，保留原始 isError，并恢复 MCP schema 中的可选参数。"""

    def __init__(self, on_change=None):
        super().__init__(structured_output=False)
        self.recording, self.calls = False, []
        self.on_change = on_change

    def checkpoint(self):
        if self.on_change:
            self.on_change(self.calls)

    def adapt(self, func, mcp_tool):
        def call(arguments):
            if not self.recording:
                return func(arguments)
            record = {"tool": mcp_tool.name, "is_error": False, "state": "running"}
            self.calls.append(record)
            self.checkpoint()
            try:
                result = func(arguments)
                record["is_error"] = bool(result.isError)
                return result
            except Exception:
                record["is_error"] = True
                raise
            finally:
                record["state"] = "complete"
                self.checkpoint()

        tool = super().adapt(call, mcp_tool)
        required = mcp_tool.inputSchema.get("required", [])
        tool.inputs = {
            name: {**schema, **({"nullable": True} if name not in required else {})}
            for name, schema in tool.inputs.items()
        }
        return tool


def run_worker(task: Path, run_dir: Path, place_name: str) -> None:
    report = {
        "task": task.relative_to(ROOT / "open-game-eval").as_posix(),
        "status": "running",
        "model": "deepseek-chat",
        "timeout_seconds": TIMEOUT,
        "max_steps": MAX_STEPS,
        "tools": list(TOOL_NAMES),
        "phase": "connect",
        "attempt_id": os.environ["SMOL_EVAL_ATTEMPT_ID"],
        "protocol_version": PROTOCOL_VERSION,
    }

    def save_calls(calls):
        report["tool_calls"] = calls
        save_json(run_dir / "result.json", report)

    adapter = RecordedAdapter(on_change=save_calls)
    save_json(run_dir / "result.json", report)
    started = None
    try:
        with MCPAdapt(configure_mcp(), adapter, client_session_timeout_seconds=60) as connected:
            tools = {tool.name: tool for tool in connected}
            studios = wait_for_studios(tools["list_roblox_studios"], place_name)
            if not studios:
                raise ConnectionError(f"Studio registration timed out for scene {place_name}")
            studio = studios[0]
            report["studio"] = studio
            started = time.monotonic()
            save_json(run_dir / "ready.json", {"monotonic": started})
            report["phase"] = "prepare"
            save_json(run_dir / "result.json", report)
            evaluator = GameEvaluator(tools["execute_luau"], studio["id"], os.environ["SMOL_EVAL_TOKEN"])
            prepared = evaluator.prepare(task)
            if prepared.get("excluded"):
                report.update(status="excluded_multi_client", clients=prepared["clients"])
                return
            report["metadata"] = prepared
            report["phase"] = "agent"
            save_json(run_dir / "result.json", report)
            model = OpenAIModel(
                model_id="deepseek-chat",
                api_base="https://api.deepseek.com",
                api_key=os.environ["DEEPSEEK_API_KEY"],
                client_kwargs={"timeout": TIMEOUT, "max_retries": 0},
            )

            def save_step(step, agent):
                save_json(run_dir / "trajectory.json", agent.memory.get_full_steps())

            agent = ToolCallingAgent(
                model=model,
                max_steps=MAX_STEPS,
                max_tool_threads=1,
                tools=[StudioTool(tools[name], studio["id"]) for name in TOOL_NAMES],
                step_callbacks=[save_step],
                verbosity_level=0,
            )
            adapter.recording = True
            try:
                run = agent.run(prepared["prompt"], return_full_result=True)
                report["agent"] = run.dict()
                report["agent_state"] = run.state
            finally:
                adapter.recording = False
            report["phase"] = "grade"
            save_json(run_dir / "result.json", report)
            actions = []
            for step in run.steps:
                for call in step.get("tool_calls") or []:
                    function = call["function"]
                    arguments = function["arguments"]
                    if isinstance(arguments, str):
                        arguments = json.loads(arguments)
                    actions.append({"origin": function["name"], "command": function["name"], "arguments": arguments})

            def save_grade(grade):
                report["grade"] = grade
                save_json(run_dir / "result.json", report)

            report["grade"] = evaluator.grade(actions, on_phase=save_grade)
            report["status"] = grade_status(report["grade"])
            report["phase"] = "complete"
    except Exception as exc:
        status = {
            "connect": "infrastructure_error",
            "prepare": "setup_error",
            "agent": "agent_error",
            "grade": "infrastructure_error",
        }.get(report["phase"], "infrastructure_error")
        report.update(status=status, error=str(exc))
    finally:
        report["tool_calls"] = adapter.calls
        report["seconds"] = time.monotonic() - started if started else None
        save_json(run_dir / "result.json", report)


def run_attempt(task: Path, run_dir: Path, token: str) -> dict:
    started = time.monotonic()
    run_dir.mkdir(parents=True, exist_ok=True)
    attempt_id = secrets.token_hex(16)
    result_file = run_dir / "result.json"
    # Replace a partial result before launching, so a crashed replacement worker
    # can never inherit a previous attempt's verdict or telemetry.
    save_json(
        result_file,
        {
            "task": task.relative_to(ROOT / "open-game-eval").as_posix(),
            "status": "running",
            "phase": "connect",
            "attempt_id": attempt_id,
            "protocol_version": PROTOCOL_VERSION,
        },
    )
    server = configure_mcp()
    with studio_session(server, run_dir, task_place(task)) as place:
        env = dict(os.environ, SMOL_EVAL_TOKEN=token, SMOL_EVAL_ATTEMPT_ID=attempt_id)
        with (run_dir / "output.log").open("w", encoding="utf-8") as log:
            worker = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--worker", str(task), str(run_dir), place.name],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            timed_out = False
            phase, stage, phase_started = "connect", "startup", started
            try:
                while worker.poll() is None:
                    now = time.monotonic()
                    progress = json.loads((run_dir / "phase.json").read_text(encoding="utf-8"))
                    observed = progress.get("phase", "connect")
                    observed_stage = observed if observed in {"agent", "grade", "complete"} else "startup"
                    if observed_stage != stage:
                        stage, phase_started = observed_stage, now
                    phase = observed
                    budget = TIMEOUT if phase == "agent" else GRADE_TIMEOUT if phase == "grade" else STARTUP_TIMEOUT
                    if now - phase_started >= budget:
                        timed_out = True
                        break
                    time.sleep(0.5)
            finally:
                terminate_process_tree(worker)
        result = (
            json.loads(result_file.read_text(encoding="utf-8"))
            if result_file.exists()
            else {
                "task": task.relative_to(ROOT / "open-game-eval").as_posix(),
                "status": "evaluation_error",
                "error": "Worker stopped before returning a result; see output.log",
            }
        )
        if result.get("attempt_id") != attempt_id:
            result = {
                "task": task.relative_to(ROOT / "open-game-eval").as_posix(),
                "attempt_id": attempt_id,
                "phase": "connect",
                "status": "infrastructure_error",
                "error": "Worker result belongs to another attempt",
            }
        elif timed_out:
            phase = result.get("phase", "connect")
            result.update(
                status="grade_timeout"
                if phase == "grade"
                else "Timeout"
                if phase == "agent"
                else "infrastructure_error",
                timeout_phase=phase,
                timeout_seconds=budget,
                error=f"Attempt exceeded {budget}s during {phase}",
            )
        elif worker.returncode != 0 or result.get("status") == "running":
            result.update(
                status="infrastructure_error", error=f"Worker exited before completion (exit code {worker.returncode})"
            )
        result["place_file"] = str(place)
        result["wall_seconds"] = time.monotonic() - started
        result["attempt_complete"] = True
        save_json(result_file, result)
    return result


def summarize(batch: Path, records: list[dict], current=None) -> dict:
    counts = {}
    for record in records:
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    summary = {
        "batch": str(batch),
        "runs_each": RUNS,
        "timeout_seconds": TIMEOUT,
        "current": current,
        "completed_attempts": len(records),
        "counts": counts,
        "results": records,
    }
    save_json(batch / "summary.json", summary)
    return summary


def suite_metrics(records: list[dict]) -> dict:
    """按公开指标定义计算五次运行的经验统计；基础设施异常单独列出。"""
    groups = {}
    calls = errors = 0
    for record in records:
        result = json.loads(Path(record["result_file"]).read_text(encoding="utf-8"))
        groups.setdefault(record["task"], []).append(record["status"])
        completed_calls = [
            call for call in result.get("tool_calls", []) if call.get("state", "complete") == "complete"
        ]
        calls += len(completed_calls)
        errors += sum(call["is_error"] for call in completed_calls)
    metrics = {}
    for category in ("Evals", "DebugEvals"):
        valid, unscorable, excluded = [], [], []
        for task, statuses in groups.items():
            if not task.startswith(category + "/"):
                continue
            if "excluded_multi_client" in statuses:
                excluded.append(task)
            elif len(statuses) != RUNS or any(
                status in {"evaluation_error", "infrastructure_error", "setup_error", "checker_error", "grade_timeout"}
                for status in statuses
            ):
                unscorable.append(task)
            else:
                valid.append(statuses.count("passed"))
        count = len(valid)
        metrics[category] = {
            "scorable_tasks": count,
            "excluded_multi_client": excluded,
            "unscorable_tasks": unscorable,
            "Pass@1": sum(valid) / (count * RUNS) if count else None,
            "Pass@5": sum(n >= 1 for n in valid) / count if count else None,
            "Cons@5": sum(n >= 3 for n in valid) / count if count else None,
            "All@5": sum(n == RUNS for n in valid) / count if count else None,
        }
    metrics["Avg Tool Error Rate"] = errors / calls if calls else None
    metrics["status_counts"] = {
        status: sum(r["status"] == status for r in records) for status in sorted({r["status"] for r in records})
    }
    metrics["aggregation"] = (
        "empirical five-run frequencies; incomplete or unscorable tasks listed separately; tool errors use completed calls only"
    )
    return metrics


def suite_manifest(tasks: list[Path]) -> dict:
    sources = [
        Path(__file__),
        ROOT / "game_eval_runtime.py",
        ROOT / "open-game-eval" / "EvalPlugin" / "EvalRunner.rbxm",
        *tasks,
    ]
    sources.extend(sorted((ROOT / "open-game-eval" / "Places").glob("*.rbxl")))
    sources.extend(sorted((ROOT / "src" / "smolagents").rglob("*.py")))
    return {
        "protocol_version": PROTOCOL_VERSION,
        "tasks": [t.relative_to(ROOT / "open-game-eval").as_posix() for t in tasks],
        "runs_each": RUNS,
        "timeout_seconds": TIMEOUT,
        "phase_budgets": {"startup": STARTUP_TIMEOUT, "agent": TIMEOUT, "grade": GRADE_TIMEOUT},
        "dependencies": {
            name: importlib.metadata.version(name) for name in ("smolagents", "mcp", "mcpadapt", "openai", "lz4")
        },
        "excluded": "multi-client only",
        "source_hashes": {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
    }


def check_resume_manifest(batch: Path, manifest: dict) -> None:
    previous = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
    if previous != manifest:
        raise ValueError(
            "Cannot resume: evaluation protocol, sources or configuration changed. Start a new batch to avoid mixing incompatible scores."
        )


def run_suite() -> None:
    # 只复用原题，不生成新检查规则；开发题和 Debug 题均纳入清单。
    tasks = [
        task
        for category in ("Evals", "DebugEvals")
        for task in sorted((ROOT / "open-game-eval" / category).glob("*.lua"))
    ]
    resume = "--resume" in sys.argv
    batch = (
        Path(sys.argv[sys.argv.index("--resume") + 1]).resolve()
        if resume
        else (ROOT / "local_results" / f"full-{datetime.now():%Y%m%d-%H%M%S}")
    )
    batch.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(24)
    manifest = suite_manifest(tasks)
    if resume:
        check_resume_manifest(batch, manifest)
    else:
        save_json(batch / "manifest.json", manifest)
    records = json.loads((batch / "summary.json").read_text(encoding="utf-8"))["results"] if resume else []
    finished = {(r["task"], r["repeat"]) for r in records}
    excluded = {r["task"] for r in records if r["status"] == "excluded_multi_client"}
    print(f"批次：{batch}；{len(tasks)} 题，每题 {RUNS} 次，单次 {TIMEOUT}s，串行运行。", flush=True)
    with scoring_plugin(token, batch):
        for task in tasks:
            for repeat in range(1, RUNS + 1):
                relative = task.relative_to(ROOT / "open-game-eval")
                if relative.as_posix() in excluded:
                    break
                if (relative.as_posix(), repeat) in finished:
                    continue
                run_dir = batch / relative.parent / task.stem / f"run-{repeat}"
                summarize(batch, records, {"task": relative.as_posix(), "repeat": repeat})
                print(f"[{len(records) + 1}] {relative} {repeat}/{RUNS}", flush=True)
                try:
                    result = run_attempt(task, run_dir, token)
                except Exception as exc:
                    run_dir.mkdir(parents=True, exist_ok=True)
                    result_file = run_dir / "result.json"
                    result = json.loads(result_file.read_text(encoding="utf-8")) if result_file.exists() else {}
                    result.update(task=relative.as_posix(), status="infrastructure_error", error=str(exc))
                    save_json(run_dir / "result.json", result)
                records.append(
                    {
                        "task": relative.as_posix(),
                        "repeat": repeat,
                        "status": result["status"],
                        "result_file": str(run_dir / "result.json"),
                    }
                )
                print(f"  {result['status']}", flush=True)
                summarize(batch, records)
                if result["status"] == "excluded_multi_client":
                    break
    summary = summarize(batch, records)
    summary["complete"] = True
    summary["metrics"] = suite_metrics(records)
    save_json(batch / "summary.json", summary)
    print(f"全量结束：{batch / 'summary.json'}", flush=True)


def run_baseline() -> None:
    """保留函数名兼容旧调用，默认执行完整的串行题库。"""
    run_suite()


class BaselineTests(unittest.TestCase):
    """随单文件保留必要单元测试，不连接 Studio 或模型。"""

    def test_atomic_json_retries_transient_windows_file_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "summary.json"
            save_json(path, {"completed": 25})
            replace = Path.replace
            attempts = []

            def locked_then_replace(temporary, target):
                self.assertEqual(json.loads(path.read_text())["completed"], 25)
                attempts.append(temporary)
                if len(attempts) < 3:
                    raise PermissionError("sharing violation")
                return replace(temporary, target)

            with patch.object(Path, "replace", locked_then_replace), patch("time.sleep") as sleep:
                save_json(path, {"completed": 26})
            self.assertEqual(json.loads(path.read_text())["completed"], 26)
            self.assertEqual(sleep.call_count, 2)
            self.assertFalse(list(Path(directory).glob("*.tmp")))

    def test_atomic_json_preserves_old_result_when_lock_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "summary.json"
            save_json(path, {"completed": 25})
            with patch.object(Path, "replace", side_effect=PermissionError("locked")) as replace, patch("time.sleep"):
                with self.assertRaises(PermissionError):
                    save_json(path, {"completed": 26})
            self.assertEqual(replace.call_count, 8)
            self.assertEqual(json.loads(path.read_text())["completed"], 25)
            self.assertFalse(list(Path(directory).glob("*.tmp")))

    def test_prepare_routes_original_012_check_without_writing_task(self):
        task = ROOT / "open-game-eval/Evals/012_remove_legs.lua"
        before = task.read_bytes()
        evaluator = GameEvaluator(Mock(), "studio", "token")
        evaluator.call = Mock(return_value={"hasPlay": True, "clients": 1})
        metadata = evaluator.prepare(task)
        operation, payload = evaluator.call.call_args.args
        self.assertEqual(operation, "prepare")
        self.assertIn(before.decode("utf-8-sig").rsplit("return eval", 1)[0], payload["source"])
        self.assertIn("eval.runConfig = {clientChecks = {eval.check_game}}", payload["source"])
        self.assertEqual(metadata["runtime_adapter"]["check_game_context"], "client")
        self.assertEqual(task.read_bytes(), before)

    def test_unrouted_tasks_keep_original_runtime_source(self):
        for name in ("021_gem_orbiting_part", "010_left_shift_sprint_5s"):
            with self.subTest(task=name):
                task = ROOT / "open-game-eval/Evals" / f"{name}.lua"
                source, metadata = LocalRuntimeAdapter.adapt(task)
                self.assertEqual(source, task.read_bytes().decode("utf-8-sig"))
                self.assertEqual(metadata["check_game_context"], "original")

    def test_routing_records_original_source_digest(self):
        task = ROOT / "open-game-eval/Evals/012_remove_legs.lua"
        source, metadata = LocalRuntimeAdapter.adapt(task)
        self.assertEqual(metadata["original_sha256"], hashlib.sha256(task.read_bytes()).hexdigest())
        self.assertEqual(metadata["runtime_source_sha256"], hashlib.sha256(source.encode("utf-8")).hexdigest())
        self.assertNotEqual(metadata["original_sha256"], metadata["runtime_source_sha256"])

    def test_raw_mcp_errors_and_optional_fields_are_preserved(self):
        from mcp.types import CallToolResult, TextContent
        from mcp.types import Tool as MCPTool

        schema = MCPTool(
            name="example",
            inputSchema={
                "type": "object",
                "required": ["code"],
                "properties": {"code": {"type": "string"}, "line": {"type": "integer"}},
            },
        )
        response = CallToolResult(isError=True, content=[TextContent(type="text", text="error")])
        adapter = RecordedAdapter()
        tool = adapter.adapt(Mock(return_value=response), schema)
        adapter.recording = True
        self.assertEqual(tool(code="example"), "error")
        self.assertTrue(tool.inputs["line"]["nullable"])
        self.assertTrue(adapter.calls[0]["is_error"])

    def test_hard_timeout_terminates_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            worker = Mock(returncode=None)
            worker.poll.side_effect = lambda: worker.returncode
            worker.terminate.side_effect = lambda: setattr(worker, "returncode", 1)
            with (
                patch.object(sys.modules[__name__], "studio_session") as session,
                patch("subprocess.Popen", return_value=worker),
                patch("time.monotonic", side_effect=[0, STARTUP_TIMEOUT + 1, STARTUP_TIMEOUT + 1]),
            ):
                session.return_value.__enter__.return_value = run_dir / "place.rbxl"
                result = run_attempt(ROOT / "open-game-eval" / "Evals" / "001_make_cars_faster.lua", run_dir, "test")
            self.assertEqual(result["status"], "infrastructure_error")
            self.assertEqual(result["timeout_phase"], "connect")
            worker.terminate.assert_called_once()
            worker.wait.assert_called_once_with(timeout=15)

    def test_five_run_metrics_do_not_hide_infrastructure_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            records = []
            for task, statuses in [
                ("Evals/a.lua", ["passed"] * 3 + ["failed"] * 2),
                ("Evals/b.lua", ["evaluation_error"] * 5),
            ]:
                for index, status in enumerate(statuses):
                    path = Path(directory) / f"{len(records)}.json"
                    save_json(path, {"tool_calls": []})
                    records.append({"task": task, "repeat": index + 1, "status": status, "result_file": str(path)})
            metrics = suite_metrics(records)["Evals"]
            self.assertEqual(metrics["scorable_tasks"], 1)
            self.assertEqual(metrics["unscorable_tasks"], ["Evals/b.lua"])
            self.assertEqual(metrics["Pass@1"], 0.6)
            self.assertEqual(metrics["Pass@5"], 1)
            self.assertEqual(metrics["Cons@5"], 1)
            self.assertEqual(metrics["All@5"], 0)

    def test_wait_for_studio_registration(self):
        listing = Mock(side_effect=['{"studios": []}', '{"studios": []}', '{"studios": [{"id": "racing"}]}'])
        with patch("time.sleep") as sleep:
            self.assertEqual(wait_for_studios(listing), [{"id": "racing"}])
        self.assertEqual(sleep.call_count, 2)

    def test_no_studio_stops_waiting(self):
        listing = Mock(return_value='{"studios": []}')
        with patch("time.sleep"):
            self.assertEqual(wait_for_studios(listing), [])
        self.assertEqual(listing.call_count, 10)

    def test_mcp_configuration(self):
        with (
            patch("winreg.OpenKey"),
            patch("winreg.QueryValueEx", return_value=(r"C:\Roblox\version\content", 1)),
            patch.object(Path, "is_file", return_value=True),
        ):
            server = configure_mcp()
        self.assertEqual(Path(server.command), Path(r"C:\Roblox\version\StudioMCP.exe"))
        self.assertEqual(server.env, dict(os.environ))

    def test_mcp_configuration_survives_stale_registry_after_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed = root / "Roblox/Versions/version-new"
            installed.mkdir(parents=True)
            (installed / "StudioMCP.exe").touch()
            (installed / "RobloxStudioBeta.exe").touch()
            incomplete = root / "Roblox/Versions/version-incomplete"
            incomplete.mkdir()
            (incomplete / "StudioMCP.exe").touch()
            with (
                patch("winreg.OpenKey"),
                patch("winreg.QueryValueEx", return_value=(str(root / "removed/content"), 1)),
                patch.dict(os.environ, {"LOCALAPPDATA": str(root)}),
            ):
                server = configure_mcp()
            self.assertEqual(Path(server.command), installed / "StudioMCP.exe")

    def test_lua_string_delimiters(self):
        self.assertEqual(lua_string("a]]b]=]c\\n"), "[==[\na]]b]=]c\\n]==]")

    def test_mcp_wrapped_reply(self):
        reply = MARKER + json.dumps({"checks": 1, "passes": 1, "fails": 0})
        wrapped = json.dumps({"content": [{"text": json.dumps(reply)}]})
        self.assertEqual(decode_reply(wrapped)["passes"], 1)
        with self.assertRaises(ValueError):
            decode_reply("Luau execution failed")

    def test_grading(self):
        self.assertEqual(grade_status({"checks": 1, "fails": 0}), "passed")
        self.assertEqual(grade_status({"checks": 1, "fails": 1}), "failed")
        self.assertEqual(grade_status({"checks": 0}), "checker_error")
        self.assertEqual(grade_status({"checks": 1, "error": "crash"}), "checker_error")

    def test_definite_failure_is_not_hidden_by_checker_error(self):
        self.assertEqual(grade_status({"checks": 1, "fails": 1, "error": "crash"}), "failed")

    def test_failed_scene_does_not_start_play_with_invalid_objects(self):
        evaluator = GameEvaluator(Mock(), "studio", "token")
        evaluator.metadata = {"hasPlay": True}
        evaluator.call = Mock(return_value={"checks": 1, "passes": 0, "fails": 1})
        self.assertEqual(grade_status(evaluator.grade([])), "failed")
        evaluator.call.assert_called_once_with("scene", {"actions": []})

    def test_scene_grade_survives_play_connection_failure(self):
        evaluator = GameEvaluator(Mock(), "studio", "token")
        evaluator.metadata = {"hasPlay": True}
        evaluator.call = Mock(side_effect=[{"checks": 1, "passes": 1, "fails": 0}, ConnectionError("lost connection")])
        saved = []
        with self.assertRaises(ConnectionError):
            evaluator.grade([], on_phase=saved.append)
        self.assertEqual(saved[0]["passes"], 1)
        self.assertEqual(saved[0]["phases"][0]["source"], "edit")

    def test_tool_call_is_checkpointed_before_and_after_execution(self):
        from mcp.types import CallToolResult, TextContent
        from mcp.types import Tool as MCPTool

        snapshots = []
        adapter = RecordedAdapter(lambda calls: snapshots.append(json.loads(json.dumps(calls))))
        tool = adapter.adapt(
            Mock(return_value=CallToolResult(isError=True, content=[TextContent(type="text", text="error")])),
            MCPTool(name="example", inputSchema={"type": "object", "properties": {}}),
        )
        adapter.recording = True
        tool()
        self.assertEqual(snapshots[0][0]["state"], "running")
        self.assertEqual(snapshots[1][0]["state"], "complete")
        self.assertTrue(snapshots[1][0]["is_error"])

    def test_replacement_worker_cannot_reuse_old_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            save_json(run_dir / "result.json", {"status": "passed", "old_result": True})
            worker = Mock(returncode=1)
            worker.poll.return_value = 1
            with (
                patch.object(sys.modules[__name__], "configure_mcp", return_value=Mock()),
                patch.object(sys.modules[__name__], "studio_session") as session,
                patch("subprocess.Popen", return_value=worker),
            ):
                session.return_value.__enter__.return_value = run_dir / "new.rbxl"
                result = run_attempt(ROOT / "open-game-eval/Evals/001_make_cars_faster.lua", run_dir, "test")
            self.assertEqual(result["status"], "infrastructure_error")
            self.assertNotIn("old_result", result)

    def test_grade_timeout_preserves_checkpointed_telemetry(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            worker = Mock(returncode=None)
            worker.poll.side_effect = lambda: worker.returncode
            worker.terminate.side_effect = lambda: setattr(worker, "returncode", 1)

            def launch(*args, **kwargs):
                path = run_dir / "result.json"
                report = json.loads(path.read_text(encoding="utf-8"))
                report.update(
                    phase="grade",
                    tool_calls=[{"is_error": True, "state": "complete"}],
                    grade={"checks": 1, "passes": 1, "fails": 0},
                )
                save_json(path, report)
                return worker

            with (
                patch.object(sys.modules[__name__], "configure_mcp", return_value=Mock()),
                patch.object(sys.modules[__name__], "studio_session") as session,
                patch("subprocess.Popen", side_effect=launch),
                patch("time.monotonic", side_effect=[0, 0, GRADE_TIMEOUT + 1, GRADE_TIMEOUT + 1]),
            ):
                session.return_value.__enter__.return_value = run_dir / "new.rbxl"
                result = run_attempt(ROOT / "open-game-eval/Evals/001_make_cars_faster.lua", run_dir, "test")
            self.assertEqual(result["status"], "grade_timeout")
            self.assertEqual(result["timeout_phase"], "grade")
            self.assertEqual(len(result["tool_calls"]), 1)
            self.assertEqual(result["grade"]["passes"], 1)

    def test_empty_studio_list_reports_registration_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            listing = Mock(name="list_roblox_studios")
            listing.name = "list_roblox_studios"
            with (
                patch.object(sys.modules[__name__], "configure_mcp", return_value=Mock()),
                patch.object(sys.modules[__name__], "MCPAdapt") as connected,
                patch.object(sys.modules[__name__], "wait_for_studios", return_value=[]),
                patch.dict(os.environ, {"SMOL_EVAL_ATTEMPT_ID": "test"}),
            ):
                connected.return_value.__enter__.return_value = [listing]
                run_worker(ROOT / "open-game-eval/Evals/013_inf_cube_fall.lua", Path(directory), "scene.rbxl")
            report = json.loads((Path(directory) / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "infrastructure_error")
            self.assertIn("Studio registration timed out", report["error"])
            self.assertIn("scene.rbxl", report["error"])
            self.assertNotIn("list index out of range", report["error"])

    def test_resume_rejects_changed_protocol_without_overwriting_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            batch = Path(directory)
            save_json(batch / "manifest.json", {"protocol_version": 1})
            with self.assertRaisesRegex(ValueError, "Cannot resume"):
                check_resume_manifest(batch, {"protocol_version": PROTOCOL_VERSION})
            self.assertEqual(json.loads((batch / "manifest.json").read_text())["protocol_version"], 1)

    def test_passed_checks_are_independent_of_agent_stop_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            names = ("list_roblox_studios", *TOOL_NAMES)
            tools = []
            for name in names:
                tool = Mock()
                tool.name = name
                tools.append(tool)
            run = Mock(state="max_steps_error", steps=[])
            run.dict.return_value = {"state": "max_steps_error"}
            grade = {"checks": 3, "passes": 3, "fails": 0, "error": ""}
            with (
                patch.object(sys.modules[__name__], "configure_mcp", return_value=Mock()),
                patch.object(sys.modules[__name__], "MCPAdapt") as connected,
                patch.object(sys.modules[__name__], "wait_for_studios", return_value=[{"id": "test"}]),
                patch.object(sys.modules[__name__], "GameEvaluator") as evaluator,
                patch.object(sys.modules[__name__], "StudioTool", side_effect=lambda tool, studio: tool),
                patch.object(sys.modules[__name__], "OpenAIModel"),
                patch.object(sys.modules[__name__], "ToolCallingAgent") as agent,
                patch.dict(
                    os.environ,
                    {"SMOL_EVAL_ATTEMPT_ID": "test", "SMOL_EVAL_TOKEN": "test", "DEEPSEEK_API_KEY": "unit-test-key"},
                ),
            ):
                connected.return_value.__enter__.return_value = tools
                evaluator.return_value.prepare.return_value = {"prompt": "test"}
                evaluator.return_value.grade.return_value = grade
                agent.return_value.run.return_value = run
                run_worker(ROOT / "open-game-eval/Evals/008_spawn_as_r6.lua", Path(directory), "test.rbxl")
            report = json.loads((Path(directory) / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["agent_state"], "max_steps_error")

    def test_metrics_keep_model_failures_and_ignore_unfinished_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            save_json(
                path,
                {"tool_calls": [{"state": "running", "is_error": False}, {"state": "complete", "is_error": True}]},
            )
            records = [
                {
                    "task": "Evals/a.lua",
                    "repeat": i + 1,
                    "status": "passed" if i == 0 else "failed",
                    "result_file": str(path),
                }
                for i in range(RUNS)
            ]
            metrics = suite_metrics(records)
            self.assertEqual(metrics["Evals"]["scorable_tasks"], 1)
            self.assertEqual(metrics["Evals"]["Pass@1"], 0.2)
            self.assertEqual(metrics["Avg Tool Error Rate"], 1)

    def test_bound_studio_tool(self):
        target = Mock()
        target.name, target.description, target.output_type = "execute_luau", "Execute code", "string"
        target.inputs = {k: {"type": "string", "description": k} for k in ("code", "studio_id", "datamodel_type")}
        tool = StudioTool(target, "chosen")
        tool(code="return 1", studio_id="other", datamodel_type="Server")
        target.assert_called_once_with(code="return 1", studio_id="chosen", datamodel_type="Edit")

    def test_wait_ignores_other_studio_instances(self):
        old = {"id": "old", "name": "racing.rbxl"}
        starting = {"id": "new", "name": None}
        new = {"id": "new", "name": "this-run.rbxl"}
        listing = Mock(side_effect=[json.dumps({"studios": [old, starting]}), json.dumps({"studios": [old, new]})])
        with patch("time.sleep"):
            self.assertEqual(wait_for_studios(listing, "this-run.rbxl"), [new])

    def test_launcher_copies_initial_place_and_installs_plugin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "open-game-eval"
            (source / "Places").mkdir(parents=True)
            (source / "Places" / "racing.rbxl").write_bytes(b"original place")
            (source / "EvalPlugin").mkdir()
            (source / "EvalPlugin" / "EvalRunner.rbxm").write_bytes(b"eval plugin")
            server = StdioServerParameters(command=str(root / "StudioMCP.exe"), env={"HTTP_PROXY": "http://proxy"})
            with (
                patch.object(sys.modules[__name__], "ROOT", root),
                patch.dict(os.environ, {"LOCALAPPDATA": str(root / "appdata")}),
                patch("subprocess.Popen") as launch,
            ):
                place, _ = launch_studio(server, root / "results" / "this-run")
            self.assertEqual(place.read_bytes(), b"original place")
            self.assertEqual(
                (root / "appdata" / "Roblox" / "Plugins" / "EvalRunner.rbxm").read_bytes(), b"eval plugin"
            )
            self.assertEqual(launch.call_args.args[0], [str(root / "RobloxStudioBeta.exe"), str(place)])
            self.assertEqual(launch.call_args.kwargs["env"]["HTTP_PROXY"], "http://proxy")
            self.assertIn("localhost", launch.call_args.kwargs["env"]["NO_PROXY"])

    def test_experiment_lock_rejects_concurrent_run_and_releases(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(sys.modules[__name__], "ROOT", Path(directory)):
            with single_experiment() as first:
                self.assertTrue(first)
                with single_experiment() as second:
                    self.assertFalse(second)
            with single_experiment() as third:
                self.assertTrue(third)

    def test_studio_is_closed_even_after_failure(self):
        process = Mock()
        process.poll.return_value = None
        with patch.object(sys.modules[__name__], "launch_studio", return_value=(Path("run.rbxl"), process)):
            with self.assertRaises(RuntimeError):
                with studio_session(Mock(), Path("run")):
                    raise RuntimeError("agent failed")
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=15)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    if "--worker" in sys.argv:
        run_worker(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
    elif "--self-test" in sys.argv:
        unittest.main(argv=[sys.argv[0]])
    else:
        main()
