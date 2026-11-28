# Ycode

一个用 Python 编写的小型个人 Agent 框架。你在同一个终端里可以来回处理几件事，Ycode 会把消息接到对应的任务。

## 运行

需要 Python 3.9 或更高版本。默认使用本机已登录的 Codex CLI，支持 ChatGPT 桌面应用自带的 CLI。

首次使用时在 Ycode 仓库目录安装依赖。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

### 启动方式

```sh
.venv/bin/python ycode.py                        # 交互模式
.venv/bin/python ycode.py --edit                 # 交互模式，允许提议文件改动
.venv/bin/python ycode.py "总结 README.md"       # 执行一次并退出
.venv/bin/python ycode.py --help                 # 查看启动参数
```

当前目录就是工作区。要处理其他项目，先切换到目标目录，再用绝对路径启动 Ycode。

```sh
/path/to/Ycode/.venv/bin/python /path/to/Ycode/ycode.py --edit
```

`--edit` 也可用于单次任务。每轮可提议创建一个文件或做一处精确替换，预览后输入 `y` 才会写入。

### 交互指令

| 指令 | 功能 |
| --- | --- |
| `/parallel <任务>` | 立即启动一个独立任务，可与当前请求同时执行。 |
| `/bg <命令>` | 在工作目录用本机 shell 执行后台命令，结束时显示退出状态和日志路径。 |
| `/jobs` | 列出当前窗口启动的后台命令及状态。 |
| `/stop <编号>` | 停止指定后台命令。编号可用 `/jobs` 查看，如 `/stop 1`。 |
| `/resume` | 恢复一个中断的请求，并继续处理上次保存的排队消息。也可输入「继续刚才没做完的」。 |
| `/cancel` | 放弃当前等待回复的澄清。 |
| `/exit` | 退出 Ycode，并停止本窗口仍在运行的后台命令。 |

普通消息可在模型执行时继续输入，落盘后显示 `QUEUED`。等待模型时按 `Ctrl+C` 可以中断当前请求。

### 环境变量

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `YCODE_PROVIDER` | `codex` | 选择 `codex` 或 `ollama`。未指定时，设置下列任一 Ollama 配置会自动选择 `ollama`。 |
| `YCODE_MODEL` | `qwen3:4b` | Ollama 模型名。 |
| `YCODE_BASE_URL` | `http://127.0.0.1:11434` | Ollama 服务地址。 |
| `YCODE_CONTEXT_BYTES` | `49152`（48 KiB） | 交互模式的上下文预算，最小 `4096`。控制附带的历史，按 UTF-8 序列化大小估算。 |
| `YCODE_TOKEN_RATES_USD` | 未设置 | JSON 格式的美元费率，单位为每百万 token。设置 `input` 和 `output` 后记录估算费用，`cached_input` 可选，省略时沿用 `input`。 |
| `HTTPS_PROXY` | 未设置 | 配置 HTTPS 代理。macOS 上的 Codex 请求也可沿用已启用的系统 HTTPS 代理。 |
| `SHELL` | `/bin/sh` | macOS 和 Linux 上运行 `/bg` 的 shell。Windows 使用 `ComSpec`，缺省为 `cmd.exe`。 |

配置可直接写在启动命令前。下面的费率仅用于演示格式。

```sh
YCODE_PROVIDER=ollama YCODE_MODEL=qwen3:4b .venv/bin/python ycode.py
YCODE_TOKEN_RATES_USD='{"input":2,"cached_input":0.5,"output":4}' .venv/bin/python ycode.py
```

### 任务续接

用 `继续 [写周报]：把标题改短` 可按唯一的任务标题续接。任务 ID 或唯一的产物路径也可直接定位任务。交互历史保存在 `.ycode/tasks/`，文件名去掉 `.json` 就是任务 ID。后台命令日志保存在 `.ycode/jobs/`。

未压缩的历史达到预算约 70% 时，Ycode 会在终端空闲后启动一小批增量摘要。已启动的摘要会继续跑完，新请求同时使用已有摘要和近期历史。后端若串行推理，新请求可能要等当前摘要批次结束。完整原文仍保存在任务 JSON 中。

使用 Codex 时，本轮请求、附带历史和工具读取到的内容会提交给 Codex。

## 查看运行轨迹

每次交互请求、`/parallel`、`/resume` 或单次命令都会在工作目录的 `.ycode/runs/` 写一份 JSONL。按 `run_id` 确定是哪次运行，按 `step_id` 找到模型调用，按 `tool_call_id` 对上工具的开始和结束。轨迹包含路由决定、工具参数和结果、耗时、错误及取消状态。文件工具提出的修改和实际应用的结果分别记录。

模型调用结束时会记录后端返回的 `usage`。缺少费率或用量时，`estimated_cost_usd` 为 `null`。轨迹包含用户请求、模型回复和工具输出，保存在本地，`.ycode/` 已加入 Git 忽略列表。

## 代码结构

- `ycode.py` 是 CLI 入口，`ycode_core/cli.py` 处理终端输入和确认。
- `ycode_core/agent.py` 负责路由和工具循环，每轮最多执行 12 次工具调用。应用也可以通过 `Assistant(store, chat).send(text, tools)` 接入自己的模型和工具。
- `ycode_core/storage.py` 保存独立的任务历史和待处理消息，可以直接使用已有的 `.ycode/` 数据。
- `ycode_core/models.py` 接入 Codex CLI 和 Ollama，`ycode_core/jobs.py` 管理后台命令。
- `ycode_core/trace.py` 记录每次运行的 JSONL 事件和用量估算。
- `ycode_core/tools.py` 提供工作区的列目录、读文件、创建文件和文本替换。文本文件上限为 64 KiB。

## 开发

```sh
.venv/bin/python -m unittest discover -s tests
```

测试通过本地模拟服务验证完整的 CLI 工具调用流程，无需下载模型。
