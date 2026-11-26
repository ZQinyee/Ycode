import { realpath } from "node:fs/promises";
import { createInterface } from "node:readline";
import { runTurn, type Chat, type Tools } from "./agent";
import { openAssistant, type TurnOptions } from "./assistant";
import { codex } from "./codex";
import { ollama } from "./ollama";
import { workspaceTools } from "./tools";

const args = process.argv.slice(2);
const allowEdit = args[0] === "--edit";
if (allowEdit) args.shift();
const terminal = Boolean(process.stdin.isTTY && process.stdout.isTTY);

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

function createStatus() {
  const enabled = terminal && process.env.TERM !== "dumb";
  const color = enabled && !process.env.NO_COLOR && process.stdout.hasColors();
  const frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏";
  const clearLine = "\r\x1b[2K";
  let timer: ReturnType<typeof setInterval> | undefined;
  let started = 0;
  let stageStarted = 0;
  let frame = 0;
  let stage: StatusStage = "routing";

  return {
    show(nextStage: StatusStage) {
      if (!enabled) return;
      const now = performance.now();
      if (stage !== nextStage || !timer) {
        stage = nextStage;
        stageStarted = now;
      }
      if (timer) return;
      started = now;
      timer = setInterval(() => {
        const now = performance.now();
        const phrases = statusPhrases[stage];
        const label = phrases[Math.floor((now - stageStarted) / 4000) % phrases.length]!;
        const width = Math.max(0, (process.stdout.columns || 80) - 1);
        const elapsed = `${((now - started) / 1000).toFixed(1)}S`;
        const visibleLabel = label.slice(0, Math.max(0, width - elapsed.length - 5));
        const detail = visibleLabel ? `  ${visibleLabel}  ${elapsed}` : ` ${elapsed}`;
        const glyph = frames[frame++ % frames.length];
        const line = (glyph + detail).slice(0, width);
        if (!color || !line) {
          process.stdout.write(clearLine + line);
          return;
        }
        const text = visibleLabel ? `  ${shimmerLabel(visibleLabel, frame)}` : "";
        const time = line.slice(visibleLabel ? visibleLabel.length + 3 : 1);
        process.stdout.write(`${clearLine}\x1b[36m${glyph}\x1b[0m${text}\x1b[2m${time}\x1b[0m`);
      }, 100).unref();
    },
    clear() {
      if (!timer) return;
      clearInterval(timer);
      timer = undefined;
      if (frame > 0) process.stdout.write(clearLine);
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

async function previewEdit(
  tools: ReturnType<typeof workspaceTools>,
  confirm: () => Promise<boolean>,
) {
  const edit = tools.getEdit();
  if (!edit) return;

  console.log(`\nProposed change in ${JSON.stringify(edit.path)}`);
  if (edit.oldText !== undefined) console.log("-", JSON.stringify(edit.oldText));
  console.log("+", JSON.stringify(edit.newText));

  const approved = await confirm();
  if (approved) {
    await edit.apply();
    console.log("Edit applied.");
  }
  const outcome = approved ? "The user approved the file change and it was applied." : "The proposed file change was not applied.";
  return { note: `${outcome} Path: ${edit.path}`, artifact: approved ? edit.path : undefined };
}

async function interact(chat: Chat, root: string) {
  const assistant = await openAssistant(root, chat);
  const lines = createInterface({ input: process.stdin, output: process.stdout, terminal });
  const input = lines[Symbol.asyncIterator]();
  const status = createStatus();
  let active: AbortController | undefined;
  lines.on("SIGINT", () => {
    if (active) active.abort();
    else lines.close();
  });
  if (terminal) lines.on("close", () => active?.abort());
  try {
    console.log("YCODE · /EXIT TO QUIT");
    while (true) {
      if (terminal) {
        lines.setPrompt("YCODE> ");
        lines.prompt();
      }
      const next = await input.next();
      if (next.done) break;
      const text = next.value.trim();
      const command = text.toLowerCase();
      if (command === "/exit") break;
      if (!text) continue;
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
            console.log(`[${title}] ${answer}`);
            return previewEdit(tools, async () => {
              if (!terminal) return false;
              lines.setPrompt(editPrompt);
              lines.prompt();
              const answer = await input.next();
              return !answer.done && answer.value.trim().toLowerCase() === "y";
            });
          },
        };
        const resume = command === "/resume" || /^继续刚才没做完的(?:任务)?[。！!]?$/u.test(text);
        const result = resume ? await assistant.resume(turnTools, options) : await assistant.send(text, turnTools, options);
        if (!result) console.log("NO UNFINISHED REQUEST");
        else if (result.type === "question") {
          status.clear();
          console.log(result.question + " (/CANCEL TO START OVER)");
        }
      } catch (err) {
        status.clear();
        if (controller.signal.aborted) console.log("REQUEST STOPPED. TYPE /RESUME TO CONTINUE.");
        else console.error("ycode:", err instanceof Error ? err.message : String(err));
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
