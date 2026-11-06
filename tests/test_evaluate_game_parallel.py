import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import evaluate_game_parallel as parallel


class ParallelSuiteTests(unittest.TestCase):
    def test_completed_result_is_recovered_after_controller_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            batch = Path(directory)
            path = batch / "Evals/example/run-1/result.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"phase": "complete", "status": "passed"}), encoding="utf-8")
            suite = parallel.ParallelSuite(batch, ["Evals/example.lua"], [], jobs=3)
            self.assertEqual(suite.next_attempt().repeat, 2)
            self.assertEqual(suite.records[0]["status"], "passed")

    def test_two_checker_errors_block_remaining_repeats_only_for_affected_task(self):
        records = [{"task": "Evals/bad.lua", "repeat": i, "status": "checker_error"} for i in (1, 2)]
        with tempfile.TemporaryDirectory() as directory:
            suite = parallel.ParallelSuite(Path(directory), ["Evals/bad.lua", "Evals/good.lua"], records, 3)
            self.assertIn("Evals/bad.lua", suite.blocked)
            self.assertEqual(suite.next_attempt().task, "Evals/good.lua")

    def test_three_attempts_overlap_without_exceeding_limit_or_repeating_finished_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            batch = Path(directory)
            tasks = [f"Evals/task-{i}.lua" for i in range(4)]
            inherited = [{"task": tasks[0], "repeat": 1, "status": "failed", "result_file": "old-result.json"}]
            suite = parallel.ParallelSuite(batch, tasks, inherited, jobs=3)
            barrier = threading.Barrier(3, timeout=5)
            lock = threading.Lock()
            seen, active = [], set()
            peak = 0

            def execute(attempt, token):
                nonlocal peak
                with lock:
                    self.assertNotIn(attempt.task, active)
                    active.add(attempt.task)
                    seen.append((attempt.task, attempt.repeat))
                    peak = max(peak, len(active))
                    first_wave = len(seen) <= 3
                if first_wave:
                    barrier.wait()
                with lock:
                    active.remove(attempt.task)
                return {**attempt.as_dict(), "status": "passed", "result_file": "mock.json"}

            with (
                patch.object(suite, "execute_attempt", side_effect=execute),
                patch.object(parallel.baseline, "RUNS", 2),
                patch.object(parallel.baseline, "suite_metrics", return_value={}),
            ):
                result = suite.run("test-token")
            self.assertEqual(peak, 3)
            self.assertEqual(len(seen), 7)
            self.assertEqual(len(set(seen)), 7)
            self.assertNotIn((tasks[0], 1), seen)
            self.assertEqual(result["completed_attempts"], 8)
            self.assertTrue(result["complete"])
            self.assertEqual(result["counts"], {"failed": 1, "passed": 7})

    def test_multi_client_exclusion_does_not_schedule_remaining_repeats(self):
        with tempfile.TemporaryDirectory() as directory:
            suite = parallel.ParallelSuite(Path(directory), ["Evals/multi.lua"], [], jobs=3)
            with (
                patch.object(
                    suite,
                    "execute_attempt",
                    side_effect=lambda attempt, token: {
                        **attempt.as_dict(),
                        "status": "excluded_multi_client",
                        "result_file": "mock.json",
                    },
                ) as execute,
                patch.object(parallel.baseline, "suite_metrics", return_value={}),
            ):
                result = suite.run("test")
            self.assertEqual(execute.call_count, 1)
            self.assertTrue(result["complete"])

    def test_stop_request_drains_active_attempts_without_starting_more(self):
        with tempfile.TemporaryDirectory() as directory:
            batch = Path(directory)
            suite = parallel.ParallelSuite(batch, [f"Evals/task-{i}.lua" for i in range(5)], [], jobs=3)
            barrier = threading.Barrier(3, timeout=5)

            def execute(attempt, token):
                barrier.wait()
                (batch / "STOP").touch()
                return {**attempt.as_dict(), "status": "passed", "result_file": "mock.json"}

            with patch.object(suite, "execute_attempt", side_effect=execute) as execute:
                result = suite.run("test")
            self.assertEqual(execute.call_count, 3)
            self.assertEqual(result["completed_attempts"], 3)
            self.assertFalse(result["complete"])
            self.assertTrue(result["stopped_by_user"])
            self.assertIsNone(result["current"])

    def test_attempt_failure_is_saved_and_does_not_abort_other_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            batch = Path(directory)
            suite = parallel.ParallelSuite(batch, ["Evals/example.lua"], [], jobs=3)
            with patch.object(parallel.baseline, "run_attempt", side_effect=RuntimeError("connection failed")):
                result = suite.execute_attempt(parallel.Attempt("Evals/example.lua", 1), "test")
            saved = json.loads(Path(result["result_file"]).read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "infrastructure_error")
            self.assertEqual(saved["error"], "connection failed")

    def test_continuation_keeps_original_summary_and_records_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "local_results" / "original"
            source.mkdir(parents=True)
            task = root / "open-game-eval" / "Evals" / "example.lua"
            task.parent.mkdir(parents=True)
            task.write_text("return {}", encoding="utf-8")
            manifest = {"tasks": ["Evals/example.lua"]}
            summary = {
                "stopped_by_user": True,
                "results": [{"task": "Evals/example.lua", "repeat": 1, "status": "failed", "result_file": "old.json"}],
            }
            (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            original = json.dumps(summary).encode()
            (source / "summary.json").write_bytes(original)
            with (
                patch.object(parallel.baseline, "ROOT", root),
                patch.object(parallel.baseline, "suite_manifest", return_value=manifest),
            ):
                suite = parallel.prepare_suite(source, jobs=3, resume=False)
            self.assertNotEqual(suite.batch, source)
            self.assertEqual((source / "summary.json").read_bytes(), original)
            execution = json.loads((suite.batch / "execution.json").read_text(encoding="utf-8"))
            self.assertEqual(execution["source_batch"], str(source))
            self.assertEqual(execution["inherited_attempts"], 1)
            self.assertEqual(suite.records, summary["results"])


if __name__ == "__main__":
    unittest.main()
