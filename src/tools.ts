import { readdir, realpath, stat, readFile, writeFile } from "node:fs/promises";
import { basename, dirname, isAbsolute, relative, resolve, sep } from "node:path";
import type { ToolDefinition } from "./agent";

type Tool = {
  description: string;
  extra?: Record<string, { type: "string"; description: string }>;
  run(path: string, args: object): Promise<string>;
};

const maxFileSize = 64 * 1024;

async function readText(path: string) {
  const info = await stat(path);
  if (!info.isFile() || info.size > maxFileSize) throw new Error("Expected a file of at most 64 KiB.");
  const bytes = await readFile(path);
  if (bytes.includes(0)) throw new Error("Expected a text file.");
  return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
}

type Edit = {
  path: string;
  oldText?: string;
  newText: string;
  apply(): Promise<void>;
};

export function workspaceTools(root: string, allowEdit = false) {
  let edit: Edit | undefined;

  function checkPath(path: string) {
    const local = relative(root, path);
    if (isAbsolute(local) || local.split(sep).some(part => part.startsWith(".") || part === "node_modules")) {
      throw new Error("Path is outside the visible project.");
    }
  }

  const tools: Record<string, Tool> = {
    list_files: {
      description: "List entries and file sizes in bytes in a directory. Use . for the project root.",
      async run(path) {
        const entries = (await readdir(path, { withFileTypes: true }))
          .filter(entry => !entry.name.startsWith(".") && entry.name !== "node_modules" && !entry.isSymbolicLink())
          .sort((a, b) => a.name.localeCompare(b.name));
        const shown = await Promise.all(entries.slice(0, 200).map(async entry => entry.isFile()
          ? `${entry.name}\t${(await stat(resolve(path, entry.name))).size} B`
          : entry.name + (entry.isDirectory() ? "/" : "")));
        if (entries.length > shown.length) shown.push("[listing truncated]");
        return shown.join("\n") || "[empty directory]";
      },
    },
    read_file: {
      description: "Read a UTF-8 text file (up to 64 KiB).",
      run: readText,
    },
  };
  if (allowEdit) {
    tools.write_file = {
      description: "Propose creating a new UTF-8 text file for the user to review. Use replace_text for existing files.",
      extra: { content: { type: "string", description: "The full text of the new file." } },
      async run(path, args) {
        if (edit) throw new Error("An edit is already pending. Finish the task so the user can review it.");
        if (!("content" in args) || typeof args.content !== "string" ||
            args.content.includes("\0") || Buffer.byteLength(args.content) > maxFileSize) {
          throw new Error("Expected UTF-8 text of at most 64 KiB.");
        }
        const newText = args.content;
        edit = {
          path: relative(root, path),
          newText,
          async apply() {
            if (await realpath(dirname(path)) !== dirname(path)) throw new Error("The destination directory changed.");
            await writeFile(path, newText, { flag: "wx" });
          },
        };
        return `New file ${edit.path} prepared for review. The file has not been created yet.`;
      },
    };
    tools.replace_text = {
      description: "Propose one exact text replacement in an existing file for the user to review.",
      extra: {
        old_text: { type: "string", description: "Exact text copied from the file. It must occur once." },
        new_text: { type: "string", description: "Replacement text. Use an empty string to delete the old text." },
      },
      async run(path, args) {
        if (edit) throw new Error("An edit is already pending. Finish the task so the user can review it.");
        if (!("old_text" in args) || typeof args.old_text !== "string" || !args.old_text ||
            !("new_text" in args) || typeof args.new_text !== "string") {
          throw new Error("Expected nonempty old_text and a string new_text.");
        }
        const oldText = args.old_text;
        const newText = args.new_text;
        const before = await readText(path);
        const at = before.indexOf(oldText);
        if (at < 0 || before.indexOf(oldText, at + 1) >= 0) throw new Error("old_text must match exactly once.");
        if (oldText === newText) throw new Error("The proposed replacement makes no change.");
        const after = before.slice(0, at) + newText + before.slice(at + oldText.length);
        if (newText.includes("\0") || Buffer.byteLength(after) > maxFileSize) {
          throw new Error("The edited file must be text and at most 64 KiB.");
        }
        edit = {
          path: relative(root, path),
          oldText,
          newText,
          async apply() {
            if (await realpath(path) !== path || await readText(path) !== before) {
              throw new Error("The file changed after the proposal. Run Ycode again to refresh it.");
            }
            await writeFile(path, after);
          },
        };
        return "Replacement prepared for review. The file has not been changed yet.";
      },
    };
  }

  return {
    getEdit: () => edit,
    definitions: Object.entries(tools).map(([name, tool]): ToolDefinition => ({
      type: "function",
      function: {
        name, description: tool.description,
        parameters: {
          type: "object",
          properties: {
            path: { type: "string", description: "Relative path inside the project." },
            ...tool.extra,
          },
          required: ["path", ...Object.keys(tool.extra ?? {})],
        },
      },
    })),
    async execute(name: string, args: unknown) {
      try {
        if (!Object.hasOwn(tools, name)) throw new Error("Unknown tool.");
        if (!args || typeof args !== "object" || !("path" in args) || typeof args.path !== "string" || isAbsolute(args.path))
          throw new Error("Expected a relative path.");
        const requested = resolve(root, args.path);
        checkPath(requested);
        const path = name === "write_file"
          ? resolve(await realpath(dirname(requested)), basename(requested))
          : await realpath(requested);
        checkPath(path);
        if (name === "write_file") {
          const exists = await stat(path).then(() => true, err => {
            if (err.code === "ENOENT") return false;
            throw err;
          });
          if (exists) throw new Error("File already exists. Use replace_text to edit it.");
        }
        return await tools[name].run(path, args);
      } catch (err) {
        return "Tool error: " + (err instanceof Error ? err.message : String(err));
      }
    },
  };
}
