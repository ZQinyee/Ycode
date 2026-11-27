# Ycode

一个用 Python 编写的小型个人 Agent 框架。你在同一个终端里可以来回处理几件事，Ycode 会把消息接到对应的任务。

## 运行

需要 Python 3.9 或更高版本。默认调用本机已登录的 Codex CLI，也可以使用 ChatGPT 桌面应用自带的 CLI。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python ycode.py --edit
```

例如在同一个窗口，先说「根据 notes.md 写一份周报，保存到 report.md」，再说「找出当前目录最大的三个文件」，最后说「周报的标题再短一点」。加文件和改文件都会先预览，输入 `y` 后写入。

交互模式在当前目录的 `.ycode/tasks/` 保存任务历史和产物。终端会把任务名显示在回答前的方括号里。看到 `[写周报]` 后，可以输入「继续 [写周报]：把标题改短」，唯一匹配时会直接用该任务的历史执行，省去路由的模型调用。也可以用「继续改 report.md，把标题再短一点」按唯一的产物路径续接，或用「继续 <任务 ID>：把标题再短一点」按 ID 续接。任务 ID 是 `.ycode/tasks/` 下的文件名去掉 `.json`。

归属不明确时，Ycode 会用模型选择任务，必要时再问你指的是哪件事。问候和可以单独回答的问题能在选择任务时一起回答。等待时，状态行显示当前阶段和已用时间，下面的 `YCODE>` 可以接着输入。消息保存到本地后才显示 `QUEUED`，并按发送顺序处理。文件编辑在单独的确认提示里审批。输入 `/cancel` 可以结束这次澄清，输入 `/exit` 退出。

等待模型时按 `Ctrl+C` 会停止当前请求。输入「继续刚才没做完的」或 `/resume` 可以重试上一次未完成的请求。重开同一个项目时，Ycode 会显示保存的排队消息。输入 `/resume` 后，先重试中断的请求，再按原顺序处理恢复的队列。已经选中的任务会沿用原来的历史，文件改动仍要预览和确认。

模型忙时可以输入 `/parallel 总结 README.md`，在同一窗口立即启动一条新任务。它有自己的历史，用回答前显示的任务名可以继续。普通消息按顺序处理，多个任务的文件改动逐个预览和确认。

`/bg .venv/bin/python -m unittest discover -s tests` 会在当前项目目录通过本机 shell 运行测试。你可以继续聊天，命令退出时会显示退出状态和日志路径。`/jobs` 查看本窗口的命令，`/stop 1` 停止编号 1 的命令。用 `/exit` 退出时会停止正在运行的命令，日志保存在项目的 `.ycode/jobs/`。

也可以带上一个任务，单次运行：

```sh
.venv/bin/python ycode.py "读一下 ycode_core/agent.py，解释代理什么时候结束一次任务。"
.venv/bin/python ycode.py --edit "把 README 标题下面的项目简介改成一句简短的英文"
```

`--edit` 允许每轮提议创建一个新文件或对现有文件做一处精确替换。单次命令不使用交互模式的任务记录。

使用 Codex 时，本轮请求、相关任务历史和工具读取到的内容会提交给 Codex。网络可使用 `HTTPS_PROXY`，在 macOS 上也能沿用已启用的系统 HTTPS 代理。

要改用 Ollama，运行 `YCODE_PROVIDER=ollama .venv/bin/python ycode.py --edit`。Ollama 默认使用 `qwen3:4b` 和 `http://127.0.0.1:11434`。可通过 `YCODE_MODEL` 和 `YCODE_BASE_URL` 修改，单独设置其中一个也会选择 Ollama。

在其他项目中使用时，先切换到目标目录，再通过绝对路径运行 Ycode。

```sh
/path/to/Ycode/.venv/bin/python /path/to/Ycode/ycode.py "这个项目的入口在哪里？"
```

## 查看运行轨迹

每次交互请求、`/parallel`、`/resume` 或单次命令都会在工作目录的 `.ycode/runs/` 写一份 JSONL。按 `run_id` 确定是哪次运行，按 `step_id` 找到模型调用，按 `tool_call_id` 对上工具的开始和结束。轨迹包含路由决定、工具参数和结果、耗时、错误及取消状态。文件工具提出的修改和实际应用的结果分别记录。

模型调用结束时会记录后端返回的 `usage`。可以通过 `YCODE_TOKEN_RATES_USD` 提供每百万 token 的美元费率，以计算每次模型调用的估算费用。

```sh
YCODE_TOKEN_RATES_USD='{"input":2,"cached_input":0.5,"output":4}' .venv/bin/python ycode.py "读一下 README"
```

上面的费率用于举例。缺少费率或用量时，`estimated_cost_usd` 为 `null`。未单独设置缓存输入费率时使用普通输入费率。轨迹包含用户请求、模型回复和工具输出，保存在本地，`.ycode/` 已加入 Git 忽略列表。

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
