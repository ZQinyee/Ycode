from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryFile
from typing import Awaitable, Callable

from .jobs import stop_process
from .models import codex_environment, codex_executable
from .trace import Trace


Approval = Callable[[str, str], Awaitable[bool]]


@dataclass
class CodexResult:
    answer: str
    thread_id: str
    files: list[str]


class Rpc:
    def __init__(self, process: asyncio.subprocess.Process, errors):
        self.process = process
        self.errors = errors
        self.next_id = 0
        self.events: list[dict] = []

    async def send(self, message: dict) -> None:
        self.process.stdin.write(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
        await self.process.stdin.drain()

    async def receive(self) -> dict:
        line = await self.process.stdout.readline()
        if not line:
            length = self.errors.seek(0, os.SEEK_END)
            self.errors.seek(max(0, length - 1200))
            detail = self.errors.read().decode(errors="replace").strip()
            raise RuntimeError(f"Codex app-server closed unexpectedly. {detail}".strip())
        return json.loads(line)

    async def call(self, method: str, params: dict) -> dict:
        self.next_id += 1
        request_id = self.next_id
        await self.send({"id": request_id, "method": method, "params": params})
        while True:
            message = await asyncio.wait_for(self.receive(), 30)
            if "method" in message or message.get("id") != request_id:
                self.events.append(message)
                continue
            if "error" in message:
                raise RuntimeError(f"Codex {method} failed: {message['error'].get('message', 'Unknown error')}")
            return message["result"]


@asynccontextmanager
async def connect(root: Path):
    with TemporaryFile() as errors:
        process = await asyncio.create_subprocess_exec(
            codex_executable(), "app-server", "--listen", "stdio://", cwd=root, env=codex_environment(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=errors,
            start_new_session=os.name == "posix", limit=16 * 1024 * 1024,
        )
        rpc = Rpc(process, errors)
        try:
            await rpc.call("initialize", {"clientInfo": {"name": "ycode", "title": "Ycode", "version": "0.1.0"}})
            await rpc.send({"method": "initialized", "params": {}})
            yield rpc
        finally:
            process.stdin.close()
            drain = asyncio.create_task(process.stdout.read())
            try:
                await asyncio.wait_for(process.wait(), 2)
            except asyncio.TimeoutError:
                await stop_process(process, group=True)
            finally:
                await drain


async def respond(rpc: Rpc, event: dict, params: dict, items: dict, approve: Approval | None, trace: Trace):
    method = event["method"]
    item = items.get(params.get("itemId"), {})
    reason = params.get("reason") or ""
    diff = ""
    available = params.get("availableDecisions")
    can_accept = available is None or "accept" in available
    if method == "item/commandExecution/requestApproval":
        network = params.get("networkApprovalContext")
        if network:
            detail = f"Network access to {network.get('host')} ({network.get('protocol')})"
        else:
            command = params.get("command") or item.get("command")
            cwd = params.get("cwd") or item.get("cwd")
            detail = f"Command: {command}\nDirectory: {cwd}"
            can_accept = can_accept and bool(command)
        if params.get("additionalPermissions"):
            detail += "\nRequested access\n" + json.dumps(params["additionalPermissions"], ensure_ascii=False, indent=2)
    elif method == "item/fileChange/requestApproval":
        changes = item.get("changes", [])
        detail = "File changes\n" + "\n".join(change["path"] for change in changes)
        diff = "\n".join(change.get("diff", "") for change in changes)
        can_accept = can_accept and (bool(changes) or bool(params.get("grantRoot")))
        if params.get("grantRoot"):
            detail += f"\nWrite access requested for {params['grantRoot']}"
    elif method == "item/permissions/requestApproval":
        detail = "Requested access\n" + json.dumps(params.get("permissions", {}), ensure_ascii=False, indent=2)
    else:
        await rpc.send({"id": event["id"], "error": {"code": -32601, "message": "Unsupported Ycode approval request"}})
        return
    accepted = bool(approve and can_accept and await approve(f"{detail}\n{reason}".rstrip(), diff))
    result = {"decision": "accept" if accepted else "decline"}
    if method == "item/permissions/requestApproval":
        result = {"permissions": params.get("permissions", {}) if accepted else {}, "scope": "turn"}
    trace.emit("codex.approval", method=method, item_id=params.get("itemId"), accepted=accepted)
    await rpc.send({"id": event["id"], "result": result})


def completed_result(root: Path, thread_id: str, items: list[dict]) -> CodexResult:
    messages = [item for item in items if item.get("type") == "agentMessage" and item.get("text")]
    final = next((item["text"] for item in reversed(messages) if item.get("phase") == "final_answer"), None)
    files = set()
    for item in items:
        if item.get("type") == "fileChange" and item.get("status") == "completed":
            for change in item.get("changes", []):
                path = Path(os.path.abspath(root / change["path"]))
                if path.is_relative_to(root):
                    files.add(path.relative_to(root).as_posix())
    return CodexResult(final or (messages[-1]["text"] if messages else "Codex completed the task."), thread_id, sorted(files))


class CodexAgent:
    def __init__(self, root: Path, allow_edit: bool, approve: Approval | None = None):
        self.root = root
        self.approve = approve if allow_edit else None

    async def run(self, request: str, trace: Trace, progress: Callable[[str], None], *,
                  thread_id: str | None = None, interrupted_turn: str | None = None,
                  checkpoint: Callable[[str, str | None], None] = lambda *_: None, ephemeral: bool = False) -> CodexResult:
        options = {"cwd": str(self.root), "sandbox": "workspace-write" if self.approve else "read-only",
                   "approvalPolicy": "on-request" if self.approve else "never"}
        progress("answering")
        async with connect(self.root) as rpc:
            if thread_id:
                result = await rpc.call("thread/resume", {
                    **options, "threadId": thread_id, "excludeTurns": not interrupted_turn,
                })
            else:
                result = await rpc.call("thread/start", {**options, "ephemeral": ephemeral})
            thread = result["thread"]
            thread_id = thread["id"]
            if interrupted_turn:
                previous = next((turn for turn in thread.get("turns", []) if turn["id"] == interrupted_turn), None)
                if previous and previous.get("status") == "completed":
                    return completed_result(self.root, thread_id, previous["items"])
                request = ("Continue the interrupted request in this session. Verify the current workspace and previous "
                           "actions before making changes. Reuse work that already completed.\n\n" + request)
            # Save the native thread before asking it to execute anything.
            checkpoint(thread_id, None)
            rpc.events.clear()
            turn_id = None
            try:
                started = await rpc.call("turn/start", {
                    "threadId": thread_id, "input": [{"type": "text", "text": request}],
                })
                turn_id = started["turn"]["id"]
                checkpoint(thread_id, turn_id)
                trace.emit("codex.turn.started", thread_id=thread_id, turn_id=turn_id)
                return await self.finish_turn(rpc, thread_id, turn_id, trace, progress)
            except asyncio.CancelledError:
                if turn_id:
                    with suppress(OSError, asyncio.TimeoutError):
                        await asyncio.wait_for(rpc.send({
                            "id": rpc.next_id + 1, "method": "turn/interrupt",
                            "params": {"threadId": thread_id, "turnId": turn_id},
                        }), 1)
                raise

    async def finish_turn(self, rpc: Rpc, thread_id: str, turn_id: str, trace: Trace,
                          progress: Callable[[str], None]) -> CodexResult:
        items = {}
        completed = []
        pending = iter(rpc.events)
        while True:
            event = next(pending, None) or await rpc.receive()
            method, params = event.get("method"), event.get("params", {})
            if params.get("threadId", thread_id) != thread_id:
                continue
            if params.get("turnId", turn_id) != turn_id:
                continue
            if method in ("item/started", "item/completed"):
                item = params["item"]
                items[item["id"]] = item
                kind = item["type"]
                if kind in ("commandExecution", "fileChange"):
                    fields = {key: item[key] for key in ("command", "status", "exitCode", "durationMs") if key in item}
                    trace.emit("codex." + method.replace("/", "."), item_id=item["id"], kind=kind, **fields)
                    progress("code_command" if kind == "commandExecution" else "code_edit")
                if method == "item/completed":
                    completed.append(item)
                    progress("answering")
            elif method and "id" in event:
                await respond(rpc, event, params, items, self.approve, trace)
            elif method == "thread/tokenUsage/updated":
                trace.emit("codex.usage", usage=params.get("tokenUsage"))
            elif method == "turn/completed" and params["turn"]["id"] == turn_id:
                turn = params["turn"]
                status = turn["status"]
                trace.emit("codex.turn.finished", thread_id=thread_id, turn_id=turn_id, status=status)
                if status != "completed":
                    detail = (turn.get("error") or {}).get("message", status)
                    raise RuntimeError(f"Codex turn ended: {detail}")
                return completed_result(self.root, thread_id, completed or turn.get("items", []))
