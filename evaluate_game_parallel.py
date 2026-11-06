"""Continue an OpenGameEval batch with isolated Studios and one shared plugin.

Run: python evaluate_game_parallel.py --from-batch local_results/full-... --jobs 3
Resume: python evaluate_game_parallel.py --resume local_results/parallel3-...
To stop scheduling, create STOP in the new batch directory; active attempts finish.
"""

import argparse
import hashlib
import json
import os
import secrets
import sys
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import evaluate_game as baseline


@dataclass(frozen=True)
class Attempt:
    task: str
    repeat: int

    def as_dict(self) -> dict:
        return {"task": self.task, "repeat": self.repeat}


class ParallelSuite:
    """Keep scheduling and summary writes on one thread; isolate each attempt."""

    def __init__(self, batch: Path, tasks: list[str], records: list[dict], jobs: int):
        if jobs < 1:
            raise ValueError("jobs must be positive")
        self.batch, self.tasks, self.records, self.jobs = batch, tasks, list(records), jobs
        self.finished = {(record["task"], record["repeat"]) for record in records}
        if len(self.finished) != len(records):
            raise ValueError("Duplicate task/repeat records in source batch")
        self.excluded = {record["task"] for record in records if record["status"] == "excluded_multi_client"}
        self.blocked = {}
        self.active = {}
        # Reconcile completed worker checkpoints not yet committed to summary.
        for task in self.tasks:
            for repeat in range(1, baseline.RUNS + 1):
                if (task, repeat) in self.finished:
                    continue
                path = self.batch / Path(task).with_suffix("") / f"run-{repeat}" / "result.json"
                if not path.exists():
                    continue
                result = json.loads(path.read_text(encoding="utf-8"))
                if result.get("status") not in {None, "running"} and (
                    result.get("attempt_complete") or result.get("phase") == "complete"
                ):
                    self.records.append(
                        {"task": task, "repeat": repeat, "status": result["status"], "result_file": str(path)}
                    )
                    self.finished.add((task, repeat))
        self.excluded.update(record["task"] for record in self.records if record["status"] == "excluded_multi_client")
        for task in tasks:
            self.check_blocked(task)

    def check_blocked(self, task: str) -> None:
        errors = [r for r in self.records if r["task"] == task and r["status"] in {"checker_error", "setup_error"}]
        if len(errors) >= 2:
            self.blocked[task] = "Two evaluator errors; remaining repeats withheld pending diagnosis"

    def next_attempt(self) -> Attempt | None:
        active_tasks = {attempt.task for attempt in self.active.values()}
        for task in self.tasks:
            if task in active_tasks or task in self.excluded or task in self.blocked:
                continue
            for repeat in range(1, baseline.RUNS + 1):
                if (task, repeat) not in self.finished:
                    return Attempt(task, repeat)
        return None

    def checkpoint(self, **extra) -> dict:
        summary = {
            "batch": str(self.batch),
            "runs_each": baseline.RUNS,
            "timeout_seconds": baseline.TIMEOUT,
            "parallel_jobs": self.jobs,
            "current": [attempt.as_dict() for attempt in self.active.values()] or None,
            "completed_attempts": len(self.records),
            "counts": dict(Counter(record["status"] for record in self.records)),
            "results": self.records,
            "complete": False,
            "blocked_tasks": self.blocked,
            **extra,
        }
        baseline.save_json(self.batch / "summary.json", summary)
        return summary

    def execute_attempt(self, attempt: Attempt, token: str) -> dict:
        task = baseline.ROOT / "open-game-eval" / attempt.task
        run_dir = self.batch / Path(attempt.task).with_suffix("") / f"run-{attempt.repeat}"
        try:
            result = baseline.run_attempt(task, run_dir, token)
        except Exception as exc:
            result_file = run_dir / "result.json"
            result = json.loads(result_file.read_text(encoding="utf-8")) if result_file.exists() else {}
            result.update(task=attempt.task, status="infrastructure_error", error=str(exc), attempt_complete=True)
            run_dir.mkdir(parents=True, exist_ok=True)
            baseline.save_json(result_file, result)
        return {**attempt.as_dict(), "status": result["status"], "result_file": str(run_dir / "result.json")}

    def run(self, token: str) -> dict:
        stopping = False
        with ThreadPoolExecutor(max_workers=self.jobs, thread_name_prefix="game-eval") as executor:
            while True:
                stopping = stopping or (self.batch / "STOP").exists()
                while not stopping and len(self.active) < self.jobs:
                    attempt = self.next_attempt()
                    if attempt is None:
                        break
                    future = executor.submit(self.execute_attempt, attempt, token)
                    self.active[future] = attempt
                    self.checkpoint()
                    print(f"Started {attempt.task} {attempt.repeat}/{baseline.RUNS}", flush=True)
                if not self.active:
                    break
                done, _ = wait(self.active, timeout=1, return_when=FIRST_COMPLETED)
                for future in done:
                    attempt = self.active.pop(future)
                    record = future.result()
                    self.records.append(record)
                    self.finished.add((attempt.task, attempt.repeat))
                    if record["status"] == "excluded_multi_client":
                        self.excluded.add(attempt.task)
                    self.check_blocked(attempt.task)
                    self.checkpoint()
                    print(f"Finished {attempt.task} {attempt.repeat}: {record['status']}", flush=True)
        if stopping:
            return self.checkpoint(
                stopped_by_user=True,
                stopped_at=datetime.now().astimezone().isoformat(),
                stop_reason="STOP requested; active attempts drained before closing Studios and restoring plugin",
            )
        return self.checkpoint(
            complete=not bool(self.blocked),
            needs_attention=bool(self.blocked),
            metrics=baseline.suite_metrics(self.records),
        )


