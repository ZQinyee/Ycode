from __future__ import annotations

import argparse
import asyncio
import os
import re
import signal
import sys
import time
from collections import deque
from hashlib import sha256
from pathlib import Path
from typing import Awaitable, Callable

from .agent import Assistant, review_change, run_turn
from .codex_agent import CodexAgent
from .git import apply_push, prepare_push
from .jobs import Jobs
from .memory import context_budget
from .models import Chat, configured_chat, configured_provider
from .storage import Store
from .tools import Edit, Workspace
from .trace import Trace


STATUS = {
    "routing": ("FINDING THE RIGHT TASK", "CONNECTING THE DOTS", "LOOKING FOR A MATCH"),
    "answering": ("ON IT", "PUTTING THE ANSWER TOGETHER", "STILL WORKING ON IT"),
    "listing": ("SCANNING THE FOLDER",), "reading": ("READING THE FILE",),
    "editing": ("DRAFTING THE CHANGE",), "tool": ("RUNNING A TOOL",),
    "git": ("CHECKING GIT CHANGES",), "pushing": ("PUSHING GIT COMMIT",),
    "code_command": ("CODEX IS RUNNING A COMMAND",), "code_edit": ("CODEX IS EDITING FILES",),
}
FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
QUEUE_STYLES = ("class:queue0", "class:queue1", "class:queue2", "class:queue3")
EDIT_PROMPT = "APPLY THIS EDIT? [Y/N] "
PUSH_PROMPT = "COMMIT AND PUSH? [Y/N] "
CODEX_PROMPT = "ALLOW CODEX ACTION? [Y/N] "


async def show_preview(answer: str, preview: str = "", *, title: str = "", markdown: bool = False) -> None:
    if not sys.stdout.isatty():
        print(f"[{title}] {answer}" if title else answer)
        if preview:
            print(preview)
        return

    from prompt_toolkit.application import get_app_session, in_terminal
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.output import ColorDepth
    from prompt_toolkit.renderer import print_formatted_text
    from prompt_toolkit.styles import Style
    from prompt_toolkit.utils import get_cwidth

    output = get_app_session().output
    depth = output.get_default_color_depth()
    columns = output.get_size().columns
    removed, added = "#ffe4e6 bg:#44252c", "#d8f8e4 bg:#1c382e"
    if depth == ColorDepth.DEPTH_4_BIT:
        removed, added = "ansiwhite bg:ansired", "ansiblack bg:ansibrightgreen"
    elif depth == ColorDepth.DEPTH_8_BIT:
        removed, added = "#ffe4e6 bg:#5f0000", "#d8f8e4 bg:#005f00"
    styles = Style.from_dict({
        "file": "bold", "range": "ansibrightblack",
        "removed": removed, "added": added,
    })
    fragments = FormattedText()
    if title:
        fragments.append(("bold #8fe3ed", f"[{title}]\n\n"))
    if markdown:
        from .terminal import markdown_fragments

        fragments.extend(markdown_fragments(answer, columns, hyperlinks=depth != ColorDepth.DEPTH_1_BIT))
        fragments.append(("", "\n"))
    else:
        fragments.append(("", answer + "\n"))
    in_hunk = False
    width = columns if depth != ColorDepth.DEPTH_1_BIT else 0
    for line in preview.splitlines():
        style = ""
        if line.startswith("@@ "):
            in_hunk = True
            style = "class:range"
        elif in_hunk and line.startswith(("-", "+")):
            style = "class:removed" if line.startswith("-") else "class:added"
            if width:
                line = line.expandtabs()
                line += " " * max(0, width - get_cwidth(line))
        elif line.startswith("Proposed change in "):
            style = "class:file"
        fragments.extend(((style, line), ("", "\n")))

    async with in_terminal():
        print_formatted_text(output, fragments, styles, color_depth=depth)


async def confirm_edit(edit: Edit, ask: Callable[[], Awaitable[bool]], *, answer: str, title: str = "") -> bool:
    await show_preview(answer, edit.preview(), title=title, markdown=True)
    if not await ask():
        return False
    edit.apply()
    print("Edit applied.")
    return True


def command(text: str) -> str | None:
    lowered = text.lower()
    if lowered in ("/exit", "/cancel", "/resume", "/push"):
        return lowered
    if re.fullmatch(r"push\s*吧?[。！!]?", lowered):
        return "/push"
    if re.fullmatch(r"继续刚才没做完的(?:任务)?[。！!]?", text):
        return "/resume"
    return None


