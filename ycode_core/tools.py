from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


MAX_FILE_SIZE = 64 * 1024


class ToolError(str):
    pass


def read_text(path: Path) -> str:
    if not path.is_file() or path.stat().st_size > MAX_FILE_SIZE:
        raise ValueError("Expected a file of at most 64 KiB.")
    with path.open("rb") as file:
        data = file.read(MAX_FILE_SIZE + 1)
    if b"\0" in data or len(data) > MAX_FILE_SIZE:
        raise ValueError("Expected a UTF-8 text file of at most 64 KiB.")
    return data.decode("utf-8")


@dataclass
class Edit:
    path: Path
    relative_path: str
    new_text: str
    old_text: str | None = None
    before: str | None = None

    def preview(self) -> str:
        lines = [f"\nProposed change in {json.dumps(self.relative_path, ensure_ascii=False)}"]
        if self.old_text is not None:
            lines.append(f"- {json.dumps(self.old_text, ensure_ascii=False)}")
        lines.append(f"+ {json.dumps(self.new_text, ensure_ascii=False)}")
        return "\n".join(lines)

    def apply(self) -> None:
        if self.before is None:
            if self.path.parent.resolve() != self.path.parent:
                raise ValueError("The destination directory changed.")
            with self.path.open("xb") as output:
                output.write(self.new_text.encode("utf-8"))
            return
        if self.path.resolve() != self.path or read_text(self.path) != self.before:
            raise ValueError("The file changed after the proposal. Run Ycode again to refresh it.")
        self.path.write_bytes(self.before.replace(self.old_text, self.new_text, 1).encode("utf-8"))


class Workspace:
    def __init__(self, root: Path, allow_edit: bool = False):
        self.root = root.resolve()
        self.edit: Edit | None = None
        specs = {
            "list_files": ("List entries and file sizes in bytes. Use . for the project root.", {}),
            "read_file": ("Read a UTF-8 text file of at most 64 KiB.", {}),
        }
        if allow_edit:
            specs.update({
                "write_file": ("Propose creating a new UTF-8 text file. Use replace_text for existing files.",
                               {"content": "The full text of the new file."}),
                "replace_text": ("Propose one exact text replacement in an existing file.",
                                 {"old_text": "Exact text copied from the file. It must occur once.",
                                  "new_text": "Replacement. Use an empty string to delete the old text."}),
            })
        self.definitions = []
        for name, (description, extra) in specs.items():
            fields = {"path": "Relative path inside the project.", **extra}
            self.definitions.append({"type": "function", "function": {
                "name": name, "description": description, "parameters": {
                    "type": "object", "required": list(fields),
                    "properties": {key: {"type": "string", "description": value} for key, value in fields.items()},
                },
            }})
        self.names = specs.keys()

    def check(self, path: Path) -> None:
        if not path.is_relative_to(self.root) or any(
            part.startswith(".") or part == "node_modules" for part in path.relative_to(self.root).parts
        ):
            raise ValueError("Path is outside the visible project.")

    def execute(self, name: str, args: dict) -> str:
        try:
            if name not in self.names:
                raise ValueError("Unknown tool.")
            target = args.get("path") if isinstance(args, dict) else None
            if not isinstance(target, str) or Path(target).is_absolute():
                raise ValueError("Expected a relative path.")
            requested = Path(os.path.abspath(self.root / target))
            self.check(requested)
            path = (requested.parent.resolve(strict=True) / requested.name
                    if name == "write_file" else requested.resolve(strict=True))
            self.check(path)
            if name == "read_file":
                return read_text(path)
            if name == "list_files":
                entries = sorted(item for item in path.iterdir()
                                 if not item.name.startswith(".") and item.name != "node_modules" and not item.is_symlink())
                rows = [f"{item.name}\t{item.stat().st_size} B" if item.is_file()
                        else item.name + ("/" if item.is_dir() else "") for item in entries[:200]]
                if len(entries) > 200:
                    rows.append("[listing truncated]")
                return "\n".join(rows) or "[empty directory]"
            return self.propose(name, path, args)
        except (OSError, UnicodeError, ValueError) as error:
            return ToolError(f"Tool error: {error}")

    def propose(self, name: str, path: Path, args: dict) -> str:
        if self.edit:
            raise ValueError("An edit is already pending. Finish the task so the user can review it.")
        relative_path = path.relative_to(self.root).as_posix()
        if name == "write_file":
            if path.exists() or path.is_symlink():
                raise ValueError("File already exists. Use replace_text to edit it.")
            new = args.get("content")
            if not isinstance(new, str) or "\0" in new or len(new.encode("utf-8")) > MAX_FILE_SIZE:
                raise ValueError("Expected UTF-8 text of at most 64 KiB.")
            self.edit = Edit(path, relative_path, new)
            return f"New file {relative_path} prepared for review."

        old, new = args.get("old_text"), args.get("new_text")
        if not isinstance(old, str) or not old or not isinstance(new, str) or old == new:
            raise ValueError("Expected nonempty old_text and a different string new_text.")
        before = read_text(path)
        start = before.find(old)
        if start < 0 or before.find(old, start + 1) != -1:
            raise ValueError("old_text must match exactly once.")
        if "\0" in new or len(before.replace(old, new, 1).encode("utf-8")) > MAX_FILE_SIZE:
            raise ValueError("The edited file must be UTF-8 text of at most 64 KiB.")
        self.edit = Edit(path, relative_path, new, old, before)
        return "Replacement prepared for review."
