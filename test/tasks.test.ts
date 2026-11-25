import { expect, test } from "bun:test";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import type { Chat, Message } from "../src/agent";
import { openAssistant } from "../src/assistant";
import { openTaskStore } from "../src/tasks";

test("a self-contained response takes one model call and is saved in its task", async () => {
  const root = await mkdtemp(join(tmpdir(), "ycode-direct-answer-"));
  let calls = 0;
  const chat: Chat = async (messages, tools) => {
    calls++;
    expect(tools).toEqual([]);
    expect(messages[0]?.content).toContain("Answer in Chinese.");
    const { tasks } = JSON.parse(messages.at(-1)!.content);
    const decision = tasks.length === 0
      ? { action: "new", title: "问候", answer: "你好！" }
      : { action: "resume", id: tasks[0].id, answer: "晚上好！" };
    return { role: "assistant", content: JSON.stringify(decision) };
  };
  try {
    const assistant = await openAssistant(root, chat);
    const tools = {
      definitions: [],
      async execute() { throw new Error("Unexpected tool call"); },
    };
    const options = { instructions: "Answer in Chinese." };
    expect(await assistant.send("你好", tools, options)).toMatchObject({ type: "answer", answer: "你好！" });
    expect(calls).toBe(1);
    expect(await assistant.send("晚上好", tools, options)).toMatchObject({ type: "answer", answer: "晚上好！" });
    expect(calls).toBe(2);
    const tasks = await (await openTaskStore(root)).list();
    expect(tasks).toHaveLength(1);
    expect(tasks[0]?.history.map(message => message.content)).toEqual(["你好", "你好！", "晚上好", "晚上好！"]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("the terminal assistant switches tasks, clarifies follow-ups, and resumes a saved task after restart", async () => {
  const root = await mkdtemp(join(tmpdir(), "ycode-tasks-"));
  const server = Bun.serve({
    hostname: "127.0.0.1",
    port: 0,
    async fetch(request) {
      const { messages, tools } = await request.json() as { messages: Message[]; tools: unknown[] };
      const last = messages.at(-1)!;
      const reply = (content: string) => Response.json({ message: { role: "assistant", content } });

      if (tools.length === 0) {
        const { input, tasks } = JSON.parse(last.content);
        if (input === "写周报") return reply(JSON.stringify({ action: "new", title: "写周报" }));
        if (input === "查大文件") return reply(JSON.stringify({ action: "new", title: "查大文件" }));
        if (input === "刚才那个再短一点") {
          return reply(JSON.stringify({ action: "clarify", question: "你指的是周报还是文件列表？" }));
        }
        const report = tasks.find((task: { title: string }) => task.title === "写周报");
        if (report && input.includes("周报")) return reply(JSON.stringify({ action: "resume", id: report.id }));
        return new Response("Unexpected routing input", { status: 400 });
      }

      if (last.content === "写周报") {
        return Response.json({ message: {
          role: "assistant", content: "",
          tool_calls: [{ function: { name: "read_file", arguments: { path: "notes.md" } } }],
        } });
      }
      if (last.role === "tool" && last.content === "本周发版") return reply("长版周报");
      if (last.content === "查大文件") return reply("文件列表");

      // Switching tasks must keep the original request and tool result, and exclude the other task.
      if (!messages.some(message => message.role === "tool" && message.content === "本周发版") ||
          messages.some(message => message.role === "assistant" && message.content === "文件列表")) {
        return new Response("Wrong task history", { status: 400 });
      }
      if (last.content.includes("Original request: 刚才那个再短一点") && last.content.includes("User's answer: 周报")) {
        return reply("短版周报");
      }
      if (last.content === "继续改周报" && messages.some(message => message.content === "短版周报")) {
        return reply("恢复后继续改周报");
      }
      return new Response("Unexpected task input", { status: 400 });
    },
  });
  async function run(...lines: string[]) {
    const child = Bun.spawn([process.execPath, resolve(import.meta.dir, "../src/cli.ts")], {
      cwd: root,
      env: { ...process.env, YCODE_BASE_URL: server.url.origin },
      stdin: new Blob([lines.join("\n") + "\n"]),
      stdout: "pipe",
      stderr: "pipe",
    });
    const [out, err, code] = await Promise.all([
      new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited,
    ]);
    expect({ err, code }).toEqual({ err: "", code: 0 });
    return out;
  }
  try {
    await writeFile(join(root, "notes.md"), "本周发版");
    const first = await run("写周报", "查大文件", "刚才那个再短一点", "周报", "/exit");
    expect(first).toContain("[写周报] 长版周报\n[查大文件] 文件列表\n你指的是周报还是文件列表？");
    expect(first).toContain("[写周报] 短版周报");
    expect(await run("继续改周报", "/exit")).toContain("[写周报] 恢复后继续改周报");
  } finally {
    server.stop(true);
    await rm(root, { recursive: true, force: true });
  }
});
