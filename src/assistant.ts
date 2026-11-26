import { runTurn, type Chat, type Tools } from "./agent";
import { openTaskStore, routeKnownTask, routeTask, type Task } from "./tasks";

export type TurnOptions = {
  instructions?: string;
  signal?: AbortSignal;
  onProgress?(stage: "routing" | "answering"): void;
  onAnswer?(answer: string, title: string): Promise<{ note: string; artifact?: string } | undefined>;
};

export async function openAssistant(workspace: string, chat: Chat) {
  const store = await openTaskStore(workspace);
  let pending: { request: string; question: string } | undefined;

  async function execute(
    turn: Awaited<ReturnType<typeof store.journal.begin>>, tools: Tools, options: TurnOptions, useLocalRoute = true,
  ) {
    const { request, task: savedTask } = turn.record;
    const { signal } = options;
    try {
      signal?.throwIfAborted();
      const tasks = await store.list();
      const known = savedTask
        ? { action: "resume" as const, id: savedTask.id, answer: undefined }
        : useLocalRoute ? routeKnownTask(request, tasks) : undefined;
      signal?.throwIfAborted();
      if (!known) options.onProgress?.("routing");
      const route = known ?? await routeTask(request, chat, tasks, options.instructions, signal);
      signal?.throwIfAborted();
      if (route.action === "clarify") {
        await turn.complete();
        pending = { request, question: route.question };
        return { type: "question" as const, question: route.question };
      }
      const existing = route.action === "resume" ? tasks.find(task => task.id === route.id) : undefined;
      const task: Task = existing ?? {
        id: savedTask?.id ?? crypto.randomUUID(),
        title: route.action === "new" ? route.title : savedTask!.title,
        goal: request,
        history: [],
        artifacts: [],
        updatedAt: 0,
      };
      if (!savedTask) await turn.selectTask(task);
      signal?.throwIfAborted();
      let { answer } = route;
      if (answer) {
        task.history.push({ role: "user", content: request }, { role: "assistant", content: answer });
      } else {
        options.onProgress?.("answering");
        const reply = await runTurn(request, chat, tools, task.history, options.instructions, signal);
        answer = reply.answer;
        task.history = reply.history;
      }
      signal?.throwIfAborted();
      // The answer is ready. Retire the request before any approved edit can run.
      await turn.complete();
      const outcome = await options.onAnswer?.(answer, task.title);
      if (outcome) {
        task.history.push({ role: "system", content: outcome.note });
        if (outcome.artifact && !task.artifacts.includes(outcome.artifact)) task.artifacts.push(outcome.artifact);
      }
      task.updatedAt = Date.now();
      await store.save(task);
      return { type: "answer" as const, answer, task };
    } finally {
      await turn.release();
    }
  }

  return {
    cancelClarification() {
      pending = undefined;
    },
    async send(text: string, tools: Tools, options: TurnOptions = {}) {
      const request = pending
        ? `Original request: ${pending.request}\nClarifying question: ${pending.question}\nUser's answer: ${text}`
        : text;
      const useLocalRoute = !pending;
      const turn = await store.journal.begin(request);
      pending = undefined;
      return execute(turn, tools, options, useLocalRoute);
    },
    async resume(tools: Tools, options: TurnOptions = {}) {
      const turn = await store.journal.resume();
      if (!turn) return;
      pending = undefined;
      return execute(turn, tools, options);
    },
  };
}
