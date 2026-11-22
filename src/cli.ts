import { realpath } from "node:fs/promises";
import { runAgent } from "./agent";
import { ollama } from "./ollama";
import { workspaceTools } from "./tools";

const args = process.argv.slice(2);
if (args.length === 0 || args[0] === "--help" || args[0] === "-h") {
  console.log('Usage: ycode "question about the current project"\n\nYCODE_MODEL     Ollama model (default: qwen3:4b)\nYCODE_BASE_URL  Ollama server (default: http://127.0.0.1:11434)');
} else {
  const base = process.env.YCODE_BASE_URL || "http://127.0.0.1:11434";
  try {
    const chat = ollama(base, process.env.YCODE_MODEL || "qwen3:4b");
    console.log(await runAgent(args.join(" "), chat, workspaceTools(await realpath(process.cwd()))));
  } catch (err) {
    console.error("ycode:", err instanceof Error ? err.message : String(err));
    process.exitCode = 1;
  }
}
