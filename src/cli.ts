import { realpath } from "node:fs/promises";
import { clearScreenDown, createInterface, cursorTo, moveCursor } from "node:readline";
import { runTurn, type Chat, type Tools } from "./agent";
import { openAssistant, type TurnOptions } from "./assistant";
import { codex } from "./codex";
import { ollama } from "./ollama";
import { workspaceTools } from "./tools";

const args = process.argv.slice(2);
const allowEdit = args[0] === "--edit";
if (allowEdit) args.shift();
const terminal = Boolean(process.stdin.isTTY && process.stdout.isTTY);
const animatedTerminal = terminal && process.env.TERM !== "dumb";
const terminalColor = animatedTerminal && !process.env.NO_COLOR && process.stdout.hasColors();

const instructions = [
  "Use the current directory as the workspace. Cite file paths when relevant.",
  "Use write_file to propose a new file and replace_text to propose a change to an existing file.",
  "Propose at most one edit per turn. A proposed edit still needs the user's approval.",
].join("\n");

const statusPhrases = {
  routing: ["FINDING THE RIGHT TASK", "CONNECTING THE DOTS", "LOOKING FOR A MATCH"],
  answering: ["ON IT", "PUTTING THE ANSWER TOGETHER", "STILL WORKING ON IT"],
  listing: ["SCANNING THE FOLDER"],
  reading: ["READING THE FILE"],
  editing: ["DRAFTING THE CHANGE"],
  tool: ["RUNNING A TOOL"],
} as const;

type StatusStage = keyof typeof statusPhrases;

const glowStyles = ["1;96", "96", "36", "2;36"] as const;
const queueAccents = ["1;97", "1;96", "96", "1;36"] as const;

function shimmerLabel(label: string, frame: number) {
  const center = frame % (label.length + 6) - 3;
  let styled = "";
  let previous = "";
  for (let index = 0; index < label.length; index++) {
    const distance = Math.min(Math.abs(index - center), glowStyles.length - 1);
    const style = glowStyles[distance];
    if (style !== previous) {
      styled += `\x1b[0;${style}m`;
      previous = style;
    }
    styled += label[index];
  }
  return styled + "\x1b[0m";
}

function createStatus(render: (status: string) => void) {
  const frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏";
  let timer: ReturnType<typeof setInterval> | undefined;
  let started = 0;
  let stageStarted = 0;
  let frame = 0;
  let stage: StatusStage | undefined;

  return {
    show(nextStage: StatusStage) {
      if (!animatedTerminal || stage === nextStage) return;
      stage = nextStage;
      stageStarted = performance.now();
      if (timer) return;
      started = stageStarted;
      timer = setInterval(() => {
        if (!stage) return;
        const now = performance.now();
        const phrases = statusPhrases[stage];
        const label = phrases[Math.floor((now - stageStarted) / 4000) % phrases.length]!;
        const width = Math.max(0, (process.stdout.columns || 80) - 1);
        const elapsed = `${((now - started) / 1000).toFixed(1)}S`;
        const visibleLabel = label.slice(0, Math.max(0, width - elapsed.length - 5));
        const detail = visibleLabel ? `  ${visibleLabel}  ${elapsed}` : ` ${elapsed}`;
        const glyph = frames[frame++ % frames.length];
        const line = (glyph + detail).slice(0, width);
        if (!terminalColor || !line) {
          render(line);
          return;
        }
        const text = visibleLabel ? `  ${shimmerLabel(visibleLabel, frame)}` : "";
        const time = line.slice(visibleLabel ? visibleLabel.length + 3 : 1);
        render(`\x1b[36m${glyph}\x1b[0m${text}\x1b[2m${time}\x1b[0m`);
      }, 100).unref();
    },
    clear() {
      stage = undefined;
      if (!timer) return;
      clearInterval(timer);
      timer = undefined;
      if (frame > 0) render("");
      frame = 0;
    },
  };
}

