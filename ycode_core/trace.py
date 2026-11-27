from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .storage import local_directory, timestamp


class Trace:
    def __init__(self, root: Path):
        self.root = root
        self.run_id = str(uuid4())
        self.task_id: str | None = None
        self.step_id = 0
        self.edit_call: dict = {}
        self.output = None
        self.rates = {}

    @contextmanager
    def run(self, request: str, mode: str):
        try:
            directory = local_directory(local_directory(self.root / ".ycode") / "runs")
            path = directory / f"{timestamp()}-{self.run_id}.jsonl"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self.output = os.fdopen(fd, "w", encoding="utf-8", buffering=1)
        except (OSError, ValueError) as error:
            self.warn(error)
        try:
            self.rates = self.read_rates()
        except ValueError as error:
            self.warn(error)
        try:
            with span(self, "run", request=request, mode=mode):
                yield self
        finally:
            self.close()

    def emit(self, event: str, **fields) -> None:
        if self.output is None:
            return
        record = {"event": event, "timestamp_ms": timestamp(), "run_id": self.run_id,
                  "task_id": self.task_id, **fields}
        try:
            self.output.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        except (OSError, TypeError, ValueError) as error:
            self.warn(error)
            self.close()

    def close(self) -> None:
        if self.output is not None:
            try:
                self.output.close()
            except OSError as error:
                self.warn(error)
            finally:
                self.output = None

    def warn(self, error: Exception) -> None:
        print(f"ycode: trace {self.run_id}: {error}", file=sys.stderr)

    @staticmethod
    def read_rates() -> dict:
        raw = os.getenv("YCODE_TOKEN_RATES_USD")
        rates = json.loads(raw) if raw else {}
        if not isinstance(rates, dict) or any(
            key not in {"input", "cached_input", "output"} or type(rate) not in (int, float)
            or not 0 <= rate < math.inf for key, rate in rates.items()
        ):
            raise ValueError("YCODE_TOKEN_RATES_USD must contain nonnegative USD rates per million tokens.")
        return rates

    def estimate_cost(self, usage: dict | None) -> float | None:
        if not usage or not {"input_tokens", "output_tokens"} <= usage.keys() or not {"input", "output"} <= self.rates.keys():
            return None
        input_tokens, output_tokens = usage["input_tokens"], usage["output_tokens"]
        cached = min(input_tokens, usage.get("cached_input_tokens", 0))
        # The input rate applies to all input unless a separate cache rate is configured.
        try:
            cost = ((input_tokens - cached) * self.rates["input"] +
                    cached * self.rates.get("cached_input", self.rates["input"]) +
                    output_tokens * self.rates["output"]) / 1_000_000
        except OverflowError:
            return None
        return round(cost, 12) if math.isfinite(cost) else None


@contextmanager
def span(trace: Trace | None, kind: str, **fields):
    outcome = {"status": "ok"}
    if trace is None:
        yield outcome
        return
    started = time.monotonic_ns()
    trace.emit(kind + ".started", **fields)
    try:
        yield outcome
    except (asyncio.CancelledError, KeyboardInterrupt):
        outcome["status"] = "cancelled"
        raise
    except Exception as error:
        outcome.update(status="error", error={"type": type(error).__name__, "message": str(error)})
        raise
    finally:
        duration = round((time.monotonic_ns() - started) / 1_000_000, 3)
        trace.emit(kind + ".finished", **{**fields, **outcome, "duration_ms": duration})
