# Nuli

一个用 Python 编写的任务路由工具。你可以在同一个终端里来回处理几件事，Nuli 会把消息接到对应的任务，也可以把整轮工作交给 Codex 会话执行。

## 运行

需要 Python 3.9 或更高版本。默认使用本机已登录的 Codex CLI，支持 ChatGPT 桌面应用自带的 CLI。

首次使用时在 Nuli 仓库目录安装依赖。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

### 启动方式

```sh
.venv/bin/python nuli.py                        # 交互模式
.venv/bin/python nuli.py --edit                 # 交互模式，允许在工作区编辑
.venv/bin/python nuli.py "总结 README.md"       # 执行一次并退出
.venv/bin/python nuli.py --help                 # 查看启动参数
```

当前目录就是工作区。要处理其他项目，先切换到目标目录，再用绝对路径启动 Nuli。

```sh
/path/to/Nuli/.venv/bin/python /path/to/Nuli/nuli.py --edit
```

默认使用 Codex App Server 执行任务。Codex 在当前目录读取和编辑代码、运行测试，按你的指令执行 Git 操作。终端带 `--edit` 时，Codex 可在工作区写入，按自己的策略请求额外审批。看到 `ALLOW CODEX ACTION? [Y/N]` 时可确认或拒绝。通过管道输入时使用只读模式。

Ollama 模式支持 Nuli 的本地文件工具。带 `--edit` 时，每轮可提议创建一个文件或做一处精确替换，预览后输入 `y` 才会写入。

### 交互指令

| 指令 | 功能 |
| --- | --- |
| `/parallel <任务>` | 立即启动一个独立任务，可与当前请求同时执行。 |
| `/bg <命令>` | 在工作目录用本机 shell 执行后台命令，结束时显示退出状态和日志路径。 |
| `/jobs` | 列出当前窗口启动的后台命令及状态。 |
| `/stop <编号>` | 停止指定后台命令。编号可用 `/jobs` 查看，如 `/stop 1`。 |
| `/push` 或「push 吧」 | 请求 Codex 推送当前任务的改动。Ollama 模式会预览本窗口已批准的改动，确认后提交并推送。 |
| `/resume` | 恢复一个中断的请求，并继续处理上次保存的排队消息。也可输入「继续刚才没做完的」。 |
| `/cancel` | 放弃当前等待回复的澄清。 |
| `/exit` | 退出 Nuli，并停止本窗口仍在运行的后台命令。 |

普通消息可在模型执行时继续输入，落盘后显示 `QUEUED`。等待模型时按 `Ctrl+C` 可以中断当前请求。

`/push` 使用当前 Git 分支配置的 upstream 和本机 Git 凭据。Ollama 模式的 Git 预览也会列出将随分支一起推送的本地提交。

### 环境变量

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `YCODE_PROVIDER` | `codex` | 选择 `codex` 或 `ollama`。未指定时，设置下列任一 Ollama 配置会自动选择 `ollama`。 |
| `YCODE_MODEL` | `qwen3:4b` | Ollama 模型名。 |
| `YCODE_BASE_URL` | `http://127.0.0.1:11434` | Ollama 服务地址。 |
| `YCODE_CONTEXT_BYTES` | `49152`（48 KiB） | Ollama 交互模式的上下文预算，最小 `4096`。控制附带的历史，按 UTF-8 序列化大小估算。 |
| `YCODE_MAX_TOOL_CALLS` | `24` | Ollama 模式每轮最多执行的工具调用数，须为正整数。用完预算后，模型会根据已有结果给出回答。 |
| `YCODE_TOKEN_RATES_USD` | 未设置 | JSON 格式的美元费率，单位为每百万 token。设置 `input` 和 `output` 后记录估算费用，`cached_input` 可选，省略时沿用 `input`。 |
| `HTTPS_PROXY` | 未设置 | 配置 HTTPS 代理。macOS 上的 Codex 请求也可沿用已启用的系统 HTTPS 代理。 |
| `SHELL` | `/bin/sh` | macOS 和 Linux 上运行 `/bg` 的 shell。Windows 使用 `ComSpec`，缺省为 `cmd.exe`。 |

配置可直接写在启动命令前。下面的费率仅用于演示格式。

```sh
YCODE_PROVIDER=ollama YCODE_MODEL=qwen3:4b .venv/bin/python nuli.py
YCODE_TOKEN_RATES_USD='{"input":2,"cached_input":0.5,"output":4}' .venv/bin/python nuli.py
```

### 任务续接

用 `继续 [写周报]：把标题改短` 可按唯一的任务标题续接。任务 ID 或唯一的产物路径也可直接定位任务。交互历史保存在 `.ycode/tasks/`，文件名去掉 `.json` 就是任务 ID。后台命令日志保存在 `.ycode/jobs/`。

Nuli 在 `.ycode/task_index.json` 缓存任务卡。普通消息先从所有任务的标题、目标、产物和已有摘要里找候选，再补入最近任务。续聊线索仍不足时，会在本地查找相关原文片段供模型判断。索引可从任务文件重建。

Codex 模式会把原生会话 ID 存到任务 JSON。重启后续接同一任务会恢复对应 Codex 会话，完整执行上下文由 Codex 保存。旧任务首次交给 Codex 时会带上任务目标、产物路径和部分近期对话。

Ollama 模式在未压缩历史达到预算约 70% 时，Nuli 会在终端空闲后启动一小批增量摘要。已启动的摘要会继续跑完，新请求同时使用已有摘要和近期历史。后端若串行推理，新请求可能要等当前摘要批次结束。完整原文仍保存在任务 JSON 中。

使用 Codex 时，本轮请求、附带历史和工具读取到的内容会提交给 Codex。

## 查看运行轨迹

每次交互请求、`/parallel`、`/resume` 或单次命令都会在工作目录的 `.ycode/runs/` 写一份 JSONL。按 `run_id` 找到整次运行。Codex 会话、命令、文件编辑和审批的关键事件以 `codex.` 开头。Nuli 的本地工具通过 `step_id` 和 `tool_call_id` 关联模型调用、工具参数、结果和耗时。

模型调用结束时会记录后端返回的 `usage`。缺少费率或用量时，`estimated_cost_usd` 为 `null`。轨迹包含用户请求、模型回复和工具输出，保存在本地，`.ycode/` 已加入 Git 忽略列表。

## 代码结构

- `nuli.py` 是 CLI 入口，`core/cli.py` 处理终端输入和确认。
- `core/agent.py` 负责路由和工具循环。应用也可以通过 `Assistant(store, chat).send(text, tools)` 接入自己的模型和工具。
- `core/codex_agent.py` 转发 Codex 会话请求、执行状态和审批。
- `core/storage.py` 保存独立的任务历史和待处理消息，可以直接使用已有的 `.ycode/` 数据。
- `core/models.py` 接入 Codex CLI 和 Ollama，`core/jobs.py` 管理后台命令。
- `core/trace.py` 记录每次运行的 JSONL 事件和用量估算。
- `core/tools.py` 提供工作区的列目录、读文件、创建文件和文本替换。文本文件上限为 64 KiB。

## 开发

```sh
.venv/bin/python -m unittest discover -s tests
```

测试通过本地模拟服务验证完整的 CLI 工具调用流程，无需下载模型。
