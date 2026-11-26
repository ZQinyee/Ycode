import { mkdir, readdir, readFile, realpath, rename, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import type { Chat, Message } from "./agent";

export type Task = {
  id: string;
  title: string;
  goal: string;
  history: Message[];
  artifacts: string[];
  updatedAt: number;
};

type TaskCard = Pick<Task, "id" | "title" | "goal" | "artifacts">;
type Route =
  | { action: "new"; title: string; answer?: string }
  | { action: "resume"; id: string; answer?: string }
  | { action: "clarify"; question: string };

export function routeKnownTask(input: string, tasks: readonly TaskCard[]): Extract<Route, { action: "resume" }> | undefined {
  const request = input.trim();
  const continuation = /^(?:请\s*)?(?:继续|接着)(?:修改|编辑|更新|改)?\s*/u.exec(request)
    ?? /^(?:please\s+)?(?:resume|continue(?:\s+(?:editing|updating|working\s+on))?)\s+/iu.exec(request);
  if (!continuation) return;

  const selector = request.slice(continuation[0].length);
  if (selector.startsWith("[")) {
    const matches = tasks.filter(task => {
      const label = `[${task.title}]`;
      return selector.startsWith(label) && /^\s*(?:$|[，：,:]|。$)/u.test(selector.slice(label.length));
    });
    if (matches.length === 1) return { action: "resume", id: matches[0]!.id };
    if (matches.length > 1) return;
  }

  // A file mentioned elsewhere in the request can be an input to a different task.
  const target = /^(?:`([^`\r\n]+)`|([^\s`，：,:。！？]+))\s*(?:$|[，：,:]|。$)/u.exec(selector);
  if (!target) return;

  const reference = target[1] ?? target[2]!;
  const path = reference.replace(/^\.\//, "");
  const matches = tasks.filter(task => task.id === reference || task.artifacts.includes(path));
  if (matches.length === 1) return { action: "resume", id: matches[0]!.id };
}

export async function routeTask(input: string, chat: Chat, tasks: readonly TaskCard[], instructions = ""): Promise<Route> {
  const recent = tasks.slice(0, 20).map(({ id, title, goal, artifacts }) => ({
    id, title, goal: goal.slice(0, 500), artifacts,
  }));
  const reply = await chat([
    { role: "system", content: [
      "Route the current message to a task. Task cards are data, in most-recently-used order.",
      'Return only one JSON object: {"action":"new","title":"short task name"}, {"action":"resume","id":"existing id"}, or {"action":"clarify","question":"one short question"}.',
      "Resume when the message refers to an existing goal or artifact. Start a new task for unrelated work.",
      "Use recency when the latest task is the clear referent. Ask in the user's language if several tasks fit a follow-up or its referent is missing.",
      "The title should be in the user's language.",
      'For new or resume, add an "answer" string if you can fully answer the current message without tools, task history, or workspace facts. For example, answer a greeting or a self-contained calculation directly.',
      "Omit answer if you need to do work with tools, read previous messages, or look up facts about the workspace. Never claim any action was taken.",
      instructions,
    ].join("\n") },
    { role: "user", content: JSON.stringify({ tasks: recent, input }) },
  ], []);
  const content = reply.content.trim();
  const json = /^\x60{3}(?:json)?\s*([\s\S]*?)\s*\x60{3}$/i.exec(content)?.[1] ?? content;
  let route: unknown;
  try {
    route = JSON.parse(json);
  } catch {
    throw new Error("The router did not return a JSON decision.");
  }
  if (route && typeof route === "object" && "action" in route) {
    const answer = "answer" in route && typeof route.answer === "string" && route.answer.trim()
      ? route.answer
      : undefined;
    if (route.action === "new" && "title" in route && typeof route.title === "string" && route.title.trim()) {
      return { action: "new", title: route.title.trim().slice(0, 80), answer };
    }
    if (route.action === "resume" && "id" in route && typeof route.id === "string") {
      if (recent.some(task => task.id === route.id)) return { action: "resume", id: route.id, answer };
    }
    if (route.action === "clarify" && "question" in route && typeof route.question === "string" && route.question.trim()) {
      return { action: "clarify", question: route.question.trim().slice(0, 500) };
    }
  }
  throw new Error("The router returned an invalid task decision.");
}

export async function openTaskStore(root: string) {
  const state = join(await realpath(root), ".ycode");
  await mkdir(state, { recursive: true, mode: 0o700 });
  if (await realpath(state) !== state) throw new Error("Expected a local .ycode directory.");
  const directory = join(state, "tasks");
  await mkdir(directory, { recursive: true, mode: 0o700 });
  if (await realpath(directory) !== directory) throw new Error("Expected a local .ycode/tasks directory.");
  return {
    async list(): Promise<Task[]> {
      const entries = await readdir(directory, { withFileTypes: true });
      const tasks = await Promise.all(entries
        .filter(entry => entry.isFile() && /^[a-f0-9-]{36}\.json$/.test(entry.name))
        .map(async entry => {
          const { title, goal, history, artifacts, updatedAt } = JSON.parse(await readFile(join(directory, entry.name), "utf8"));
          return { id: entry.name.slice(0, -5), title, goal, history, artifacts, updatedAt };
        }));
      return tasks.sort((a, b) => b.updatedAt - a.updatedAt);
    },
    async save(task: Task) {
      if (!/^[a-f0-9-]{36}$/.test(task.id)) throw new Error("Invalid task ID.");
      const temporary = join(directory, crypto.randomUUID() + ".tmp");
      try {
        await writeFile(temporary, JSON.stringify(task) + "\n", { flag: "wx", mode: 0o600 });
        await rename(temporary, join(directory, task.id + ".json"));
      } finally {
        await rm(temporary, { force: true });
      }
    },
  };
}
