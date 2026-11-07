<!---
Copyright 2024 The HuggingFace Team. All rights reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

# Roblox Studio MCP Agent

**基于 smolagents 与 Roblox Studio 内置 MCP 的游戏开发 Agent 和本地评测工具。**

让大语言模型在真实的 Roblox Studio 场景中查找对象、阅读和修改脚本、执行 Luau，并通过 [OpenGameEval](https://github.com/Roblox/open-game-eval) 的场景检查与运行时检查评估修改结果。

本项目基于 [Hugging Face smolagents](https://github.com/huggingface/smolagents) 扩展，保留其 Agent 框架源码，重点提供 Roblox Studio 的工具接入、任务执行和本地评测流程。当前评测入口使用 `ToolCallingAgent` 与 `deepseek-chat`。

[安装与运行](#安装与运行) · [评测与续跑](#评测与续跑) · [验证](#验证) · [评测细节](EVALUATION.md)

## 核心能力

- **在真实场景中执行任务**：通过 Studio 内置的 `StudioMCP.exe` 连接编辑器，读取场景结构、修改脚本和对象，执行游戏开发或调试任务。
- **基于工具反馈逐步修改**：Agent 使用 `ToolCallingAgent` 调用 MCP 工具，根据对象信息、脚本内容和控制台输出决定后续操作。
- **复用原始评测断言**：从 OpenGameEval 的 `Evals` 与 `DebugEvals` 加载任务，复用场景及 Play 模式检查；评测器不向 Agent 提供检查源码或参考解法。
- **隔离每次尝试**：每轮复制初始 Place，绑定独立 Studio 实例；分别限制启动、Agent 执行和评分时间，并在结束后清理本轮进程。
- **保留可审查记录**：保存工具调用、Agent 轨迹、评分结果、日志及源码指纹，支持兼容批次续跑和最多 3 个 Studio 并行调度。

### Agent 使用的工具

| 工具 | 用途 |
| --- | --- |
| `search_game_tree` | 搜索场景中的对象 |
| `inspect_instance` | 查看实例信息和属性 |
| `script_search` | 查找脚本 |
| `script_read` | 读取脚本内容 |
| `script_grep` | 在脚本中搜索文本 |
| `multi_edit` | 批量编辑脚本 |
| `execute_luau` | 执行 Luau，读取或修改场景 |
| `get_console_output` | 获取控制台输出 |

评测入口将工具绑定到指定 Studio 的 Edit 模式；任务完成后的 Play 模式检查由评测器调度。

## 安装与运行

当前本地评测依赖 **Windows、Python 3.10+ 和已安装并登录的 Roblox Studio**。Studio 版本需要包含 `StudioMCP.exe`，并启用内置 MCP。

以下命令使用 PowerShell。

### 1. 获取代码与任务资源

```powershell
git clone --recurse-submodules https://github.com/kecheng-lai/Roblox-Studio-MCP-Agent.git
cd Roblox-Studio-MCP-Agent
```

如果已经克隆过仓库，补充初始化 OpenGameEval 子模块：

```powershell
git submodule update --init --recursive
```

### 2. 安装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[mcp,openai]" lz4
```

### 3. 配置模型并运行

```powershell
$env:DEEPSEEK_API_KEY = "<your-deepseek-api-key>"
.\.venv\Scripts\python.exe evaluate_game.py
```

该命令会启动串行批量评测，遍历 `Evals` 和 `DebugEvals`，每题默认尝试 5 次，自动排除需要多个客户端的任务。脚本定位本机 Studio 安装，复制任务场景并启动编辑器，无需手动打开每一题的 Place。

评测期间会临时安装适配后的 Eval Runner 插件，退出时恢复原插件。批次与逐次记录保存在 `local_results/`。

## 评测与续跑

### 默认配置

| 配置项 | 当前设置 |
| --- | --- |
| Agent | `ToolCallingAgent` |
| 模型 | `deepseek-chat`，通过 DeepSeek 的 OpenAI 兼容接口调用 |
| 最大 Agent 步数 | 30 |
| 单个 Agent 内的工具执行 | 串行 |
| 每题尝试次数 | 5 |
| 启动、连接与准备预算 | 180 秒 |
| Agent 执行预算 | 300 秒 |
| 评分预算 | 180 秒 |
| 并行 Studio 数量 | 1–3，由并行入口的 `--jobs` 指定 |

这些设置来自当前评测脚本；底层 smolagents 支持的其他模型不会自动切换此入口的模型。

### 续跑串行批次

将路径替换为已有的串行批次目录：

```powershell
.\.venv\Scripts\python.exe evaluate_game.py --resume local_results\full-YYYYMMDD-HHMMSS
```

续跑会跳过已完成记录，并检查评测协议、任务、场景、Agent 源码和依赖版本是否兼容。

### 从已有批次创建并行评测

并行入口需要已有批次的任务清单，不支持无参数创建首个批次：

```powershell
.\.venv\Scripts\python.exe evaluate_game_parallel.py --from-batch local_results\full-YYYYMMDD-HHMMSS --jobs 3
```

该命令保留来源批次，创建新的并行批次。只有指纹兼容时才继承已完成记录；不兼容时仅沿用任务清单并记录来源。

续跑已有并行批次时，使用对应目录并保持原并发数：

```powershell
.\.venv\Scripts\python.exe evaluate_game_parallel.py --resume local_results\parallel3-YYYYMMDD-HHMMSS --jobs 3
```

在活动的**并行批次目录**中创建名为 `STOP` 的文件，可停止派发新尝试并等待当前尝试结束；显式续跑前需要移除该文件。插件由单个控制器管理，同一时间只运行一个评测控制器。

### 如何解读结果

批次的 `summary.json` 保存汇总，每次尝试的 `result.json` 保存状态、工具调用及评分信息，`trajectory.json` 和 `output.log` 用于追踪执行过程；提前退出的尝试可能没有完整轨迹。

模型任务失败、连接或启动错误、评分异常应分别分析。集成测试通过只说明对应流程或适配逻辑通过验证；部分任务的成功记录不能代表完整基准成绩，也不能直接与使用不同任务范围和协议的官方榜单比较。

更多运行时适配、指纹兼容和异常处理说明见 [EVALUATION.md](EVALUATION.md)。

## 验证

以下命令运行离线自测，不启动模型评测：

```powershell
.\.venv\Scripts\python.exe evaluate_game.py --self-test
.\.venv\Scripts\python.exe -m unittest tests.test_evaluate_game_parallel tests.test_game_eval_runtime
```

以下命令需要可连接的 Roblox Studio，分别检查适配后任务的 Luau 编译和运行时集成：

```powershell
.\.venv\Scripts\python.exe validate_game_runtime.py --syntax-only
.\.venv\Scripts\python.exe validate_game_runtime.py
```

## 代码结构

```text
.
├── evaluate_game.py             # Agent 配置、Studio 生命周期和串行评测
├── evaluate_game_parallel.py    # 并行调度、检查点和续跑
├── game_eval_runtime.py         # 评测运行时与旧任务适配
├── validate_game_runtime.py     # Studio 编译与运行时验证
├── EVALUATION.md                # 本地评测协议与操作说明
├── open-game-eval/              # 原始任务、场景和评分插件（Git 子模块）
├── src/smolagents/              # Agent 框架、模型接口和 MCP 客户端
└── tests/                      # 框架及评测适配测试
```

## 致谢与许可证

- [smolagents](https://github.com/huggingface/smolagents)：提供 Agent 框架、模型接口和工具抽象。上游使用文档见 [Hugging Face 文档](https://huggingface.co/docs/smolagents)。
- [OpenGameEval](https://github.com/Roblox/open-game-eval)：提供 Roblox 游戏开发任务、初始场景与可执行检查。相关资源遵循其上游许可证。

本仓库采用 [Apache License 2.0](LICENSE)，保留上游版权与许可声明。