def prepare_suite(source: Path, jobs: int, resume: bool) -> ParallelSuite:
    """Verify unchanged scoring sources and retain the sequential batch intact."""
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    tasks = [baseline.ROOT / "open-game-eval" / task for task in manifest["tasks"]]
    current_manifest = baseline.suite_manifest(tasks)
    compatible = manifest == current_manifest
    if resume:
        baseline.check_resume_manifest(source, current_manifest)
    summary = json.loads((source / "summary.json").read_text(encoding="utf-8"))
    scheduler_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if resume:
        execution = json.loads((source / "execution.json").read_text(encoding="utf-8"))
        if execution["scheduler_sha256"] != scheduler_hash or execution["jobs"] != jobs:
            raise ValueError("Parallel scheduler or concurrency changed; create a new continuation batch")
        if (source / "STOP").exists():
            raise ValueError("Remove STOP before explicitly resuming this batch")
        batch = source
    else:
        batch = baseline.ROOT / "local_results" / f"parallel{jobs}-{datetime.now():%Y%m%d-%H%M%S}"
        batch.mkdir(parents=True, exist_ok=False)
        baseline.save_json(batch / "manifest.json", current_manifest)
        baseline.save_json(
            batch / "execution.json",
            {
                "source_batch": str(source),
                "inherited_attempts": len(summary["results"]) if compatible else 0,
                "previous_attempts_archived": len(summary["results"]),
                "protocol_changed": not compatible,
                "jobs": jobs,
                "scheduler_sha256": scheduler_hash,
                "started_at": datetime.now().astimezone().isoformat(),
                "isolation": "one plugin installation; distinct place copies, Studio IDs, workers and MCP clients",
            },
        )
    suite = ParallelSuite(batch, manifest["tasks"], summary["results"] if compatible else [], jobs)
    suite.checkpoint()
    return suite


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--from-batch", type=Path)
    group.add_argument("--resume", type=Path)
    parser.add_argument("--jobs", type=int, choices=range(1, 4), default=3)
    args = parser.parse_args()
    if not os.environ.get("DEEPSEEK_API_KEY"):
        parser.error("DEEPSEEK_API_KEY is required")
    with baseline.single_experiment() as acquired:
        if not acquired:
            return
        suite = prepare_suite((args.resume or args.from_batch).resolve(), args.jobs, bool(args.resume))
        baseline.save_json(suite.batch / "active-run.json", {"pid": os.getpid(), "status": "running"})
        print(f"Batch: {suite.batch}; concurrent Studios: {args.jobs}", flush=True)
        token = secrets.token_hex(24)
        status = "error"
        try:
            with baseline.scoring_plugin(token, suite.batch):
                # Requests remain local to the selected Studio's DataModel.
                summary = suite.run(token)
            status = (
                "needs_attention"
                if summary.get("needs_attention")
                else "complete"
                if summary["complete"]
                else "stopped"
            )
        except BaseException as exc:
            suite.checkpoint(error=f"Controller stopped: {type(exc).__name__}: {exc}")
            raise
        finally:
            baseline.save_json(suite.batch / "active-run.json", {"pid": os.getpid(), "status": status})


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    main()
