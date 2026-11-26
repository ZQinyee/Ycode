import { realpath } from "node:fs/promises";
import { createInterface } from "node:readline";
import { runTurn, type Chat } from "./agent";
import { openAssistant } from "./assistant";
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

function createStatus() {
  const enabled = terminal && process.env.TERM !== "dumb";
  const color = enabled && process.stdout.hasColors();
  const frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏";
  const clearLine = "\r\x1b[2K";
  let timer: ReturnType<typeof setInterval> | undefined;
  let started = 0;
  let frame = 0;
  let label = "";

  return {
    show(stage: string) {
      if (!enabled) return;
      label = stage;
      if (timer) return;
      started = performance.now();
      timer = setInterval(() => {
        const width = Math.max(0, (process.stdout.columns || 80) - 1);
        const elapsed = `${((performance.now() - started) / 1000).toFixed(1)}s`;
        const labelWidth = width - elapsed.length - 5;
        const detail = labelWidth > 0 ? `  ${label.slice(0, labelWidth)}  ${elapsed}` : ` ${elapsed}`;
        const glyph = frames[frame++ % frames.length];
        const line = (glyph + detail).slice(0, width);
        process.stdout.write(clearLine + (color && line ? `\x1b[36m${glyph}\x1b[0m${line.slice(1)}` : line));
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

const toolStatus = new Map([
  ["list_files", "Listing files"],
  ["read_file", "Reading file"],
  ["write_file", "Preparing edit"],
  ["replace_text", "Preparing edit"],
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
      try {
        const tools = workspaceTools(root, allowEdit);
        const result = await assistant.send(text, {
          definitions: tools.definitions,
          async execute(name, args) {
            status.show(toolStatus.get(name) ?? "Running tool");
            try {
              return await tools.execute(name, args);
            } finally {
              status.show("Waiting for model");
            }
          },
        }, {
          instructions,
          onProgress(stage) {
            status.show(stage === "routing" ? "Finding task" : "Waiting for model");
          },
          async onAnswer(answer, title) {
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
        });
        if (result.type === "question") {
          status.clear();
          console.log(result.question + " (/CANCEL TO START OVER)");
        }
      } catch (err) {
        status.clear();
        console.error("ycode:", err instanceof Error ? err.message : String(err));
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
