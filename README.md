# Ycode

一个用 TypeScript 和 Bun 编写的精简编程代理框架。第一版是只读的终端助手：你在一个项目目录里提问，模型可以列目录、读取文本文件，再根据内容回答。

## 运行

需要 [Bun](https://bun.sh/) 和一个正在运行的 [Ollama](https://ollama.com/) 服务。默认使用支持工具调用的 `qwen3:4b`。

```sh
ollama pull qwen3:4b
bun install
bun start "读一下 src/agent.ts，解释代理什么时候结束一次任务。"
```

模型名称可通过 `YCODE_MODEL` 修改。默认服务地址为 `http://127.0.0.1:11434`。如果用 `YCODE_BASE_URL` 指向其他 Ollama 服务，问题和被读取的文件内容也会发送给该服务。

要在其他项目中使用，先运行 `bun run build`，再切换到目标目录并执行：

```sh
/path/to/Ycode/dist/ycode "这个项目的入口在哪里？"
```

## 代码结构

- `src/cli.ts` 处理命令行输入，以当前目录为工作区。
- `src/agent.ts` 调用模型，把工具结果放回对话，直到模型回答或用完 12 次工具调用额度。
- `src/ollama.ts` 负责 [Ollama 聊天接口](https://docs.ollama.com/api/chat)。要接其他模型服务，可以实现 `Chat` 接口。
- `src/tools.ts` 注册工具的说明和执行函数。初始工具为 `list_files` 和 `read_file`，限制访问工作区内的非隐藏路径，跳过 `node_modules`。文本文件上限为 64 KiB。

## 开发

```sh
bun test
bun run check
```

测试通过本地模拟服务验证完整的 CLI 工具调用流程，无需下载模型。
