from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .storage import local_directory


async def stop_process(process: asyncio.subprocess.Process, group: bool = False) -> None:
    if process.returncode is not None:
        return

    def send(kind: signal.Signals) -> None:
        try:
            if group and os.name == "posix":
                os.killpg(process.pid, kind)
            elif kind == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except ProcessLookupError:
            pass

    send(signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), 0.5)
    except asyncio.TimeoutError:
        send(getattr(signal, "SIGKILL", signal.SIGTERM))
        await process.wait()


@dataclass
class Job:
    command: str
    path: Path
    process: asyncio.subprocess.Process
    watcher: asyncio.Task

    def state(self) -> str:
        code = self.process.returncode
        if code is None:
            return "RUNNING"
        return f"EXIT {code}" if code >= 0 else f"STOPPED {signal.Signals(-code).name}"


class Jobs:
    def __init__(self, root: Path):
        self.root = root
        self.jobs: dict[int, Job] = {}
        self.next_id = 0

    async def start(self, command: str) -> int:
        self.next_id += 1
        number = self.next_id
        directory = local_directory(self.root / ".ycode" / "jobs")
        path = directory / f"{number}-{uuid4()}.log"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        shell = os.environ.get("SHELL", "/bin/sh")
        args = [shell, "-c", command] if os.name == "posix" else [os.environ.get("ComSpec", "cmd.exe"), "/d", "/s", "/c", command]
        with os.fdopen(fd, "wb") as log:
            process = await asyncio.create_subprocess_exec(*args, cwd=self.root, stdin=asyncio.subprocess.DEVNULL,
                                                           stdout=log, stderr=log, start_new_session=os.name == "posix")

        async def report() -> None:
            await process.wait()
            print(f"[JOB {number}] {self.jobs[number].state()}\n{path}")

        self.jobs[number] = Job(command, path, process, asyncio.create_task(report()))
        print(f"[JOB {number}] STARTED {command}")
        return number

    def status(self) -> str:
        return "\n".join(f"[JOB {number}] {job.state()} · {job.command}\n{job.path}"
                         for number, job in self.jobs.items()) or "NO BACKGROUND COMMANDS"

    async def stop(self, number: int) -> bool:
        job = self.jobs.get(number)
        if not job or job.process.returncode is not None:
            return False
        await stop_process(job.process, group=True)
        await job.watcher
        return True

    async def close(self) -> None:
        await asyncio.gather(*(stop_process(job.process, group=True) for job in self.jobs.values()))
        await asyncio.gather(*(job.watcher for job in self.jobs.values()))
