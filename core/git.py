from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .jobs import stop_process


async def git(root: Path, *args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "git", "--literal-pathspecs", *args, cwd=root,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=os.name == "posix",
    )
    output = asyncio.create_task(process.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(output), 120)
    except (asyncio.CancelledError, asyncio.TimeoutError) as error:
        await stop_process(process, group=True)
        await output
        if isinstance(error, asyncio.TimeoutError):
            raise RuntimeError(f"git {args[0]} timed out.") from error
        raise
    if process.returncode:
        detail = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip()
        raise RuntimeError(detail or f"git {args[0]} failed.")
    return stdout.decode("utf-8", errors="surrogateescape")


@dataclass(frozen=True)
class PushPlan:
    root: Path
    branch: str
    remote: str
    target: str
    push_url: str
    head: str
    upstream: str
    ahead: tuple[str, ...]
    files: tuple[tuple[str, str], ...]
    new_files: tuple[str, ...]
    message: str
    diff: str

    def preview(self) -> str:
        lines = [f"Push {self.branch} to {self.remote}/{self.target.removeprefix('refs/heads/')}"]
        if self.files:
            lines.append(f"Commit message  {self.message}")
            lines.extend(f"  {path!r}" + (" (new)" if path in self.new_files else "") for path, _ in self.files)
            if self.diff:
                lines.extend(("", self.diff.rstrip()))
        if self.ahead:
            lines.append("Local commits included in this push")
            lines.extend(f"  {entry}" for entry in self.ahead)
        return "\n".join(lines)


async def prepare_push(workspace: Path, approved_files: dict[str, str]) -> PushPlan | None:
    root = Path((await git(workspace, "rev-parse", "--show-toplevel")).rstrip("\n")).resolve()
    branch = (await git(root, "symbolic-ref", "--short", "HEAD")).strip()
    tracking = (await git(root, "for-each-ref", "--format=%(upstream:remotename)%09%(upstream:remoteref)",
                          f"refs/heads/{branch}")).rstrip("\n")
    remote, _, target = tracking.partition("\t")
    if not remote or not target.startswith("refs/heads/"):
        raise ValueError("Set an upstream branch before using /push.")
    urls = (await git(root, "remote", "get-url", "--push", "--all", "--", remote)).splitlines()
    if len(urls) != 1:
        raise ValueError("/push requires a single push URL for the upstream remote.")
    head = (await git(root, "rev-parse", "HEAD")).strip()
    upstream = (await git(root, "rev-parse", "@{upstream}")).strip()
    behind = int(await git(root, "rev-list", "--count", f"{head}..{upstream}"))
    if behind:
        raise ValueError("The upstream branch has commits this branch needs to incorporate before /push.")
    ahead = tuple((await git(root, "log", "--format=%h %s", f"{upstream}..{head}")).splitlines())

    candidates = {(workspace / name).relative_to(root).as_posix(): digest for name, digest in approved_files.items()}
    files, new_files, diff = [], (), ""
    if candidates:
        tracked = await git(root, "diff", "--name-only", "--no-renames", "-z", head, "--", *sorted(candidates))
        untracked = await git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", *sorted(candidates))
        new_files = tuple(sorted(set(untracked.split("\0")) & candidates.keys()))
        selected = (set(tracked.split("\0")) | set(new_files)) & set(candidates)
        for name in sorted(selected):
            path = root / name
            if (path.resolve() != path or not path.is_file()
                    or sha256(path.read_bytes()).hexdigest() != candidates[name]):
                raise ValueError(f"File {name!r} changed after approval. Review it before using /push.")
            blob = (await git(root, "hash-object", f"--path={name}", "--", name)).strip()
            files.append((name, blob))
        if files:
            names = [name for name, _ in files]
            diff = await git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-color", head, "--", *names)
    if not files and not ahead:
        return None
    filename = Path(files[0][0]).name if len(files) == 1 else ""
    message = f"Update {filename}" if filename and filename.isprintable() else "Apply Ycode edits"
    return PushPlan(root, branch, remote, target, urls[0], head, upstream, ahead, tuple(files), new_files, message, diff)


async def apply_push(workspace: Path, approved_files: dict[str, str], plan: PushPlan) -> str:
    # Use the push URL for the check in case it differs from the fetch URL.
    remote_head = await git(plan.root, "ls-remote", "--exit-code", "--", plan.push_url, plan.target)
    if remote_head.split("\t", 1)[0] != plan.upstream:
        raise ValueError("The remote branch changed. Fetch and review the updated branch before retrying /push.")
    if await prepare_push(workspace, approved_files) != plan:
        raise ValueError("The push plan changed. Enter /push to review the new plan.")

    commit = plan.head
    if plan.files:
        if plan.new_files:
            await git(plan.root, "add", "--intent-to-add", "--", *plan.new_files)
        names = [name for name, _ in plan.files]
        await git(plan.root, "commit", "--only", "-m", plan.message, "--", *names)
        commit = (await git(plan.root, "rev-parse", "HEAD")).strip()
        changed = await git(plan.root, "diff", "--name-only", "--no-renames", "-z", plan.head, commit, "--")
        parents = (await git(plan.root, "rev-list", "--parents", "-n", "1", commit)).split()
        blobs = [(name, (await git(plan.root, "rev-parse", f"{commit}:{name}")).strip()) for name in names]
        if parents != [commit, plan.head] or set(filter(None, changed.split("\0"))) != set(names) or tuple(blobs) != plan.files:
            raise RuntimeError(f"Commit {commit[:12]} was created locally with different changes. Review it before pushing.")
    try:
        await git(plan.root, "push", "--no-follow-tags", "--recurse-submodules=no", "--",
                  plan.remote, f"{commit}:{plan.target}")
    except RuntimeError as error:
        raise RuntimeError(f"Git reported a push failure for commit {commit[:12]}.\n{error}") from error
    return commit
