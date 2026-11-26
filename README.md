# Ycode

一个用 TypeScript 和 Bun 编写的小型个人 Agent 框架。你在同一个终端里可以来回处理几件事，Ycode 会把消息接到对应的任务。

## 运行

默认调用本机已登录的 [Codex CLI](https://learn.chatgpt.com/docs/non-interactive-mode)，也可以使用 ChatGPT 桌面应用自带的 CLI。直接运行编译好的可执行文件：

```sh
./dist/ycode --edit
```

从源码运行需要 [Bun](https://bun.sh/)：

```sh
bun install
bun start --edit
```

例如在同一个窗口，先说「根据 notes.md 写一份周报，保存到 report.md」，再说「找出当前目录最大的三个文件」，最后说「周报的标题再短一点」。加文件和改文件都会先预览，输入 `y` 后写入。

交互模式在当前目录的 `.ycode/tasks/` 保存任务历史和产物。终端会把任务名显示在回答前的方括号里。看到 `[写周报]` 后，可以输入「继续 [写周报]：把标题改短」，唯一匹配时会直接用该任务的历史执行，省去路由的模型调用。也可以用「继续改 report.md，把标题再短一点」按唯一的产物路径续接，或用「继续 <任务 ID>：把标题再短一点」按 ID 续接。任务 ID 是 `.ycode/tasks/` 下的文件名去掉 `.json`。

归属不明确时，Ycode 会用模型选择任务，必要时再问你指的是哪件事。问候和可以单独回答的问题能在选择任务时一起回答。等待时，终端状态行显示当前阶段和已用时间。输入 `/cancel` 可以结束这次澄清，输入 `/exit` 退出。

等待模型时按 `Ctrl+C` 会停止当前请求并返回输入框。输入「继续刚才没做完的」或 `/resume` 可以重试上一次未完成的请求，从同一个项目目录重开后也可以。已经选中的任务会沿用原来的历史。重试时，文件改动仍要预览和确认。

也可以带上一个任务，单次运行：

```sh
bun start "读一下 src/agent.ts，解释代理什么时候结束一次任务。"
bun start --edit "把 README 标题下面的项目简介改成一句简短的英文"
```

`--edit` 允许每轮提议创建一个新文件或对现有文件做一处精确替换。单次命令不使用交互模式的任务记录。

使用 Codex 时，本轮请求、相关任务历史和工具读取到的内容会提交给 Codex。网络可使用 `HTTPS_PROXY`，在 macOS 上也能沿用已启用的系统 HTTPS 代理。

要改用 [Ollama](https://ollama.com/)，运行 `YCODE_PROVIDER=ollama bun start --edit`。Ollama 默认使用 `qwen3:4b` 和 `http://127.0.0.1:11434`。可通过 `YCODE_MODEL` 和 `YCODE_BASE_URL` 修改，单独设置其中一个也会选择 Ollama。

要在其他项目中使用，先运行 `bun run build`，再切换到目标目录并执行：

```sh
/path/to/Ycode/dist/ycode "这个项目的入口在哪里？"
```

## 代码结构

- `src/cli.ts` 处理命令行输入，以当前目录为工作区。
- `src/assistant.ts` 提供通用入口 `openAssistant(workspace, chat)`。应用调用 `send(text, tools)`，可以传入自己的工具定义和执行函数。
- `src/tasks.ts` 保存任务。Router 读取最近的任务卡，决定新建、接续或澄清。执行器只收到选中任务的历史。
- `src/agent.ts` 负责调用模型和工具，直到模型回答或用完 12 次工具调用额度。
- `src/codex.ts` 调用 `codex exec`，把结构化回复交给 Ycode 处理。
- `src/ollama.ts` 负责 [Ollama 聊天接口](https://docs.ollama.com/api/chat)。要接其他模型服务，可以实现 `Chat` 接口。
- `src/tools.ts` 提供列目录、读文件、创建文件和文本替换。文件工具限定在当前工作区，文本文件上限为 64 KiB。

## 开发

```sh
bun test
bun run check
```

测试通过本地模拟服务验证完整的 CLI 工具调用流程，无需下载模型。
