from __future__ import annotations

import threading
import time
import unittest
from argparse import Namespace
from importlib.util import module_from_spec, spec_from_file_location
from inspect import signature
from pathlib import Path
from tempfile import TemporaryDirectory
import json


HARNESS_PATH = Path(__file__).with_name("formula_pressure_run.py")
SPEC = spec_from_file_location("formula_pressure_run", HARNESS_PATH)
assert SPEC is not None and SPEC.loader is not None
harness = module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


class CommandConstructionTests(unittest.TestCase):
    def test_claude_read_only_job_does_not_use_plan_permission_mode(self) -> None:
        args = Namespace(claude="claude", run_dir=Path("run"))

        command, stdin = harness.build_command(
            {"backend": "claude", "model": "claude-haiku-4-5", "effort": "high"},
            args,
            "prompt",
            [],
            Path("schema.json"),
        )

        self.assertEqual(stdin, "prompt")
        self.assertIn("--permission-mode", command)
        mode_index = command.index("--permission-mode")
        self.assertEqual(command[mode_index + 1], "dontAsk")
        self.assertNotIn("plan", command)

    def test_kimi_read_only_job_uses_stdin_without_plan_and_disables_mcp(self) -> None:
        args = Namespace(kimi="kimi", repo=Path("repo"), run_dir=Path("run"))

        command, stdin = harness.build_command(
            {"backend": "kimi", "model": "kimi-code/kimi-for-coding", "effort": "thinking"},
            args,
            "prompt",
            [],
            Path("schema.json"),
        )

        self.assertEqual(stdin, "prompt")
        self.assertNotIn("--plan", command)
        self.assertIn("--mcp-config", command)
        self.assertEqual(command[command.index("--mcp-config") + 1], "{}")


class OutputParsingTests(unittest.TestCase):
    def test_invalid_results_object_reports_json_error_instead_of_inner_record(self) -> None:
        stdout = (
            'progress\n{"results":[{"candidate_id":"x","classification":"formula",'
            '"latex":"\\pi","confidence":"high"},{"candidate_id":"y",'
            '"classification":"formula","latex":"y","confidence":"high"}]}'
        )

        with self.assertRaisesRegex(ValueError, r"invalid results JSON.*Invalid \\escape"):
            harness.result_payload("agy", stdout)


class ProviderSchedulingTests(unittest.TestCase):
    def test_each_provider_starts_without_waiting_for_other_provider_slots(self) -> None:
        providers = ["claude", "codex", "agy", "kimi"]
        jobs = [
            {"backend": provider, "model": f"{provider}-{index}", "effort": "high"}
            for provider in providers
            for index in range(4)
        ]
        first_three_started = {
            provider: threading.Event() for provider in providers
        }
        release = threading.Event()
        counts = {provider: 0 for provider in providers}
        lock = threading.Lock()

        def runner(job: dict) -> dict:
            provider = job["backend"]
            with lock:
                counts[provider] += 1
                if counts[provider] == 3:
                    first_three_started[provider].set()
            release.wait(timeout=5)
            return job

        completed: list[dict] = []
        worker = threading.Thread(
            target=lambda: completed.extend(
                harness.run_jobs_by_provider(jobs, runner, per_provider=3)
            ),
            daemon=True,
        )
        worker.start()
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not all(
                event.is_set() for event in first_three_started.values()
            ):
                time.sleep(0.01)
            self.assertTrue(
                all(event.is_set() for event in first_three_started.values()),
                counts,
            )
        finally:
            release.set()
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(completed), len(jobs))

    def test_result_callback_runs_on_parent_thread(self) -> None:
        self.assertIn("on_result", signature(harness.run_jobs_by_provider).parameters)
        parent_ident = threading.get_ident()
        callback_threads: list[int] = []
        jobs = [
            {"backend": "claude", "model": "one", "effort": "high"},
            {"backend": "codex", "model": "two", "effort": "high"},
        ]

        harness.run_jobs_by_provider(
            jobs,
            lambda job: job,
            per_provider=1,
            on_result=lambda _result: callback_threads.append(threading.get_ident()),
        )

        self.assertEqual(callback_threads, [parent_ident, parent_ident])


class ResumeTests(unittest.TestCase):
    def test_reuses_existing_valid_job(self) -> None:
        self.assertTrue(hasattr(harness, "find_existing_valid_status"))
        with TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            job = {"backend": "claude", "model": "model", "effort": "high"}
            job_dir = run_dir / "jobs" / "claude-model-high"
            job_dir.mkdir(parents=True)
            row = {"candidate_id": "a", "classification": "formula", "latex": "x", "confidence": "high", "note": ""}
            (job_dir / "result.json").write_text(json.dumps({"results": [row]}), encoding="utf-8")
            status = {"job_id": "claude-model-high", "valid": True}
            (job_dir / "status.json").write_text(json.dumps(status), encoding="utf-8")

            found = harness.find_existing_valid_status(job, Namespace(run_dir=run_dir), ["a"])

            self.assertEqual(found, status)

    def test_retry_job_id_preserves_existing_failed_directory(self) -> None:
        self.assertTrue(hasattr(harness, "next_job_id"))
        with TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            jobs_dir = run_dir / "jobs"
            (jobs_dir / "agy-model-high").mkdir(parents=True)

            job_id = harness.next_job_id(
                {"backend": "agy", "model": "model", "effort": "high"},
                Namespace(run_dir=run_dir),
            )

            self.assertEqual(job_id, "agy-model-high-retry-1")


if __name__ == "__main__":
    unittest.main()