const toolStatus = new Map<string, StatusStage>([
  ["list_files", "listing"],
  ["read_file", "reading"],
  ["write_file", "editing"],
  ["replace_text", "editing"],
]);
const editPrompt = "APPLY THIS EDIT? [Y/N] ";

function createInput(lines: ReturnType<typeof createInterface>) {
  const queue: string[] = [];
  let wake: (() => void) | undefined;
  let approval: { resolve: (accepted: boolean) => void; hasDraft: boolean } | undefined;
  let closed = false;
  let status = "";
  let queueCount = 0;
  let queueChangedAt = 0;

  function render() {
    if (!terminal || closed) return;
    const now = performance.now();
    if (queueCount !== queue.length) {
      queueCount = queue.length;
      queueChangedAt = now;
    }
    let pending = "";
    if (queueCount) {
      const last = queueAccents.length - 1;
      const fade = status ? Math.min(Math.floor((now - queueChangedAt) / 120), last) : last;
      pending = terminalColor
        ? `\x1b[2mQUEUED\x1b[0m \x1b[${queueAccents[fade]}m${queueCount}\x1b[0m`
        : `QUEUED ${queueCount}`;
    }
    const divider = terminalColor ? "  \x1b[2m│\x1b[0m  " : "  │  ";
    const header = [status, pending].filter(Boolean).join(divider);
    const prompt = approval && !approval.hasDraft ? editPrompt : "YCODE> ";
    lines.setPrompt(header ? `${header}\n${prompt}` : prompt);
    lines.prompt(true);
  }

  function confirm(accepted: boolean) {
    if (!approval) return false;
    const { resolve } = approval;
    approval = undefined;
    resolve(accepted);
    render();
    return true;
  }

  lines.on("line", line => {
    const text = line.trim();
    if (approval && !approval.hasDraft && /^(?:y|n)?$/i.test(text)) {
      confirm(text.toLowerCase() === "y");
      return;
    }
    if (approval) approval.hasDraft = false;
    if (text) queue.push(text);
    wake?.();
    wake = undefined;
    render();
  });
  lines.on("close", () => {
    closed = true;
    confirm(false);
    wake?.();
    wake = undefined;
  });

  return {
    async next() {
      while (!queue.length && !closed) {
        render();
        await new Promise<void>(resolve => { wake = resolve; });
      }
      const text = queue.shift();
      render();
      return text;
    },
    ask(): Promise<boolean> {
      if (!terminal || closed) return Promise.resolve(false);
      return new Promise(resolve => {
        approval = { resolve, hasDraft: lines.line.length > 0 };
        render();
      });
    },
    rejectEdit: () => confirm(false),
    showStatus(text: string) {
      status = text;
      render();
    },
    print(text: string, write = console.log) {
      if (!terminal || closed) {
        write(text);
        return;
      }
      const rows = lines.getCursorPos().rows;
      if (rows) moveCursor(process.stdout, 0, -rows);
      cursorTo(process.stdout, 0);
      clearScreenDown(process.stdout);
      write(text);
      // Keep the row offset readline will use when it redraws the current draft.
      if (rows) process.stdout.write("\n".repeat(rows));
      render();
    },
  };
}

async function previewEdit(
  tools: ReturnType<typeof workspaceTools>,
  confirm: () => Promise<boolean>,
  print = console.log,
) {
  const edit = tools.getEdit();
  if (!edit) return;

  const preview = [`\nProposed change in ${JSON.stringify(edit.path)}`];
  if (edit.oldText !== undefined) preview.push(`- ${JSON.stringify(edit.oldText)}`);
  preview.push(`+ ${JSON.stringify(edit.newText)}`);
  print(preview.join("\n"));

  const approved = await confirm();
  if (approved) {
    await edit.apply();
    print("Edit applied.");
  }
  const outcome = approved ? "The user approved the file change and it was applied." : "The proposed file change was not applied.";
  return { note: `${outcome} Path: ${edit.path}`, artifact: approved ? edit.path : undefined };
}

