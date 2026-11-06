import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import evaluate_game as baseline
from game_eval_runtime import RuntimeSource, instrument_source, patch_plugin


class RuntimeCompatibilityTests(unittest.TestCase):
    def test_setup_snapshot_is_restored_and_assertions_are_preserved(self):
        original = """local preWorkspace
local eval = {}
eval.setup = function() preWorkspace = workspace:GetDescendants() end
eval.check_game = function() assert(#preWorkspace > 0, "original condition") end
return eval"""
        source, metadata = RuntimeSource.adapt(original)
        self.assertEqual(metadata["state_bindings"], ["preWorkspace"])
        self.assertIn('preWorkspace = __smolRestore("preWorkspace")', source)
        self.assertIn('__smolSave("preWorkspace", preWorkspace)', source)
        self.assertIn('assert(#preWorkspace > 0, "original condition")', source)

    def test_mixed_client_and_server_apis_are_routed_without_rewriting_assertions(self):
        original = """local eval = {}
eval.check_game = function()
local player = game:GetService("Players").LocalPlayer
player:LoadCharacter()
game.ReplicatedStorage.UpdateHP:FireServer(true)
assert(player.Character ~= nil, "character missing")
end
return eval"""
        source, metadata = RuntimeSource.adapt(original)
        self.assertEqual(metadata["check_game_context"], "client")
        self.assertEqual(metadata["server_reload_calls"], 1)
        self.assertIn("__smolReloadCharacter(player)", source)
        self.assertIn('assert(player.Character ~= nil, "character missing")', source)

    def test_native_client_checks_and_embedded_gameplay_scripts_do_not_get_rerouted(self):
        task = baseline.ROOT / "open-game-eval/Evals/029_play_idle_animation.lua"
        original = task.read_text(encoding="utf-8-sig")
        source, metadata = RuntimeSource.adapt(original)
        self.assertEqual(source, original)
        self.assertEqual(metadata["check_game_context"], "original")

    def test_empty_legacy_check_does_not_capture_later_native_client_checks(self):
        for name in ["090_fps_display_target_damage_ui", "092_fps_shoot_ground_bounce", "105_lasertag_friendly_fire"]:
            original = (baseline.ROOT / "open-game-eval/Evals" / f"{name}.lua").read_text(encoding="utf-8-sig")
            source, metadata = RuntimeSource.adapt(original)
            self.assertEqual(source, original)
            self.assertEqual(metadata["check_game_context"], "original")

    def test_script_source_reader_preserves_literals_and_uses_plugin_snapshot(self):
        original = """local eval = {}
eval.check_game = function()
local object = workspace.MyScript
assert(object.Source:find("LocalPlayer object.Source"), "source text missing")
end
return eval"""
        source, metadata = RuntimeSource.adapt(original)
        self.assertEqual(metadata["source_read_calls"], 1)
        self.assertEqual(metadata["check_game_context"], "original")
        self.assertIn('__smolReadSource(object):find("LocalPlayer object.Source")', source)

    def test_plugin_patch_preserves_original_check_conditions(self):
        source = (
            'utils.warning = ""\nutils.runChecks = function() end\nlocal function mockAssert() end\nutils.fails += 1'
        )
        encoded = source.encode()
        prop = struct.pack("<II", 0, 6) + b"Source" + b"\x01" + struct.pack("<I", len(encoded)) + encoded
        plugin = b"\x00" * 32 + struct.pack("<4sIII", b"PROP", 0, len(prop), 0) + prop
        patched = patch_plugin(plugin, "\n-- bridge")
        self.assertIn(b"PROP", patched)
        instrumented = instrument_source(source)
        self.assertIn("utils.fails += 1", instrumented)
        self.assertIn("table.insert(utils.failures", instrumented)

    def test_manifest_covers_places_agent_code_and_dependencies(self):
        manifest = baseline.suite_manifest([baseline.ROOT / "open-game-eval/Evals/020_gravity_well.lua"])
        self.assertIn("open-game-eval/Places/baseplate.rbxl", manifest["source_hashes"])
        self.assertIn("src/smolagents/agents.py", manifest["source_hashes"])
        self.assertIn("game_eval_runtime.py", manifest["source_hashes"])
        self.assertIn("openai", manifest["dependencies"])
        self.assertEqual(manifest["phase_budgets"]["agent"], 300)

    def test_windows_cleanup_targets_the_worker_process_tree(self):
        process = Mock(pid=12345)
        process.poll.side_effect = [None, 0]
        with patch("subprocess.run") as terminate:
            baseline.terminate_process_tree(process)
        self.assertEqual(terminate.call_args.args[0], ["taskkill", "/PID", "12345", "/T", "/F"])
        process.wait.assert_called_once_with(timeout=15)

    def test_result_checkpoint_writes_lightweight_phase_for_watchdog(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            baseline.save_json(path, {"phase": "agent", "attempt_id": "abc", "large_payload": "x" * 1000})
            phase = json.loads(path.with_name("phase.json").read_text(encoding="utf-8"))
            self.assertEqual(phase, {"phase": "agent", "attempt_id": "abc"})
