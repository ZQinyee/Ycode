import { expect, test } from "bun:test";
import { mkdtemp, realpath, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { runAgent, type Message } from "../src/agent";
import { workspaceTools } from "../src/tools";

test("CLI sends tool results back to the model before printing the answer", async () => {
  const root = await mkdtemp(join(tmpdir(), "ycode-"));
  const marker = crypto.randomUUID();
  const server = Bun.serve({
    hostname: "127.0.0.1", port: 0,
    async fetch(request) {
      const body = await request.json();
      const last: Message = body.messages.at(-1);
      if (last.role === "user") {
        return Response.json({ message: { role: "assistant", content: "", tool_calls: [
          { function: { name: "read_file", arguments: { path: "note.txt" } } },
        ] } });
      }
      if (last.role !== "tool" || last.tool_name !== "read_file") return new Response("Missing tool result", { status: 400 });
      return Response.json({ message: { role: "assistant", content: last.content } });
    },
  });
  try {
    await writeFile(join(root, "note.txt"), marker);
    const child = Bun.spawn([process.execPath, resolve(import.meta.dir, "../src/cli.ts"), "Read note.txt"], {
      cwd: root, env: { ...process.env, YCODE_BASE_URL: server.url.origin }, stdout: "pipe", stderr: "pipe",
    });
    const [output, error, code] = await Promise.all([
      new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited,
    ]);
    expect({ output: output.trim(), error, code }).toEqual({ output: marker, error: "", code: 0 });
  } finally {
    server.stop(true);
    await rm(root, { recursive: true, force: true });
  }
});

test("file tools reject a symlink pointing outside the workspace", async () => {
  const root = await realpath(await mkdtemp(join(tmpdir(), "ycode-")));
  try {
    await symlink(tmpdir(), join(root, "outside"));
    expect(await workspaceTools(root).execute("list_files", { path: "outside" })).toContain("outside the visible project");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("a repeating model stops at the tool budget", async () => {
  const chat = async () => ({
    role: "assistant" as const, content: "", tool_calls: [{ function: { name: "unknown", arguments: {} } }],
  });
  await expect(runAgent("inspect", chat, workspaceTools("."))).rejects.toThrow("limit of 12");
});
