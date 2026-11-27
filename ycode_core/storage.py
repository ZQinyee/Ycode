from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


TASK_FILE = re.compile(r"[a-f0-9-]{36}\.json")
QUEUED_FILE = re.compile(r"\d{13}-[a-f0-9-]{36}\.queued\.json")
TURN_FILE = re.compile(r"\d{13}-[a-f0-9-]{36}(?:\.([1-9]\d*))?\.json")


def local_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, exist_ok=True)
    if path.resolve() != path:
        raise ValueError(f"Expected a local directory at {path}.")
    return path


def save_json(path: Path, data: dict) -> None:
    temporary = path.with_name(f"{uuid4()}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False)
            output.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def timestamp() -> int:
    return time.time_ns() // 1_000_000


def process_running(pid: int) -> bool:
    if os.name == "nt":
        # On Windows os.kill(pid, 0) terminates the process.
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: PID is gone.
        try:
            return kernel.WaitForSingleObject(handle, 0) != 0  # WAIT_OBJECT_0: process exited.
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class Turn:
    path: Path
    record: dict

    def select(self, task: dict) -> None:
        self.record["task"] = {key: task[key] for key in ("id", "title")}
        save_json(self.path, self.record)

    def complete(self) -> None:
        self.path.unlink(missing_ok=True)

    def release(self) -> None:
        available = self.path.with_name(self.path.name.replace(f".{os.getpid()}.json", ".json"))
        try:
            self.path.replace(available)
        except FileNotFoundError:
            pass  # A completed turn already removed the file.


class Store:
    def __init__(self, root: Path):
        self.state = local_directory(root.resolve() / ".ycode")
        self.tasks = local_directory(self.state / "tasks")
        self.pending = local_directory(self.state / "pending")
        self.last_queued_at = 0

    def history(self) -> list[dict]:
        tasks = []
        for path in self.tasks.iterdir():
            if not TASK_FILE.fullmatch(path.name) or path.is_symlink() or not path.is_file():
                continue
            try:
                tasks.append({**read_json(path), "id": path.stem})
            except FileNotFoundError:
                continue
        return sorted(tasks, key=lambda task: task["updatedAt"], reverse=True)

    def save_task(self, task: dict) -> None:
        name = task["id"] + ".json"
        if not TASK_FILE.fullmatch(name):
            raise ValueError("Invalid task ID.")
        save_json(self.tasks / name, task)

    def enqueue(self, request: str, new_task: bool = False) -> dict:
        self.last_queued_at = max(timestamp(), self.last_queued_at + 1)
        name = f"{self.last_queued_at}-{uuid4()}.queued.json"
        record = {"request": request}
        if new_task:
            record["newTask"] = True
        save_json(self.pending / name, record)
        return {**record, "id": name}

    def queued(self) -> list[dict]:
        waiting = []
        for path in sorted(self.pending.iterdir()):
            if not QUEUED_FILE.fullmatch(path.name) or path.is_symlink() or not path.is_file():
                continue
            try:
                waiting.append({**read_json(path), "id": path.name})
            except FileNotFoundError:
                continue
        return waiting

    def claim(self, name: str) -> Turn | None:
        if not (QUEUED_FILE.fullmatch(name) or TURN_FILE.fullmatch(name)):
            raise ValueError("Invalid pending request ID.")
        active = self.pending / f"{timestamp()}-{uuid4()}.{os.getpid()}.json"
        try:
            # A competing CLI can only move the original file once.
            (self.pending / name).replace(active)
        except FileNotFoundError:
            return None
        try:
            return Turn(active, read_json(active))
        except (OSError, ValueError):
            active.replace(self.pending / name)
            raise

    def resume(self) -> Turn | None:
        for path in sorted(self.pending.iterdir(), reverse=True):
            match = TURN_FILE.fullmatch(path.name)
            if not match or path.is_symlink() or not path.is_file():
                continue
            if match[1] and process_running(int(match[1])):
                continue
            turn = self.claim(path.name)
            if turn:
                return turn
        return None
