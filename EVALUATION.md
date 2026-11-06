# Local OpenGameEval execution

The model remains `deepseek-chat`, using `DEEPSEEK_API_KEY`. Tasks and assertion
conditions come from the original OpenGameEval files. The evaluator does not feed
checkers or reference solutions to the agent.

Protocol 4 preserves legacy pre-edit object snapshots across Play mode, routes
legacy client checks, delegates character reloads to the server, and captures
script source with plugin permissions for runtime checks. Engine-created player
characters are included in the runtime baseline rather than counted as new NPCs.
Original task files and the bundled plugin are not rewritten; the installed local
plugin is temporarily instrumented and restored after execution.

Each attempt uses a fresh Place copy and a bound Studio ID. One controller owns
the scoring plugin and manages up to three independent workers. The budgets are
180 seconds for startup/connection/setup, 300 seconds for the agent, and 180
seconds for scoring. Worker and Studio process trees are terminated at cleanup.

Start a continuation using the previous task list:

```powershell
.\.venv\Scripts\python.exe evaluate_game_parallel.py --from-batch local_results\parallel3-20261106-121834 --jobs 3
```

When protocol or source fingerprints differ, this creates a fresh batch with no
inherited scores and records the previous batch as provenance. Matching batches
can inherit completed attempts. Resume an existing compatible batch with
`--resume <batch>`; completed worker files are reconciled with its summary before
dispatch. Source fingerprints cover Places, the agent implementation, the runtime
adapter, tasks, plugin, dependency versions, and scheduling configuration.

Create a file named `STOP` in the active batch directory to stop dispatching and
drain active attempts. Remove it only when explicitly resuming. Two evaluator
errors on one task withhold that task's remaining repeats; other tasks continue.
Blocked tasks are listed separately and prevent the batch from claiming complete
coverage. `checker_error` alone does not establish whether the cause was a broken
checker or a model edit that violated a checker assumption; inspect its details.

Verification:

```powershell
.\.venv\Scripts\python.exe evaluate_game.py --self-test
.\.venv\Scripts\python.exe -m unittest tests.test_evaluate_game_parallel tests.test_game_eval_runtime
.\.venv\Scripts\python.exe validate_game_runtime.py
.\.venv\Scripts\python.exe validate_game_runtime.py --syntax-only
```

The Studio validation fixtures, including an unchanged original 026 checker, are
software integration tests. They are stored separately and are not benchmark
attempts or evidence of model performance.
