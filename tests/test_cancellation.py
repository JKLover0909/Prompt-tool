import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import time as clock_time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prompt_tool as app  # noqa: E402


class CancellationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="prompt-tool-test-"))
        self.folder = self.tmp / "project"
        self.folder.mkdir()
        self.project_root = self.tmp / "claude-projects"
        self.project_dir = self.project_root / app.re.sub(
            r"[^A-Za-z0-9]", "-", str(self.folder.resolve())
        )
        self.project_dir.mkdir(parents=True)
        self.session_id = "11111111-1111-4111-8111-111111111111"
        (self.project_dir / f"{self.session_id}.jsonl").write_text(
            json.dumps({"type": "user", "sessionId": self.session_id}) + "\n"
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_claude(self, ignore_term=False):
        path = self.tmp / "fake-claude"
        code = "import subprocess, time\n"
        if ignore_term:
            code += (
                "subprocess.Popen([sys.executable, '-I', '-c', "
                "'import signal,time; signal.signal(signal.SIGTERM, "
                "signal.SIG_IGN); time.sleep(60)'])\n"
            )
        code += "time.sleep(60)\n"
        path.write_text("#!/usr/bin/python3\nimport sys\n" + code)
        path.chmod(0o700)
        return str(path)

    def test_zero_timeout_means_no_timeout(self):
        job = app.parse_job({
            "name": "unlimited", "at": "05:00", "folder": str(self.folder),
            "model": "tier1", "prompt": "test", "timeout_seconds": 0,
        })
        self.assertEqual(job.timeout_s, 0)

    def test_stop_kills_process_group_and_cancels_retries(self):
        job = app.Job(
            name="cancel", at=clock_time(0, 0), folder=self.folder,
            model="tier1", prompt="test", permission_mode="plan",
            timeout_s=0, retries=3, retry_delay_s=1,
        )
        cfg = app.Config(
            tz=app.ZoneInfo("UTC"), claude_bin=self.fake_claude(ignore_term=True),
            catch_up_s=60, env={},
        )
        ctl = app.RunControl()
        result = {}
        with patch.object(app, "CLAUDE_PROJECTS", self.project_root):
            worker = threading.Thread(
                target=lambda: result.setdefault("value", app.run_job(cfg, job, ctl=ctl))
            )
            worker.start()
            deadline = time.monotonic() + 5
            while ctl.proc is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertIsNotNone(ctl.proc, "fake Claude did not start")
            direct = ctl.proc
            ctl.cancel()
            worker.join(15)
            self.assertFalse(worker.is_alive(), "cancel did not release communicate()")
            self.assertEqual(result["value"].detail, "stopped by user")
            with self.assertRaises(ProcessLookupError):
                os.killpg(direct.pid, 0)

    def test_stop_rejected_after_completed_process(self):
        ctl = app.RunControl()
        proc = app.subprocess.Popen(["/usr/bin/true"], start_new_session=True)
        proc.wait(timeout=2)
        ctl.attach(proc)
        self.assertFalse(ctl.finish_attempt(proc))
        self.assertFalse(ctl.cancel())

    def test_stop_can_cancel_retry_wait(self):
        ctl = app.RunControl()
        ctl.begin_retry_wait()
        self.assertTrue(ctl.cancel())
        self.assertTrue(ctl.event.is_set())
        ctl.event.wait(0.1)
        self.assertTrue(ctl.cancelled)


if __name__ == "__main__":
    unittest.main()