class Console:
    def __init__(self, root: Path, chat: Chat, allow_edit: bool, *, delegate: bool = False):
        self.root = root
        self.allow_edit = allow_edit
        self.store = Store(root)
        approval = self.approve_codex if sys.stdin.isatty() and sys.stdout.isatty() else None
        executor = CodexAgent(root, allow_edit, approval) if delegate else None
        self.assistant = Assistant(self.store, chat, context_bytes=context_budget(), executor=executor)
        self.jobs = Jobs(root)
        self.saved = self.store.queued()
        self.queue: deque = deque()
        self.wake = asyncio.Event()
        self.quit = asyncio.Event()
        self.running: dict[asyncio.Task, tuple[str, float, float]] = {}
        self.foreground: asyncio.Task | None = None
        self.background: set[asyncio.Task] = set()
        self.review_lock = asyncio.Lock()
        self.approval: asyncio.Future | None = None
        self.approval_prompt = EDIT_PROMPT
        self.approval_has_draft = False
        self.approved_files: dict[str, str] = {}
        self.session = None
        self.queue_count = 0
        self.queue_changed_at = 0.0
        self.requests = 0
        self.summary_queue: dict[str, None] = {}
        self.summary_worker: asyncio.Task | None = None

    def prompt(self):
        from prompt_toolkit.formatted_text import FormattedText

        output = []
        now = time.monotonic()
        progress = self.running.get(self.foreground) or next(iter(self.running.values()), None)
        if progress and progress[0]:
            stage, started, changed = progress
            frame = int((now - started) / 0.1)
            phrases = STATUS[stage]
            label = phrases[int((now - changed) / 4) % len(phrases)]
            center = frame % (len(label) + 6) - 3
            output.append(("class:accent", FRAMES[frame % len(FRAMES)] + "  "))
            output.append(("class:muted", f"{now - started:.1f}S  "))
            for index, char in enumerate(label):
                distance = min(abs(index - center), 3)
                output.append((f"class:glow{distance}", char))
        if len(self.running) > 1:
            output.append(("class:muted", "  │  " if output else ""))
            output.append(("", f"RUNNING {len(self.running)}"))
        queued = sum(isinstance(item, dict) for item in self.queue)
        if queued != self.queue_count:
            self.queue_count, self.queue_changed_at = queued, now
        if queued:
            last = len(QUEUE_STYLES) - 1
            fade = min(int((now - self.queue_changed_at) / 0.12), last) if progress else last
            output.extend((("class:muted", "  │  " if output else ""), ("class:muted", "QUEUED "),
                           (QUEUE_STYLES[fade], str(queued))))
        if self.saved:
            output.append(("class:muted", ("  │  " if output else "") + f"SAVED {len(self.saved)} · /RESUME"))
        if output:
            output.append(("", "\n"))
        waiting = self.approval is not None and not self.approval_has_draft
        output.append(("", self.approval_prompt if waiting else "NULI> "))
        return FormattedText(output)

    def interrupt(self):
        if self.approval is not None and not self.approval.done():
            self.approval.set_result(False)
            return
        target = self.foreground if self.foreground in self.running else next(reversed(self.running), None)
        if target:
            target.cancel()
        else:
            self.quit.set()

    async def review(self, answer: str, title: str, edit: Edit | None) -> bool:
        self.running.pop(asyncio.current_task(), None)
        if not edit:
            await show_preview(answer, title=title, markdown=True)
            return False
        async with self.review_lock:
            applied = await confirm_edit(edit, self.ask_approval, answer=answer, title=title)
            if applied:
                self.approved_files[edit.relative_path] = sha256(edit.after.encode("utf-8")).hexdigest()
            return applied

    async def ask_approval(self, prompt: str = EDIT_PROMPT) -> bool:
        if self.session is None or self.quit.is_set():
            return False
        self.approval_has_draft = bool(self.session.default_buffer.text)
        self.approval_prompt = prompt
        self.approval = asyncio.get_running_loop().create_future()
        try:
            return await self.approval
        finally:
            self.approval = None

    async def approve_codex(self, detail: str, diff: str) -> bool:
        async with self.review_lock:
            await show_preview(detail, diff)
            return await self.ask_approval(CODEX_PROMPT)

    async def push(self, progress: Callable[[str], None]) -> None:
        async with self.review_lock:
            progress("git")
            plan = await prepare_push(self.root, self.approved_files)
            if plan is None:
                print("NO APPROVED CHANGES OR LOCAL COMMITS TO PUSH.")
                return
            progress("")
            await show_preview(plan.preview())
            prompt = PUSH_PROMPT if plan.files else "PUSH LOCAL COMMITS? [Y/N] "
            if not await self.ask_approval(prompt):
                return
            progress("pushing")
            commit = await apply_push(self.root, self.approved_files, plan)
            print(f"Pushed {commit[:12]} to {plan.remote}/{plan.target.removeprefix('refs/heads/')}.")

    async def run(self, entry: dict | str):
        if entry == "/exit":
            self.quit.set()
            return
        if entry == "/cancel":
            self.assistant.pending = None
            return
        self.requests += 1
        task = asyncio.current_task()
        started = time.monotonic()
        self.running[task] = "", started, started

        def progress(stage: str) -> None:
            previous, _, changed = self.running[task]
            self.running[task] = stage, started, changed if previous == stage else time.monotonic()

        try:
            if entry == "/push":
                await self.push(progress)
                return
            result = await self.assistant.send(None if entry == "/resume" else entry,
                                               Workspace(self.root, self.allow_edit), progress, self.review)
            if entry == "/resume":
                restored = self.saved
                self.saved = []
                self.queue.extendleft(reversed(restored))
                if restored:
                    self.wake.set()
                elif result is None:
                    print("NO UNFINISHED REQUEST")
            if result and "question" in result:
                print(result["question"] + " (/CANCEL TO START OVER)")
            if result and "notice" in result:
                print(result["notice"])
            if result and "task" in result and self.session is not None and not self.assistant.executor:
                self.summary_queue[result["task"]["id"]] = None
        except asyncio.CancelledError:
            if not self.quit.is_set():
                print("GIT COMMAND STOPPED. TYPE /PUSH TO REVIEW." if entry == "/push"
                      else "REQUEST STOPPED. TYPE /RESUME TO CONTINUE.")
        except Exception as error:
            print(f"nuli: {error}", file=sys.stderr)
        finally:
            self.running.pop(task, None)
            self.requests -= 1
            self.start_summary()

    def start_summary(self) -> None:
        if self.summary_worker or self.requests or self.queue or self.quit.is_set() or not self.summary_queue:
            return
        self.summary_worker = asyncio.create_task(self.summarize_queue())
        self.summary_worker.add_done_callback(self.summary_finished)

    async def summarize_queue(self) -> None:
        while self.summary_queue and not (self.requests or self.queue or self.quit.is_set()):
            task_id = next(iter(self.summary_queue))
            self.summary_queue.pop(task_id)
            try:
                await self.assistant.summarize(task_id)
            except Exception:
                # The model failure is recorded in the trace. Retry after a later turn.
                pass

    def summary_finished(self, task: asyncio.Task) -> None:
        self.summary_worker = None
        self.start_summary()

    def launch(self, coroutine):
        async def report():
            try:
                await coroutine
            except Exception as error:
                print(f"nuli: {error}", file=sys.stderr)

        task = asyncio.create_task(report())
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    def submit(self, text: str):
        if self.approval is not None:
            if not self.approval_has_draft and not self.approval.done() and text.lower() in ("y", "n", ""):
                self.approval.set_result(text.lower() == "y")
                return
            self.approval_has_draft = False
        if not text:
            return
        immediate = re.fullmatch(r"/(parallel|bg|jobs|stop)(?:\s+(.+))?", text, re.I)
        if immediate:
            action, value = immediate[1].lower(), immediate[2]
            if action == "parallel" and value:
                self.launch(self.run(self.store.enqueue(value, new_task=True)))
            elif action == "bg" and value:
                self.launch(self.jobs.start(value))
            elif action == "jobs":
                print(self.jobs.status())
            elif action == "stop" and value and value.isascii() and value.isdecimal():
                async def stop():
                    if not await self.jobs.stop(int(value)):
                        print("NO RUNNING JOB WITH THAT ID")
                self.launch(stop())
            else:
                print({"parallel": "USAGE /PARALLEL <TASK>", "bg": "USAGE /BG <COMMAND>",
                       "stop": "USAGE /STOP <JOB ID>"}[action])
            return
        self.queue.append(self.entry(text))
        self.wake.set()

    def entry(self, text: str) -> str | dict:
        action = command(text)
        if action == "/push" and self.assistant.executor:
            return self.store.enqueue("push" if text.lower() == "/push" else text)
        return action or self.store.enqueue(text)

    async def read_input(self):
        while not self.quit.is_set():
            try:
                line = await self.session.prompt_async(self.prompt, refresh_interval=0.1, handle_sigint=False)
            except EOFError:
                break
            try:
                self.submit(line.strip())
            except OSError as error:
                print(f"MESSAGE NOT SAVED. {error}", file=sys.stderr)
        self.quit.set()

    async def process_queue(self):
        while not self.quit.is_set():
            if not self.queue:
                self.start_summary()
                self.wake.clear()
                await self.wake.wait()
                continue
            self.foreground = asyncio.create_task(self.run(self.queue.popleft()))
            await self.foreground
            self.foreground = None

    async def interact(self):
        print("NULI · /EXIT TO QUIT")
        if self.saved:
            print(f"SAVED {len(self.saved)} MESSAGES. TYPE /RESUME TO CONTINUE.")
            for item in self.saved[:3]:
                print("  " + item["request"][:100])
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            for line in sys.stdin:
                text = line.strip()
                if not text:
                    continue
                await self.run(self.entry(text))
                if self.quit.is_set():
                    break
                while self.queue:
                    await self.run(self.queue.popleft())
            return

        from prompt_toolkit import PromptSession
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.patch_stdout import patch_stdout
        from prompt_toolkit.styles import Style

        keys = KeyBindings()

        @keys.add("c-c", eager=True)
        def interrupt(event):
            self.interrupt()
            event.app.invalidate()

        style = Style.from_dict({
            "accent": "ansicyan", "muted": "ansibrightblack",
            "queue0": "bold ansiwhite", "queue1": "bold ansibrightcyan", "queue2": "ansibrightcyan", "queue3": "bold ansicyan",
            "glow0": "bold ansibrightcyan", "glow1": "ansibrightcyan", "glow2": "ansicyan", "glow3": "ansicyan dim",
        })
        self.session = PromptSession(key_bindings=keys, style=style)
        loop = asyncio.get_running_loop()
        signals = (signal.SIGTERM, signal.SIGHUP) if os.name == "posix" else ()
        for kind in signals:
            loop.add_signal_handler(kind, self.quit.set)
        with patch_stdout():
            reader = asyncio.create_task(self.read_input())
            worker = asyncio.create_task(self.process_queue())
            quit_waiter = asyncio.create_task(self.quit.wait())
            try:
                done, _ = await asyncio.wait((reader, worker, quit_waiter), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                self.quit.set()
                self.wake.set()
                summary_worker = self.summary_worker
                if summary_worker:
                    summary_worker.cancel()
                if self.approval is not None and not self.approval.done():
                    self.approval.set_result(False)
                for task in list(self.running):
                    task.cancel()
                reader.cancel()
                quit_waiter.cancel()
                await asyncio.gather(reader, worker, quit_waiter, *self.background, return_exceptions=True)
                if summary_worker:
                    await asyncio.gather(summary_worker, return_exceptions=True)
                await self.jobs.close()
                for kind in signals:
                    loop.remove_signal_handler(kind)


async def run_cli(edit: bool, task: str):
    root = Path.cwd().resolve()
    delegate = configured_provider() == "codex"
    if not task:
        async with configured_chat() as chat:
            await Console(root, chat, edit, delegate=delegate).interact()
        return
    with Trace(root).run(task, "one_shot") as trace:
        if delegate:
            async def approve(detail: str, diff: str) -> bool:
                await show_preview(detail, diff)
                return input(CODEX_PROMPT).strip().lower() == "y"

            interactive = sys.stdin.isatty() and sys.stdout.isatty()
            executor = CodexAgent(root, edit, approve if interactive else None)
            result = await executor.run(task, trace, lambda _: None, ephemeral=True)
            await show_preview(result.answer, markdown=True)
            return
        tools = Workspace(root, edit)
        async with configured_chat() as chat:
            answer, _ = await run_turn(task, chat, tools, trace=trace)
        if tools.edit:
            async def ask() -> bool:
                return sys.stdin.isatty() and sys.stdout.isatty() and input(EDIT_PROMPT).strip().lower() == "y"

            await review_change(tools.edit, trace, lambda: confirm_edit(tools.edit, ask, answer=answer))
        else:
            await show_preview(answer, markdown=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="nuli", description="Run one task or open the interactive Nuli assistant.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Interactive commands\n"
               "  /parallel TASK  Start an independent task\n"
               "  /bg COMMAND    Run a background command\n"
               "  /jobs          List background commands\n"
               "  /stop JOB_ID   Stop a background command\n"
               "  /push          Review, commit, and push approved edits\n"
               "  /resume        Recover interrupted and queued requests\n"
               "  /exit          Exit Nuli\n\n"
               "YCODE_PROVIDER selects codex (default) or ollama.",
    )
    parser.add_argument("--edit", action="store_true", help="enable edits in the workspace")
    parser.add_argument("task", nargs="*", help="task to run once")
    args = parser.parse_args()
    try:
        asyncio.run(run_cli(args.edit, " ".join(args.task)))
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"nuli: {error}", file=sys.stderr)
        return 1
    return 0
