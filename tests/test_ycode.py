import asyncio
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from ycode_core.agent import Assistant, run_turn
from ycode_core.jobs import Jobs
from ycode_core.storage import Store
from ycode_core.tools import Workspace


CLI = Path(__file__).resolve().parents[1] / "ycode.py"


def reply(content="", *, tool=None, **arguments):
    message = {"role": "assistant", "content": content}
    if tool:
        message["tool_calls"] = [{"function": {"name": tool, "arguments": arguments}}]
    return message


def routed(action, **fields):
    return reply(json.dumps({"action": action, **fields}, ensure_ascii=False))


class YcodeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.store = Store(self.root)
        self.workspace = Workspace(self.root)

    async def test_direct_answer_and_known_task_reuse_history(self):
        async def chat(messages, tools):
            if not tools:
                self.assertEqual(json.loads(messages[-1]["content"])["input"], "你好")
                return routed("new", title="周报", answer="发布进度周报")
            self.assertEqual([message["content"] for message in messages[1:]],
                             ["你好", "发布进度周报", "继续 [周报]：更新标题"])
            return reply("本周进度")

        first = await Assistant(self.store, chat).send("你好", self.workspace)
        second = await Assistant(Store(self.root), chat).send("继续 [周报]：更新标题", self.workspace)
        self.assertEqual(second["task"]["id"], first["task"]["id"])
        self.assertEqual(second["answer"], "本周进度")

    async def test_interrupted_request_stays_bound_to_its_task(self):
        report_id = str(uuid4())
        self.store.save_task({"id": report_id, "title": "周报", "goal": "写周报", "updatedAt": 0,
                              "artifacts": ["report.md"], "history": [reply("发布进度周报")]})
        started = asyncio.Event()

        async def chat(messages, tools):
            if not tools:
                route = json.loads(messages[-1]["content"])
                if route["input"] == "加个英文标题":
                    return routed("resume", id=report_id)
                return routed("new", title="计算", answer="56")
            self.assertEqual(messages[1]["content"], "发布进度周报")
            if not started.is_set():
                started.set()
                await asyncio.Future()
            return reply("Release Update")

        assistant = Assistant(self.store, chat)
        attempt = asyncio.create_task(assistant.send("加个英文标题", self.workspace))
        await asyncio.wait_for(started.wait(), 2)
        self.assertIsNone(await Assistant(Store(self.root), chat).send(None, self.workspace))
        attempt.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await attempt
        await assistant.send("7×8", self.workspace)
        reopened = Assistant(Store(self.root), chat)
        resumed = await reopened.send(None, self.workspace)
        self.assertEqual(resumed["task"]["id"], report_id)
        self.assertEqual(resumed["answer"], "Release Update")
        self.assertIsNone(await reopened.send(None, self.workspace))

    async def test_queued_and_parallel_requests_keep_separate_histories(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def chat(messages, tools):
            self.assertTrue(tools)
            request, = [message["content"] for message in messages if message["role"] == "user"]
            if request == "写小说":
                started.set()
                await release.wait()
            return reply(request + "已完成")

        novel = self.store.enqueue("写小说", new_task=True)
        summary = self.store.enqueue("总结 README", new_task=True)
        reopened = Store(self.root)
        self.assertEqual(reopened.queued(), [novel, summary])
        assistant = Assistant(reopened, chat)
        novel_task = asyncio.create_task(assistant.send(novel, Workspace(self.root)))
        try:
            await asyncio.wait_for(started.wait(), 2)
            self.assertIsNone(await Assistant(self.store, chat).send(novel, Workspace(self.root)))
            self.assertEqual(reopened.queued(), [summary])
            await assistant.send(summary, Workspace(self.root))
            self.assertEqual([task["title"] for task in reopened.history()], ["总结 README"])
        finally:
            release.set()
            await novel_task
        histories = {task["title"]: [message["content"] for message in task["history"]]
                     for task in reopened.history()}
        self.assertEqual(histories, {"写小说": ["写小说", "写小说已完成"],
                                     "总结 README": ["总结 README", "总结 README已完成"]})

    async def test_model_tool_budget(self):
        async def chat(messages, tools):
            return reply(tool="list_files", path=".")

        with self.assertRaisesRegex(ValueError, "limit of 12"):
            await run_turn("inspect", chat, self.workspace)

    def test_edits_require_approval_and_recheck_the_file(self):
        path = self.root / "note.txt"
        path.write_bytes(b"Title: before\r\nBody")
        args = {"path": "note.txt", "old_text": "before", "new_text": "after"}
        tools = Workspace(self.root, True)
        tools.execute("replace_text", args)
        self.assertEqual(path.read_bytes(), b"Title: before\r\nBody")
        tools.edit.apply()
        self.assertEqual(path.read_bytes(), b"Title: after\r\nBody")
        second = Workspace(self.root, True)
        second.execute("replace_text", {**args, "old_text": "after", "new_text": "latest"})
        path.write_bytes(b"External change")
        with self.assertRaisesRegex(ValueError, "The file changed"):
            second.edit.apply()
        self.assertEqual(path.read_bytes(), b"External change")
        (self.root / "outside").symlink_to(self.root.parent, target_is_directory=True)
        self.assertIn("outside the visible project", tools.execute("list_files", {"path": "outside"}))

    @unittest.skipUnless(os.name == "posix", "uses POSIX shell commands")
    async def test_background_command_logs_exit_and_can_be_stopped(self):
        jobs = Jobs(self.root)
        self.addAsyncCleanup(jobs.close)
        with patch.dict(os.environ, {"SHELL": "/bin/sh"}), contextlib.redirect_stdout(io.StringIO()):
            first = await jobs.start("printf sample; exit 7")
            await asyncio.wait_for(jobs.jobs[first].watcher, 2)
            second = await jobs.start("sleep 30")
            self.assertTrue(await asyncio.wait_for(jobs.stop(second), 2))
        self.assertEqual(jobs.jobs[first].path.read_text(), "sample")
        self.assertIn("[JOB 1] EXIT 7", jobs.status())
        self.assertIn("[JOB 2] STOPPED SIGTERM", jobs.status())

    def test_cli_reads_a_file_and_returns_tool_output_to_ollama(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                last = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["messages"][-1]
                response = reply(tool="read_file", path="note.txt") if last["role"] == "user" else reply(last["content"])
                document = json.dumps({"message": response}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(document)

            def log_message(self, *args):
                pass

        (self.root / "note.txt").write_text("notes from the workspace", encoding="utf-8")
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                env = {**os.environ, "YCODE_PROVIDER": "ollama", "NO_PROXY": "127.0.0.1",
                       "YCODE_BASE_URL": f"http://127.0.0.1:{server.server_port}"}
                result = subprocess.run([sys.executable, str(CLI), "Read note.txt"], cwd=self.root, env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual((result.returncode, result.stdout.strip(), result.stderr),
                                 (0, "notes from the workspace", ""))
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    unittest.main()
