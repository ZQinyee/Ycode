import { readdir, realpath, stat, readFile } from "node:fs/promises";
import { isAbsolute, relative, resolve, sep } from "node:path";

type Tool = {
  description: string;
  run(path: string): Promise<string>;
};

export function workspaceTools(root: string) {
  const tools: Record<string, Tool> = {
    list_files: {
      description: "List entries in a directory. Use . for the project root.",
      async run(path) {
        const entries = (await readdir(path, { withFileTypes: true }))
          .filter(entry => !entry.name.startsWith(".") && entry.name !== "node_modules")
          .map(entry => entry.name + (entry.isDirectory() ? "/" : ""))
          .sort();
        const shown = entries.slice(0, 200);
        if (entries.length > shown.length) shown.push("[listing truncated]");
        return shown.join("\n") || "[empty directory]";
      },
    },
    read_file: {
      description: "Read a UTF-8 text file (up to 64 KiB).",
      async run(path) {
        const info = await stat(path);
        if (!info.isFile() || info.size > 65536) throw new Error("Expected a file of at most 64 KiB.");
        const bytes = await readFile(path);
        if (bytes.includes(0)) throw new Error("Expected a text file.");
        return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
      },
    },
  };

  return {
    definitions: Object.entries(tools).map(([name, tool]) => ({
      type: "function",
      function: {
        name, description: tool.description,
        parameters: {
          type: "object",
          properties: { path: { type: "string", description: "Relative path inside the project." } },
          required: ["path"],
        },
      },
    })),
    async execute(name: string, args: unknown) {
      try {
        if (!Object.hasOwn(tools, name)) throw new Error("Unknown tool.");
        if (!args || typeof args !== "object" || !("path" in args) || typeof args.path !== "string" || isAbsolute(args.path))
          throw new Error("Expected a relative path.");
        const path = await realpath(resolve(root, args.path));
        const local = relative(root, path);
        if (isAbsolute(local) || local.split(sep).some(part => part.startsWith(".") || part === "node_modules"))
          throw new Error("Path is outside the visible project.");
        return await tools[name].run(path);
      } catch (err) {
        return "Tool error: " + (err instanceof Error ? err.message : String(err));
      }
    },
  };
}
