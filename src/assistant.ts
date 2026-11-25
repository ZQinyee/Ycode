import { runTurn, type Chat, type Tools } from "./agent";
import { openTaskStore, routeTask, type Task } from "./tasks";

type TurnOptions = {
  instructions?: string;
  onProgress?(stage: "routing" | "answering"): void;
  onAnswer?(answer: string, title: string): Promise<{ note: string; artifact?: string } | undefined>;
};

export async function openAssistant(workspace: string, chat: Chat) {
  const store = await openTaskStore(workspace);
  let pending: { request: string; question: string } | undefined;

  return {
    cancelClarification() {
      pending = undefined;
    },
    async send(text: string, tools: Tools, options: TurnOptions = {}) {
      const request = pending
        ? `Original request: ${pending.request}\nClarifying question: ${pending.question}\nUser's answer: ${text}`
        : text;
      options.onProgress?.("routing");
      const tasks = await store.list();
      const route = await routeTask(request, chat, tasks, options.instructions);
      if (route.action === "clarify") {
        pending = { request, question: route.question };
        return { type: "question" as const, question: route.question };
      }
      const task: Task = route.action === "resume"
        ? tasks.find(task => task.id === route.id)!
        : { id: crypto.randomUUID(), title: route.title, goal: request, history: [], artifacts: [], updatedAt: 0 };
      let { answer } = route;
      if (answer) {
        task.history.push({ role: "user", content: request }, { role: "assistant", content: answer });
      } else {
        options.onProgress?.("answering");
        const turn = await runTurn(request, chat, tools, task.history, options.instructions);
        answer = turn.answer;
        task.history = turn.history;
      }
      const outcome = await options.onAnswer?.(answer, task.title);
      if (outcome) {
        task.history.push({ role: "system", content: outcome.note });
        if (outcome.artifact && !task.artifacts.includes(outcome.artifact)) task.artifacts.push(outcome.artifact);
      }
      task.updatedAt = Date.now();
      await store.save(task);
      pending = undefined;
      return { type: "answer" as const, answer, task };
    },
  };
}
