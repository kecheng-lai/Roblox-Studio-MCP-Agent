"""Real-Studio integration checks for the evaluator, separate from benchmark runs."""

import json
import secrets
import sys
import tempfile
from pathlib import Path

from mcpadapt.core import MCPAdapt

import evaluate_game as baseline


FIXTURE = """
local eval = {prompt = {{{role = "user", content = "Evaluator integration fixture"}}}}
local preWorkspace
eval.setup = function() preWorkspace = workspace:GetDescendants() end
eval.check_scene = function() end
eval.check_game = function()
    __CLIENT__
    assert(preWorkspace ~= nil, "Pre-agent snapshot was lost")
    for _, item in ipairs(preWorkspace) do
        assert(item.Name ~= "AddedAfterPrepare", "Snapshot was captured after the edit")
    end
    assert(workspace:FindFirstChild("AddedAfterPrepare") ~= nil, "Edited scene was not carried into Play")
    __FAILURE__
end
return eval
"""


def main() -> None:
    output = baseline.ROOT / "local_results" / "repair-20261106" / "integration"
    output.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(24)
    results = []
    server = baseline.configure_mcp()
    with baseline.single_experiment() as acquired:
        if not acquired:
            raise RuntimeError("Another evaluation is still active")
        with baseline.scoring_plugin(token, output), tempfile.TemporaryDirectory() as directory:
            cases = (
                [("server", False, False)]
                if "--syntax-only" in sys.argv
                else [("server", False, False), ("client", True, False), ("failure", False, True)]
            )
            for name, client, failure in cases:
                task = Path(directory) / f"runtime_{name}.lua"
                source = FIXTURE.replace(
                    "__CLIENT__",
                    'local player = game:GetService("Players").LocalPlayer\nplayer:LoadCharacter()' if client else "",
                ).replace("__FAILURE__", 'assert(false, "EXPECTED_DIAGNOSTIC_FAILURE")' if failure else "")
                task.write_text(source, encoding="utf-8")
                run_dir = output / name
                with baseline.studio_session(
                    server, run_dir, baseline.ROOT / "open-game-eval/Places/baseplate.rbxl"
                ) as place:
                    with MCPAdapt(server, baseline.RecordedAdapter(), client_session_timeout_seconds=60) as connected:
                        tools = {tool.name: tool for tool in connected}
                        studio = baseline.wait_for_studios(tools["list_roblox_studios"], place.name)[0]
                        evaluator = baseline.GameEvaluator(tools["execute_luau"], studio["id"], token)
                        if name == "server":
                            tasks = sorted((baseline.ROOT / "open-game-eval/Evals").glob("*.lua")) + sorted(
                                (baseline.ROOT / "open-game-eval/DebugEvals").glob("*.lua")
                            )
                            for offset in range(0, len(tasks), 10):
                                payload = [
                                    {"name": item.name, "source": baseline.LocalRuntimeAdapter.adapt(item)[0]}
                                    for item in tasks[offset : offset + 10]
                                ]
                                compiled = evaluator.execute(f"""
local items = game:GetService("HttpService"):JSONDecode({baseline.lua_string(json.dumps(payload))})
local errors = {{}}
for _, item in ipairs(items) do
    local fn, message = loadstring(item.source, item.name)
    if not fn then table.insert(errors, {{name = item.name, error = tostring(message)}}) end
end
return "__SMOL_GAME_EVAL__" .. game:GetService("HttpService"):JSONEncode({{errors = errors}})
""")
                                if compiled["errors"]:
                                    raise AssertionError(f"Adapted task syntax failed: {compiled}")
                            baseline.save_json(output / "syntax.json", {"compiled_tasks": len(tasks), "errors": []})
                            if "--syntax-only" in sys.argv:
                                print(f"Compiled {len(tasks)} adapted tasks in Studio", flush=True)
                                return
                        evaluator.prepare(task)
                        evaluator.execute(
                            'local p = Instance.new("Part"); p.Name = "AddedAfterPrepare"; p.Parent = workspace; return "__SMOL_GAME_EVAL__{}"'
                        )
                        grade = evaluator.grade([])
                        baseline.save_json(run_dir / "grade.json", grade)
                        expected = "failed" if failure else "passed"
                        actual = baseline.grade_status(grade)
                        if actual != expected:
                            raise AssertionError(f"{name}: expected {expected}, got {actual}: {grade}")
                        if failure and "EXPECTED_DIAGNOSTIC_FAILURE" not in json.dumps(grade):
                            raise AssertionError("Original assertion message was not retained")
                        results.append({"fixture": name, "status": actual, "expected": expected})
                        print(json.dumps(results[-1]), flush=True)
            # Exercise an unchanged real legacy checker that reads setup locals and Script.Source.
            task = baseline.ROOT / "open-game-eval/Evals/026_make_traffic_light_v2.lua"
            run_dir = output / "original_026"
            with baseline.studio_session(server, run_dir, baseline.task_place(task)) as place:
                with MCPAdapt(server, baseline.RecordedAdapter(), client_session_timeout_seconds=60) as connected:
                    tools = {tool.name: tool for tool in connected}
                    studio = baseline.wait_for_studios(tools["list_roblox_studios"], place.name)[0]
                    evaluator = baseline.GameEvaluator(tools["execute_luau"], studio["id"], token)
                    evaluator.prepare(task)
                    evaluator.execute("""
local s = Instance.new("Script")
s.Name = "TrafficLightIntegrationFixture"
s.Source = [==[
-- Integration light fixture, separate from model benchmark results.
local rig = workspace.RoadSections["Regulated 4-Way Intersection"].IntersectionTrafficSignals
while true do
    for _, light in ipairs(rig:GetDescendants()) do
        if light:IsA("SpotLight") then light.Enabled = not light.Enabled end
    end
    task.wait(0.25)
end
]==]
s.Parent = workspace
return "__SMOL_GAME_EVAL__{}"
""")
                    grade = evaluator.grade([])
                    baseline.save_json(run_dir / "grade.json", grade)
                    if baseline.grade_status(grade) != "passed":
                        raise AssertionError(f"Original 026 compatibility failed: {grade}")
                    results.append({"fixture": "original_026", "status": "passed", "expected": "passed"})
                    print(json.dumps(results[-1]), flush=True)
    baseline.save_json(output / "summary.json", {"fixtures": results, "verified": True, "benchmark": False})


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    main()