async function interact(chat: Chat, root: string) {
  const assistant = await openAssistant(root, chat);
  const lines = createInterface({ input: process.stdin, output: process.stdout, terminal });
  const input = createInput(lines);
  const status = createStatus(input.showStatus);
  let active: AbortController | undefined;
  lines.on("SIGINT", () => {
    if (active) active.abort();
    else if (!input.rejectEdit()) lines.close();
  });
  if (terminal) lines.on("close", () => active?.abort());
  try {
    console.log("YCODE · /EXIT TO QUIT");
    while (true) {
      const text = await input.next();
      if (text === undefined) break;
      const command = text.toLowerCase();
      if (command === "/exit") break;
      if (command === "/cancel") {
        assistant.cancelClarification();
        continue;
      }
      const controller = new AbortController();
      active = controller;
      try {
        const tools = workspaceTools(root, allowEdit);
        const turnTools: Tools = {
          definitions: tools.definitions,
          async execute(name, args) {
            status.show(toolStatus.get(name) ?? "tool");
            try {
              return await tools.execute(name, args);
            } finally {
              status.show("answering");
            }
          },
        };
        const options: TurnOptions = {
          instructions,
          signal: controller.signal,
          onProgress: status.show,
          async onAnswer(answer, title) {
            active = undefined;
            status.clear();
            input.print(`[${title}] ${answer}`);
            return previewEdit(tools, input.ask, input.print);
          },
        };
        const resume = command === "/resume" || /^继续刚才没做完的(?:任务)?[。！!]?$/u.test(text);
        const result = resume ? await assistant.resume(turnTools, options) : await assistant.send(text, turnTools, options);
        if (!result) input.print("NO UNFINISHED REQUEST");
        else if (result.type === "question") {
          status.clear();
          input.print(result.question + " (/CANCEL TO START OVER)");
        }
      } catch (err) {
        status.clear();
        if (controller.signal.aborted) input.print("REQUEST STOPPED. TYPE /RESUME TO CONTINUE.");
        else input.print(`ycode: ${err instanceof Error ? err.message : String(err)}`, console.error);
      } finally {
        active = undefined;
      }
    }
  } finally {
    status.clear();
    lines.close();
  }
}

if (args[0] === "--help" || args[0] === "-h") {
  console.log(`Usage: ycode [--edit] [task]

With no task, open the interactive assistant and keep separate task histories.
With a task, run it once and print the answer.

--edit          Preview one file change per turn, then ask before applying it
YCODE_PROVIDER  codex (default) or ollama
YCODE_MODEL     Ollama model (default: qwen3:4b)
YCODE_BASE_URL  Ollama server (default: http://127.0.0.1:11434)`);
} else {
  try {
    const root = await realpath(process.cwd());
    const { YCODE_PROVIDER, YCODE_BASE_URL, YCODE_MODEL } = process.env;
    const provider = YCODE_PROVIDER || (YCODE_BASE_URL || YCODE_MODEL ? "ollama" : "codex");
    if (provider !== "codex" && provider !== "ollama") throw new Error("YCODE_PROVIDER must be codex or ollama.");
    const chat = provider === "codex" ? codex() : ollama(YCODE_BASE_URL || "http://127.0.0.1:11434", YCODE_MODEL || "qwen3:4b");
    if (args.length === 0) await interact(chat, root);
    else {
      const tools = workspaceTools(root, allowEdit);
      const turn = await runTurn(args.join(" "), chat, tools, [], instructions);
      console.log(turn.answer);
      await previewEdit(tools, async () =>
        terminal && prompt(editPrompt.trimEnd())?.trim().toLowerCase() === "y");
    }
  } catch (err) {
    console.error("ycode:", err instanceof Error ? err.message : String(err));
    process.exitCode = 1;
  }
}
