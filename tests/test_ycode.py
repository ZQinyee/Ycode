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
from ycode_core.cli import Console
from ycode_core.jobs import Jobs
from ycode_core.memory import size
from ycode_core.models import codex_usage
from ycode_core.storage import Store
from ycode_core.tools import Workspace


CLI = Path(__file__).resolve().parents[1] / "ycode.py"


def reply(content="", /, *, tool=None, **arguments):
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

    def traces(self):
        return [[json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                for path in (self.root / ".ycode/runs").glob("*.jsonl")]

    def long_task(self):
        history = []
        for index in range(9):
            history.extend(({"role": "user", "content": f"Turn {index}: " + "a" * 720}, reply(f"Done {index}")))
        task = {"id": str(uuid4()), "title": "Report", "goal": "Prepare a report", "history": history,
                "artifacts": [], "updatedAt": 1}
        self.store.save_task(task)
        return task

    async def test_long_history_is_bounded_without_changing_the_archive(self):
        task = self.long_task()
        task["history"][-1:-1] = [
            reply(tool="read_file", path="report.md"),
            {"role": "tool", "tool_name": "read_file", "content": "页" * 2000},
        ]
        history = task["history"]

        async def chat(messages, tools):
            self.assertLessEqual(size(messages), 4096)
            self.assertIn("Facts from early turns", messages[1]["content"])
            self.assertNotIn(history[0], messages)
            result_index = next(i for i, item in enumerate(messages) if item["role"] == "tool")
            self.assertIn("tool_calls", messages[result_index - 1])
            self.assertIn("[omitted]", messages[result_index]["content"])
            self.assertEqual(messages[-1]["content"], "Follow up")
            return reply("Done")

        answer, transcript = await run_turn("Follow up", chat, self.workspace, history,
                                            summary={"through": 4, "text": "Facts from early turns"}, context_bytes=4096)
        self.assertEqual(answer, "Done")
        self.assertEqual(transcript[:-2], history)
        self.assertEqual(transcript[-4]["content"], "页" * 2000)

    async def test_summary_merges_with_new_turns_and_advances_incrementally(self):
        task = self.long_task()
        started, finish = asyncio.Event(), asyncio.Event()
        batches = []

        async def chat(messages, tools):
            if tools:
                return reply("Latest answer")
            batches.append(messages[-1]["content"])
            started.set()
            await finish.wait()
            return reply("Facts from early turns")

        assistant = Assistant(self.store, chat, context_bytes=4096)
        worker = asyncio.create_task(assistant.summarize(task["id"]))
        try:
            await asyncio.wait_for(started.wait(), 2)
            with self.assertRaisesRegex(RuntimeError, "busy"):
                await Assistant(Store(self.root), chat, context_bytes=4096).summarize(task["id"])
            await assistant.send("继续 [Report]：new message", self.workspace)
        finally:
            finish.set()
            await worker
        stored = self.store.load_task(task["id"])
        self.assertEqual(stored["history"][:-2], task["history"])
        self.assertEqual(stored["history"][-1]["content"], "Latest answer")
        through = stored["memory"]["through"]
        self.assertGreater(through, 0)
        self.assertEqual(stored["history"][through]["role"], "user")
        self.assertIn("Turn 0:", batches[0])
        await assistant.summarize(task["id"])
        self.assertNotIn("Turn 0:", batches[1])
        self.assertIn("Facts from early turns", batches[1])
        self.assertGreater(self.store.load_task(task["id"])["memory"]["through"], through)

    async def test_summary_starts_when_idle_and_finishes_alongside_new_turn(self):
        task = self.long_task()
        summarizing, finish = asyncio.Event(), asyncio.Event()

        async def chat(messages, tools):
            if tools:
                return reply("OK")
            summarizing.set()
            await finish.wait()
            return reply("Original summary completed")

        with patch.dict(os.environ, {"YCODE_CONTEXT_BYTES": "4096"}):
            console = Console(self.root, chat, False)
        console.session = object()
        console.queue.append(self.store.enqueue("继续 [Report]：second"))
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                await console.run(self.store.enqueue("继续 [Report]：first"))
                self.assertIsNone(console.summary_worker)
                await console.run(console.queue.popleft())
                await asyncio.wait_for(summarizing.wait(), 2)
                worker = console.summary_worker
                await asyncio.wait_for(console.run(self.store.enqueue("继续 [Report]：third")), 2)
                saved = self.store.load_task(task["id"])
                self.assertEqual(saved["history"][-2]["content"], "继续 [Report]：third")
                self.assertNotIn("memory", saved)
                finish.set()
                await asyncio.wait_for(worker, 2)
            finally:
                console.quit.set()
                if console.summary_worker:
                    console.summary_worker.cancel()
                    await asyncio.gather(console.summary_worker, return_exceptions=True)
        saved = self.store.load_task(task["id"])
        self.assertEqual(saved["memory"]["text"], "Original summary completed")
        self.assertEqual(saved["history"][-2]["content"], "继续 [Report]：third")

    async def test_save_failure_resumes_finished_answer_without_model(self):
        async def chat(messages, tools):
            return routed("new", title="Report", answer="Ready")

        with patch.object(self.store, "save_task", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                await Assistant(self.store, chat).send("Report", self.workspace)
        with patch.object(self.store, "history", side_effect=AssertionError("full scan")):
            result = await Assistant(self.store, self.unexpected_chat).send(None, self.workspace)
        self.assertEqual(result["answer"], "Ready")
        self.assertEqual(len(result["task"]["history"]), 2)
        self.assertIsNone(self.store.resume())

    async def unexpected_chat(self, messages, tools):
        self.fail("A finished answer must be recovered without another model call")

    async def test_retry_after_journal_cleanup_failure_keeps_each_turn_once(self):
        async def chat(messages, tools):
            return routed("new", title="Report", answer="First") if not tools else reply("Second")

        assistant = Assistant(self.store, chat)
        with patch("ycode_core.storage.Turn.complete", side_effect=OSError("cleanup")):
            with self.assertRaisesRegex(OSError, "cleanup"):
                await assistant.send("start", self.workspace)
        await assistant.send("继续 [Report]：more", self.workspace)
        result = await Assistant(Store(self.root), self.unexpected_chat).send(None, self.workspace)
        self.assertEqual([m["content"] for m in result["task"]["history"]],
                         ["start", "First", "继续 [Report]：more", "Second"])
        self.assertIsNone(self.store.resume())

    async def test_edit_recovery_preserves_file_and_reports_outcome(self):
        async def chat(messages, tools):
            if messages[-1]["role"] == "user":
                return reply(tool="write_file", path=messages[-1]["content"], content="proposed")
            return reply("Prepared file")

        for failure in ("saving", "reviewing"):
            with self.subTest(failure=failure):
                filename = failure + ".txt"
                entry = self.store.enqueue(filename, new_task=True)

                async def approve(answer, title, edit):
                    edit.apply()
                    if failure == "reviewing":
                        raise OSError("interrupted")
                    return True

                with patch.object(self.store, "save_task", side_effect=OSError("interrupted")):
                    with self.assertRaisesRegex(OSError, "interrupted"):
                        await Assistant(self.store, chat).send(entry, Workspace(self.root, True), review=approve)
                path = self.root / filename
                self.assertEqual(path.read_text(), "proposed")
                path.write_text("later external edit")
                reopened = Assistant(Store(self.root), self.unexpected_chat)
                result = await reopened.send(None, Workspace(self.root, True))
                self.assertEqual(path.read_text(), "later external edit")
                if failure == "saving":
                    self.assertEqual(result["task"]["artifacts"], [filename])
                else:
                    self.assertIn(filename, result["notice"])
                    self.assertEqual(result["task"]["artifacts"], [])
                self.assertIsNone(self.store.resume())

    async def test_concurrent_followups_keep_conflict_for_resume(self):
        self.store.save_task({"id": str(uuid4()), "title": "Report", "goal": "Report", "updatedAt": 1,
                              "artifacts": [], "history": []})
        started, release = asyncio.Event(), asyncio.Event()
        seen = []

        async def chat(messages, tools):
            seen.append([m["content"] for m in messages[1:]])
            if len(seen) == 1:
                started.set()
                await release.wait()
            return reply("OK")

        first = Assistant(self.store, chat)
        other = Assistant(Store(self.root), chat)
        a, b = "继续 [Report]：first", "继续 [Report]：second"
        slow = asyncio.create_task(first.send(a, self.workspace))
        await asyncio.wait_for(started.wait(), 2)
        try:
            await other.send(b, self.workspace)
        finally:
            release.set()
        with self.assertRaisesRegex(RuntimeError, "/resume"):
            await slow
        result = await first.send(None, self.workspace)
        self.assertEqual([m["content"] for m in result["task"]["history"]], [b, "OK", a, "OK"])
        self.assertEqual(seen[-1], [b, "OK", a])

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
        traces = {events[0]["request"]: events for events in self.traces()}
        self.assertEqual([event["phase"] for event in traces["你好"] if event["event"] == "model.started"],
                         ["routing"])
        continued = traces["继续 [周报]：更新标题"]
        self.assertEqual([event["phase"] for event in continued if event["event"] == "model.started"],
                         ["answering"])
        self.assertEqual(continued[-1]["task_id"], first["task"]["id"])

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
        cancelled, = [events for events in self.traces() if events[-1]["status"] == "cancelled"]
        self.assertEqual((cancelled[-2]["event"], cancelled[-2]["status"]), ("model.finished", "cancelled"))
        recovered, = [events for events in self.traces() if events[0]["mode"] == "resume"]
        self.assertNotEqual(cancelled[0]["run_id"], recovered[0]["run_id"])
        self.assertEqual(cancelled[-1]["task_id"], recovered[-1]["task_id"])

    async def test_trace_records_model_error_on_the_step_and_run(self):
        async def chat(messages, tools):
            raise RuntimeError("provider unavailable")

        with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
            await Assistant(self.store, chat).send("read notes", self.workspace)
        events, = self.traces()
        model_end, run_end = events[-2:]
        self.assertEqual((model_end["event"], model_end["status"], model_end["step_id"]),
                         ("model.finished", "error", 1))
        self.assertEqual(model_end["error"], {"type": "RuntimeError", "message": "provider unavailable"})
        self.assertEqual((run_end["event"], run_end["status"]), ("run.finished", "error"))
        self.assertIsNone(model_end["usage"])
        self.assertIsNone(model_end["estimated_cost_usd"])

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
        never_scan = patch.object(reopened, "history", side_effect=AssertionError("unnecessary full scan"))
        with never_scan:
            novel_task = asyncio.create_task(assistant.send(novel, Workspace(self.root)))
            await asyncio.wait_for(started.wait(), 2)
        try:
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
                messages = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["messages"]
                last = messages[-1]
                if last["role"] == "user":
                    response = reply(tool="read_file", path="missing.txt")
                    response["tool_calls"] += reply(tool="read_file", path="note.txt")["tool_calls"]
                elif last["role"] == "tool" and messages[-2]["content"].startswith("Tool error: "):
                    response = reply(last["content"])
                else:
                    self.send_error(400, "Missing tool error feedback")
                    return
                document = json.dumps({"message": response, "model": "mock", "prompt_eval_count": 25,
                                       "prompt_eval_cached_count": 5, "eval_count": 5}).encode()
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
                       "YCODE_TOKEN_RATES_USD": '{"input":2,"cached_input":0.5,"output":4}',
                       "YCODE_BASE_URL": f"http://127.0.0.1:{server.server_port}"}
                result = subprocess.run([sys.executable, str(CLI), "Read note.txt"], cwd=self.root, env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual((result.returncode, result.stdout.strip(), result.stderr),
                                 (0, "notes from the workspace", ""))
            finally:
                server.shutdown()
                thread.join()
        events, = self.traces()
        self.assertEqual(events[0]["mode"], "one_shot")
        self.assertEqual(len({event["run_id"] for event in events}), 1)
        model_ends = [event for event in events if event["event"] == "model.finished"]
        self.assertEqual([event["step_id"] for event in model_ends], [1, 2])
        self.assertEqual(model_ends[0]["usage"], {"input_tokens": 25, "cached_input_tokens": 5, "output_tokens": 5})
        self.assertEqual(model_ends[0]["estimated_cost_usd"], 0.0000625)
        self.assertEqual(model_ends[0]["model"], "mock")
        tool_starts = [event for event in events if event["event"] == "tool.started"]
        tool_ends = [event for event in events if event["event"] == "tool.finished"]
        self.assertEqual([event["tool_call_id"] for event in tool_starts], ["1.1", "1.2"])
        self.assertEqual([event["tool_call_id"] for event in tool_ends], ["1.1", "1.2"])
        self.assertEqual([event["status"] for event in tool_ends], ["error", "ok"])
        self.assertEqual(tool_starts[0]["arguments"], {"path": "missing.txt"})
        self.assertIn("missing.txt", tool_ends[0]["result"])
        self.assertEqual(tool_ends[1]["result"], "notes from the workspace")
        self.assertTrue(all(event["duration_ms"] >= 0 for event in tool_ends + model_ends))

    def test_codex_usage_reads_only_completion_event(self):
        events = (b'{"type":"turn.started"}\n'
                  b'{"type":"item.completed","item":{"type":"agent_message"}}\n'
                  b'{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":20,"output_tokens":5}}\n'
                  b'not JSON\n')
        self.assertEqual(codex_usage(events), {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 5})
        self.assertEqual(codex_usage(b'{"type":"turn.failed"}\n'), {})


if __name__ == "__main__":
    unittest.main()
