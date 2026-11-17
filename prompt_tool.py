#!/usr/bin/env python3
"""prompt-tool: send a scheduled prompt to the latest Claude Code session of a folder.

Typical use: at 05:00 (when the 5-hour Claude quota window can be opened early)
resume the newest session of a project folder via ``claude --resume`` and send
one prompt through 9router using a combo model (tier1 / tier2 / tier3).

``serve`` runs the scheduler plus a small web UI on http://127.0.0.1:8765 where
jobs are created and edited. Run ``python3 prompt_tool.py --help`` for the CLI.
"""
from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import logging
import logging.handlers
import os
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo

import yaml

APP = "prompt-tool"
HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = Path.home() / ".config" / APP / "config.yaml"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / APP
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
ROUTER_DB = Path.home() / ".9router" / "db" / "data.sqlite"
DEFAULT_PORT = 8765
DEFAULT_HOST = "127.0.0.1"  # set web_host: 0.0.0.0 in the config to serve the LAN (login is always required)

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
PERMISSION_MODES = ["default", "acceptEdits", "plan", "bypassPermissions"]
FALLBACK_COMBOS = ["tier1", "tier2", "tier3"]

NAME_RE = re.compile(r"[\w][\w .-]{0,59}")
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/\[\]:-]{0,99}")
SESSION_RE = re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")

log = logging.getLogger(APP)


class ConfigError(Exception):
    pass


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


# --------------------------------------------------------------------------- config


@dataclass
class Job:
    name: str
    at: dtime
    folder: Path
    model: str
    prompt: str
    days: frozenset[int] = frozenset(range(7))  # 0 = Monday
    session_id: str | None = None  # None -> newest session of the folder
    permission_mode: str = "bypassPermissions"
    timeout_s: int = 600
    retries: int = 2
    retry_delay_s: int = 60
    enabled: bool = True


@dataclass
class Config:
    tz: ZoneInfo
    claude_bin: str
    catch_up_s: int
    env: dict[str, str]
    web_port: int = DEFAULT_PORT
    web_host: str = DEFAULT_HOST
    jobs: list[Job] = field(default_factory=list)


def _parse_days(value) -> frozenset[int]:
    if value in (None, "daily", "all"):
        return frozenset(range(7))
    if value == "weekdays":
        return frozenset(range(5))
    if value == "weekends":
        return frozenset({5, 6})
    if not isinstance(value, list) or not value:
        raise ConfigError(f"days must be 'daily', 'weekdays', 'weekends' or a list like [mon, wed]; got {value!r}")
    out = set()
    for d in value:
        key = str(d).strip().lower()[:3]
        if key not in WEEKDAYS:
            raise ConfigError(f"unknown weekday {d!r}")
        out.add(WEEKDAYS.index(key))
    return frozenset(out)


def _days_to_config(days: frozenset[int]):
    if days == frozenset(range(7)):
        return "daily"
    if days == frozenset(range(5)):
        return "weekdays"
    if days == frozenset({5, 6}):
        return "weekends"
    return [WEEKDAYS[d] for d in sorted(days)]


def _parse_time(value) -> dtime:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value).strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ConfigError(f"'at' must be HH:MM (24h), got {value!r}")
    return dtime(int(m[1]), int(m[2]))


def _int_in(j: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(j.get(key, default))
    except (TypeError, ValueError):
        raise ConfigError(f"'{key}' must be a number") from None
    if not lo <= v <= hi:
        raise ConfigError(f"'{key}' must be between {lo} and {hi}")
    return v


def parse_job(j) -> Job:
    if not isinstance(j, dict):
        raise ConfigError("job must be a mapping")
    for req in ("name", "at", "folder", "model", "prompt"):
        if not str(j.get(req) or "").strip():
            raise ConfigError(f"missing '{req}'")
    name = str(j["name"]).strip()
    if not NAME_RE.fullmatch(name):
        raise ConfigError("name: use letters, digits, space, '.', '_' or '-' (max 60 chars)")
    model = str(j["model"]).strip()
    if not MODEL_RE.fullmatch(model):
        raise ConfigError(f"invalid model/combo name {model!r}")
    prompt = str(j["prompt"])
    if len(prompt) > 20000:
        raise ConfigError("prompt is too long (max 20000 characters)")
    mode = j.get("permission_mode") or "bypassPermissions"
    if mode not in PERMISSION_MODES:
        raise ConfigError(f"permission_mode must be one of {PERMISSION_MODES}")
    folder = Path(str(j["folder"]).strip()).expanduser()
    if not folder.is_absolute():
        raise ConfigError(f"folder must be an absolute path, got {j['folder']!r}")
    sid = j.get("session_id") or None
    if sid is not None:
        sid = str(sid).strip()
        if not SESSION_RE.fullmatch(sid):
            raise ConfigError(f"session_id must be a UUID, got {sid!r}")
    return Job(
        name=name,
        at=_parse_time(j["at"]),
        folder=folder,
        model=model,
        prompt=prompt,
        days=_parse_days(j.get("days")),
        session_id=sid,
        permission_mode=mode,
        timeout_s=_int_in(j, "timeout_seconds", 600, 0, 7 * 86400),  # 0 = no time limit
        retries=_int_in(j, "retries", 2, 0, 5),
        retry_delay_s=_int_in(j, "retry_delay_seconds", 60, 0, 600),
        enabled=bool(j.get("enabled", True)),
    )


def job_to_dict(job: Job, *, compact_days: bool) -> dict:
    d = {
        "name": job.name,
        "enabled": job.enabled,
        "at": job.at.strftime("%H:%M"),
        "days": _days_to_config(job.days) if compact_days else [WEEKDAYS[d] for d in sorted(job.days)],
        "folder": str(job.folder),
        "model": job.model,
        "prompt": job.prompt,
        "permission_mode": job.permission_mode,
        "timeout_seconds": job.timeout_s,
        "retries": job.retries,
        "retry_delay_seconds": job.retry_delay_s,
    }
    if job.session_id:
        d["session_id"] = job.session_id
    return d


def parse_config(raw) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    try:
        tz = ZoneInfo(raw.get("timezone", "Asia/Ho_Chi_Minh"))
    except Exception:
        raise ConfigError(f"unknown timezone {raw.get('timezone')!r}") from None
    claude_bin = raw.get("claude_bin") or shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    env = {str(k): str(v) for k, v in (raw.get("env") or {}).items()}
    try:
        catch_up = int(raw.get("catch_up_minutes", 10)) * 60
        port = int(raw.get("web_port", DEFAULT_PORT))
    except (TypeError, ValueError):
        raise ConfigError("catch_up_minutes and web_port must be numbers") from None
    if not 0 <= port <= 65535:
        raise ConfigError("web_port must be between 0 (off) and 65535")
    host = str(raw.get("web_host", DEFAULT_HOST)).strip()
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        raise ConfigError(f"web_host must be an IPv4 address such as 127.0.0.1 or 0.0.0.0, got {host!r}") from None
    cfg = Config(tz=tz, claude_bin=claude_bin, catch_up_s=catch_up, env=env, web_port=port, web_host=host)
    names: set[str] = set()
    for i, j in enumerate(raw.get("jobs") or []):
        try:
            job = parse_job(j)
        except ConfigError as e:
            raise ConfigError(f"jobs[{i}]: {e}") from None
        if job.name in names:
            raise ConfigError(f"duplicate job name {job.name!r}")
        names.add(job.name)
        cfg.jobs.append(job)
    return cfg


def read_raw(path: Path) -> dict:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise ConfigError(f"config not found: {path}") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from None
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    return raw


def load_config(path: Path) -> Config:
    return parse_config(read_raw(path))


DEFAULT_CONFIG_TEXT = """\
# prompt-tool config. Jobs are normally managed from the web UI (http://127.0.0.1:8765).
timezone: Asia/Ho_Chi_Minh
catch_up_minutes: 10
web_port: 8765
web_host: 127.0.0.1   # 0.0.0.0 = reachable from the LAN (needs the access token)
jobs: []
"""


def ensure_config(path: Path) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
        log.info("created default config %s", path)


# ------------------------------------------------------------------------ schedule


def latest_due(job: Job, now: datetime) -> datetime | None:
    """Most recent scheduled fire time <= now (within the last 8 days)."""
    for back in range(0, 8):
        day = (now - timedelta(days=back)).date()
        if day.weekday() not in job.days:
            continue
        cand = datetime.combine(day, job.at, tzinfo=now.tzinfo)
        if cand <= now:
            return cand
    return None


def next_due(job: Job, now: datetime) -> datetime | None:
    for ahead in range(0, 8):
        day = (now + timedelta(days=ahead)).date()
        if day.weekday() not in job.days:
            continue
        cand = datetime.combine(day, job.at, tzinfo=now.tzinfo)
        if cand > now:
            return cand
    return None


# -------------------------------------------------------------------------- session


def project_dir(folder: Path) -> Path:
    """Claude Code stores sessions in ~/.claude/projects/<path with non-alphanumerics -> '-'>."""
    return CLAUDE_PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", str(folder.resolve()))


def newest_session(folder: Path) -> str:
    pdir = project_dir(folder)
    files = [p for p in pdir.glob("*.jsonl") if p.is_file()]
    if not files:
        raise RuntimeError(f"no Claude session found for {folder} (looked in {pdir})")
    return max(files, key=lambda p: p.stat().st_mtime).stem


def _first_cwd(pdir: Path) -> str | None:
    files = sorted((p for p in pdir.glob("*.jsonl") if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files[:3]:
        try:
            with f.open(encoding="utf-8") as fh:
                for _, line in zip(range(60), fh):
                    if '"cwd"' not in line:
                        continue
                    try:
                        cwd = json.loads(line).get("cwd")
                    except json.JSONDecodeError:
                        continue
                    if cwd:
                        return cwd
        except OSError:
            continue
    return None


def known_folders() -> list[dict]:
    """Folders Claude Code has sessions for, newest first."""
    out = []
    if not CLAUDE_PROJECTS.is_dir():
        return out
    for pdir in CLAUDE_PROJECTS.iterdir():
        if not pdir.is_dir():
            continue
        files = [p for p in pdir.glob("*.jsonl") if p.is_file()]
        if not files:
            continue
        cwd = _first_cwd(pdir)
        if not cwd or not Path(cwd).is_dir():
            continue
        try:
            if project_dir(Path(cwd)) != pdir:
                continue
        except OSError:
            continue
        out.append({
            "path": cwd,
            "name": Path(cwd).name or cwd,
            "sessions": len(files),
            "last_used": max(p.stat().st_mtime for p in files),
        })
    out.sort(key=lambda d: d["last_used"], reverse=True)
    return out


_title_cache: dict[tuple[str, float, int], str | None] = {}


def _session_title(path: Path, st: os.stat_result) -> str | None:
    key = (str(path), st.st_mtime, st.st_size)
    if key in _title_cache:
        return _title_cache[key]
    title = None
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if '"ai-title"' not in line and '"custom-title"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = d.get("customTitle") or d.get("aiTitle")
                if t:
                    title = str(t)
    except OSError:
        pass
    if len(_title_cache) > 500:
        _title_cache.clear()
    _title_cache[key] = title
    return title


def list_sessions(folder: Path) -> list[dict]:
    out = []
    for p in project_dir(folder).glob("*.jsonl"):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({"id": p.stem, "title": _session_title(p, st), "mtime": st.st_mtime, "size": st.st_size})
    out.sort(key=lambda d: d["mtime"], reverse=True)
    return out


def router_combos() -> list[dict]:
    """Combo names defined in 9router (read-only; nothing secret is read)."""
    try:
        con = sqlite3.connect(f"file:{ROUTER_DB}?mode=ro", uri=True, timeout=2)
        try:
            rows = con.execute("select name, models from combos order by name").fetchall()
        finally:
            con.close()
        out = []
        for name, models in rows:
            try:
                chain = [m if isinstance(m, str) else m.get("model", "?") for m in json.loads(models)]
            except (json.JSONDecodeError, AttributeError):
                chain = []
            out.append({"name": name, "chain": chain})
        if out:
            return out
    except (sqlite3.Error, OSError):
        pass
    return [{"name": n, "chain": []} for n in FALLBACK_COMBOS]


# ---------------------------------------------------------------------------- run


@dataclass
class Result:
    ok: bool
    detail: str
    session_id: str | None = None
    cost_usd: float | None = None


def _kill_group(p: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(p.pid, sig)  # the child leads its own session/process group
    except (ProcessLookupError, PermissionError):
        pass


def _terminate(p: subprocess.Popen, grace: float = 10.0) -> None:
    """SIGTERM the whole process group, SIGKILL it if it is still alive after `grace` seconds."""
    _kill_group(p, signal.SIGTERM)

    def reaper() -> None:
        # The direct child may exit on SIGTERM while grandchildren keep running.
        # Always kill the process group after grace; do not use p.poll() as the
        # completion signal for the whole tree.
        time.sleep(grace)
        _kill_group(p, signal.SIGKILL)

    threading.Thread(target=reaper, daemon=True).start()


class RunControl:
    """Lets another thread stop a running attempt or a retry wait."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.event = threading.Event()
        self.proc: subprocess.Popen | None = None
        self.retry_wait = False
        self.completed = False

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()

    def attach(self, p: subprocess.Popen) -> None:
        with self.lock:
            self.retry_wait = False
            self.completed = False
            self.proc = p
            cancelled = self.event.is_set()
        if cancelled:  # stop was requested before the process existed
            _terminate(p, grace=2)

    def finish_attempt(self, p: subprocess.Popen) -> bool:
        """Atomically close the stop window for this process and return cancelled state."""
        with self.lock:
            if self.proc is p:
                self.proc = None
                self.completed = True
            return self.event.is_set()

    def begin_retry_wait(self) -> None:
        with self.lock:
            self.retry_wait = True
            self.completed = False

    def cancel(self) -> bool:
        with self.lock:
            if self.completed or (self.proc is None and not self.retry_wait):
                return False
            self.event.set()
            p = self.proc
        if p is not None:
            _terminate(p)
        return True

    def shutdown(self) -> None:
        """Cancel work even if it is between attempts while the service stops."""
        with self.lock:
            self.event.set()
            p = self.proc
        if p is not None:
            _terminate(p)


def build_command(cfg: Config, job: Job, session_id: str) -> list[str]:
    return [
        cfg.claude_bin,
        "--resume", session_id,
        "-p",
        "--model", job.model,
        "--permission-mode", job.permission_mode,
        "--output-format", "json",
    ]  # the prompt itself is sent on stdin so it can never be parsed as a flag


def run_job_once(cfg: Config, job: Job, *, dry_run: bool = False, ctl: RunControl | None = None) -> Result:
    if not job.folder.is_dir():
        return Result(False, f"folder does not exist: {job.folder}")
    try:
        sid = job.session_id or newest_session(job.folder)
    except RuntimeError as e:
        return Result(False, str(e))

    cmd = build_command(cfg, job, sid)
    if dry_run:
        return Result(True, f"DRY RUN cwd={job.folder} cmd={' '.join(cmd)} stdin={job.prompt!r}", sid)

    env = {**os.environ, **cfg.env}
    try:
        p = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=job.folder, env=env, text=True, start_new_session=True,
        )
    except OSError as e:
        return Result(False, f"cannot execute {cfg.claude_bin}: {e}", sid)
    if ctl:
        ctl.attach(p)
    try:
        stdout, stderr = p.communicate(input=job.prompt, timeout=job.timeout_s or None)
    except subprocess.TimeoutExpired:
        _kill_group(p, signal.SIGKILL)
        p.communicate()
        cancelled = ctl.finish_attempt(p) if ctl else False
        if cancelled:
            return Result(False, "stopped by user", sid)
        return Result(False, f"timed out after {job.timeout_s}s", sid)
    cancelled = ctl.finish_attempt(p) if ctl else False
    if cancelled:
        return Result(False, "stopped by user", sid)

    out = (stdout or "").strip()
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        data = None
    if p.returncode != 0 or not isinstance(data, dict) or data.get("is_error"):
        msg = (data or {}).get("result") if isinstance(data, dict) else None
        msg = msg or (stderr or out or "no output").strip()
        return Result(False, f"exit={p.returncode}: {msg[:500]}", sid)
    return Result(True, str(data.get("result", ""))[:300].replace("\n", " "), data.get("session_id", sid), data.get("total_cost_usd"))


def run_job(cfg: Config, job: Job, *, dry_run: bool = False, ctl: RunControl | None = None) -> Result:
    res = Result(False, "not run")
    for attempt in range(1, job.retries + 2):
        if ctl and ctl.cancelled:
            return Result(False, "stopped by user")
        res = run_job_once(cfg, job, dry_run=dry_run, ctl=ctl)
        if res.ok:
            log.info("[%s] OK session=%s cost=%s reply=%s", job.name, res.session_id, res.cost_usd, res.detail)
            return res
        if ctl and ctl.cancelled:
            log.warning("[%s] stopped by user", job.name)
            return res
        log.error("[%s] attempt %d/%d failed: %s", job.name, attempt, job.retries + 1, res.detail)
        if attempt <= job.retries:
            if ctl:
                ctl.begin_retry_wait()
                ctl.event.wait(job.retry_delay_s)  # a stop request wakes the wait up
            else:
                time.sleep(job.retry_delay_s)
    return res


# ------------------------------------------------------------------------- state


class _JsonFile:
    def __init__(self, path: Path, default):
        self.path = path
        self.lock = threading.Lock()
        try:
            self.data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            self.data = default

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False))
        tmp.replace(self.path)


class State(_JsonFile):
    """Remembers when each job last fired so restarts neither skip nor repeat a run."""

    def __init__(self, path: Path):
        super().__init__(path, {})

    def get(self, name: str) -> datetime | None:
        v = self.data.get(name)
        return datetime.fromisoformat(v) if v else None

    def set(self, name: str, when: datetime) -> None:
        with self.lock:
            self.data[name] = when.isoformat()
            self._flush()


class History(_JsonFile):
    MAX = 200

    def __init__(self, path: Path):
        super().__init__(path, [])

    def add(self, entry: dict) -> None:
        with self.lock:
            self.data.append(entry)
            del self.data[: -self.MAX]
            self._flush()

    def recent(self, n: int = 50) -> list[dict]:
        with self.lock:
            return list(reversed(self.data[-n:]))

    def last_for(self, job: str) -> dict | None:
        with self.lock:
            for e in reversed(self.data):
                if e.get("job") == job:
                    return e
        return None


# ----------------------------------------------------------------------- service


def load_token(state_dir: Path, rotate: bool = False) -> str:
    """Web access token, stored 0600 in the state dir. Created on first use."""
    path = state_dir / "token"
    if not rotate:
        try:
            t = path.read_text().strip()
            if len(t) >= 16:
                return t
        except OSError:
            pass
    state_dir.mkdir(parents=True, exist_ok=True)
    t = secrets.token_urlsafe(24)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(t + "\n")
    os.replace(tmp, path)
    return t


class Service:
    def __init__(self, config_path: Path, state_dir: Path):
        self.config_path = config_path
        self.cfg = load_config(config_path)
        self.state = State(state_dir / "state.json")
        self.history = History(state_dir / "runs.json")
        self.token = load_token(state_dir)
        self.lock = threading.RLock()  # serialises config edits
        self.running: set[str] = set()
        self.controls: dict[str, RunControl] = {}
        self.running_lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=8)
        self.stop = threading.Event()
        self.mtime = config_path.stat().st_mtime

    # --- running jobs
    def start(self, job: Job, trigger: str) -> bool:
        with self.running_lock:
            if job.name in self.running:
                return False
            self.running.add(job.name)
            ctl = self.controls[job.name] = RunControl()
        self.pool.submit(self._run, job, trigger, ctl)
        return True

    def stop_job(self, name: str) -> bool:
        """Stop a running job (kills its process group and cancels its retries)."""
        with self.running_lock:
            ctl = self.controls.get(name)
            if ctl is None or not ctl.cancel():
                return False
        log.info("[%s] stop requested", name)
        return True

    def _run(self, job: Job, trigger: str, ctl: RunControl) -> None:
        try:
            log.info("[%s] running (%s)", job.name, trigger)
            res = run_job(self.cfg, job, ctl=ctl)
            self.history.add({
                "job": job.name, "at": datetime.now(self.cfg.tz).isoformat(timespec="seconds"),
                "trigger": trigger, "ok": res.ok, "detail": res.detail,
                "session_id": res.session_id, "cost_usd": res.cost_usd,
                "model": job.model, "folder": str(job.folder),
            })
        except Exception:
            log.exception("[%s] unexpected error", job.name)
        finally:
            with self.running_lock:
                self.running.discard(job.name)
                self.controls.pop(job.name, None)

    def is_running(self, name: str) -> bool:
        with self.running_lock:
            return name in self.running

    # --- scheduler
    def tick(self) -> None:
        cfg = self.cfg
        now = datetime.now(cfg.tz)
        for job in cfg.jobs:
            last = self.state.get(job.name)
            if last is None:  # new job: never replay the past
                self.state.set(job.name, now)
                continue
            if not job.enabled:
                continue
            due = latest_due(job, now)
            if due is None or last >= due:
                continue
            self.state.set(job.name, now)  # mark first: a crash must not cause a double send
            late = (now - due).total_seconds()
            if late > cfg.catch_up_s:
                msg = f"missed {due.isoformat(timespec='minutes')} by {int(late)}s (> catch_up), skipped"
                log.warning("[%s] %s", job.name, msg)
                self.history.add({"job": job.name, "at": now.isoformat(timespec="seconds"), "trigger": "schedule",
                                  "ok": False, "detail": msg, "model": job.model, "folder": str(job.folder)})
            elif not self.start(job, "schedule"):
                log.warning("[%s] still running from the previous trigger, skipped", job.name)

    def reload_if_changed(self) -> None:
        try:
            m = self.config_path.stat().st_mtime
            if m == self.mtime:
                return
            with self.lock:
                self.mtime = m
                self.cfg = load_config(self.config_path)
            log.info("config reloaded: %d job(s)", len(self.cfg.jobs))
        except (OSError, ConfigError) as e:
            log.error("config reload failed, keeping the previous one: %s", e)

    # --- config edits (used by the web UI)
    def mutate_jobs(self, fn, touched: str | None = None) -> Config:
        """fn(list[dict]) -> list[dict]; validates, writes the file (with .bak) and swaps the live config."""
        with self.lock:
            raw = read_raw(self.config_path)
            jobs = [job_to_dict(j, compact_days=True) for j in self.cfg.jobs]  # normalise from the live config
            raw["jobs"] = fn(jobs)
            cfg = parse_config(raw)
            text = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, width=120)
            if self.config_path.exists():
                shutil.copy2(self.config_path, self.config_path.with_suffix(".yaml.bak"))
            tmp = self.config_path.with_suffix(".yaml.tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self.config_path)
            self.mtime = self.config_path.stat().st_mtime
            self.cfg = cfg
        if touched:  # an edited job must not fire for a time that has already passed today
            self.state.set(touched, datetime.now(cfg.tz))
        return cfg

    def find(self, name: str) -> Job:
        for j in self.cfg.jobs:
            if j.name == name:
                return j
        raise NotFound(f"no such job {name!r}")

    # --- views
    def job_view(self, job: Job) -> dict:
        now = datetime.now(self.cfg.tz)
        nxt = next_due(job, now) if job.enabled else None
        return {
            **job_to_dict(job, compact_days=False),
            "next_run": nxt.isoformat(timespec="minutes") if nxt else None,
            "running": self.is_running(job.name),
            "last_run": self.history.last_for(job.name),
            "folder_exists": job.folder.is_dir(),
        }

    def state_view(self) -> dict:
        cfg = self.cfg
        return {
            "now": datetime.now(cfg.tz).isoformat(timespec="seconds"),
            "timezone": str(cfg.tz),
            "config_path": str(self.config_path),
            "jobs": [self.job_view(j) for j in cfg.jobs],
            "permission_modes": PERMISSION_MODES,
        }


def _check_job_payload(body, svc: Service) -> dict:
    if not isinstance(body, dict):
        raise ConfigError("expected a JSON object")
    job = parse_job(body)
    if not job.folder.is_dir():
        raise ConfigError(f"folder does not exist: {job.folder}")
    if job.session_id and not (project_dir(job.folder) / f"{job.session_id}.jsonl").is_file():
        raise ConfigError("that session does not belong to this folder")
    return job_to_dict(job, compact_days=True)


# --------------------------------------------------------------------------- web


def make_handler(svc: Service):
    index = HERE / "web" / "index.html"
    login_page = HERE / "web" / "login.html"
    # Stateless session cookie derived from the token: rotating the token logs everyone out.
    session_cookie = hmac.new(svc.token.encode(), b"prompt-tool-session", "sha256").hexdigest()
    fails: dict[str, list[float]] = {}  # client ip -> monotonic times of recent failed logins
    fails_lock = threading.Lock()
    MAX_FAILS, FAIL_WINDOW_S = 5, 300

    class Handler(BaseHTTPRequestHandler):
        server_version = APP

        def log_message(self, fmt, *args):  # route http.server noise to debug
            log.debug("web %s", fmt % args)

        def _send(self, code: int, payload=None, *, body: bytes | None = None, ctype="application/json; charset=utf-8",
                  headers: dict | None = None):
            if body is None:
                body = json.dumps(payload if payload is not None else {}, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 200_000:
                raise ConfigError("request too large")
            if not n:
                return None
            try:
                return json.loads(self.rfile.read(n))
            except json.JSONDecodeError:
                raise ConfigError("body is not valid JSON") from None

        def _dispatch(self, method: str):
            try:
                # Cross-site protection: a state-changing request must come from our own page (same origin).
                # DNS rebinding is covered by the login cookie, which a foreign hostname never carries.
                if method != "GET":
                    origin = self.headers.get("Origin")
                    host = self.headers.get("Host") or ""
                    if (origin and origin != f"http://{host}") or self.headers.get("X-Requested-With") != APP:
                        return self._send(403, {"error": "cross-site request refused"})
                url = urlsplit(self.path)
                parts = [unquote(p) for p in url.path.strip("/").split("/") if p]
                query = parse_qs(url.query)
                self._route(method, parts, query)
            except ConfigError as e:
                self._send(400, {"error": str(e)})
            except NotFound as e:
                self._send(404, {"error": str(e)})
            except Conflict as e:
                self._send(409, {"error": str(e)})
            except Exception:
                log.exception("web request failed: %s %s", method, self.path)
                self._send(500, {"error": "internal error, see the service log"})

        def _authed(self) -> bool:
            raw = self.headers.get("Cookie")
            if not raw:
                return False
            try:
                morsel = SimpleCookie(raw).get("pt_session")
            except CookieError:
                return False
            return bool(morsel) and hmac.compare_digest(morsel.value.encode(), session_cookie.encode())

        def _login(self):
            ip = self.client_address[0]
            now = time.monotonic()
            with fails_lock:
                if len(fails) > 1000:
                    fails.clear()
                recent = [t for t in fails.get(ip, []) if now - t < FAIL_WINDOW_S]
                fails[ip] = recent
                if len(recent) >= MAX_FAILS:
                    return self._send(429, {"error": "Nhập sai quá nhiều lần, thử lại sau vài phút."})
            body = self._body()
            token = str(body.get("token", "")) if isinstance(body, dict) else ""
            if not hmac.compare_digest(token.encode(), svc.token.encode()):
                with fails_lock:
                    fails.setdefault(ip, []).append(now)
                log.warning("web: failed login from %s", ip)
                return self._send(401, {"error": "Mã truy cập không đúng."})
            with fails_lock:
                fails.pop(ip, None)
            log.info("web: login from %s", ip)
            cookie = f"pt_session={session_cookie}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000"
            self._send(200, {"ok": True}, headers={"Set-Cookie": cookie})

        def _route(self, method, parts, query):
            if method == "POST" and parts == ["api", "login"]:
                return self._login()
            if not self._authed():
                if method == "GET" and not parts:
                    return self._send(200, body=login_page.read_bytes(), ctype="text/html; charset=utf-8")
                return self._send(401, {"error": "login required"})
            if method == "POST" and parts == ["api", "logout"]:
                return self._send(200, {"ok": True}, headers={"Set-Cookie": "pt_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
            if method == "GET" and not parts:
                return self._send(200, body=index.read_bytes(), ctype="text/html; charset=utf-8")
            if not parts or parts[0] != "api":
                raise NotFound("not found")
            route = parts[1:]
            if method == "GET":
                if route == ["state"]:
                    return self._send(200, svc.state_view())
                if route == ["combos"]:
                    return self._send(200, router_combos())
                if route == ["folders"]:
                    return self._send(200, known_folders())
                if route == ["history"]:
                    return self._send(200, svc.history.recent(60))
                if route == ["sessions"]:
                    folder = Path((query.get("folder") or [""])[0]).expanduser()
                    if not folder.is_absolute() or not folder.is_dir():
                        raise ConfigError("folder must be an existing absolute path")
                    return self._send(200, list_sessions(folder))
                raise NotFound("not found")
            if route == ["jobs"] and method == "POST":
                new = _check_job_payload(self._body(), svc)

                def add(jobs):
                    if any(j["name"] == new["name"] for j in jobs):
                        raise Conflict(f"a job named {new['name']!r} already exists")
                    return jobs + [new]

                svc.mutate_jobs(add, touched=new["name"])
                log.info("web: created job %s", new["name"])
                return self._send(201, svc.job_view(svc.find(new["name"])))
            if len(route) >= 2 and route[0] == "jobs":
                name = route[1]
                if len(route) == 3 and route[2] == "stop" and method == "POST":
                    svc.find(name)
                    if not svc.stop_job(name):
                        raise Conflict("this job is not running")
                    return self._send(202, {"stopping": True})
                if len(route) == 3 and route[2] == "run" and method == "POST":
                    job = svc.find(name)
                    if not svc.start(job, "manual"):
                        raise Conflict("this job is already running")
                    return self._send(202, {"started": True})
                if len(route) == 2 and method == "PUT":
                    new = _check_job_payload(self._body(), svc)
                    svc.find(name)
                    if svc.is_running(name) and new["name"] != name:
                        raise Conflict("stop the running task before renaming this job")

                    def edit(jobs):
                        if svc.is_running(name):
                            raise Conflict("stop the running task before editing this job")

                        if new["name"] != name and any(j["name"] == new["name"] for j in jobs):
                            raise Conflict(f"a job named {new['name']!r} already exists")
                        return [new if j["name"] == name else j for j in jobs]

                    svc.mutate_jobs(edit, touched=new["name"])
                    log.info("web: updated job %s", name)
                    return self._send(200, svc.job_view(svc.find(new["name"])))
                if len(route) == 2 and method == "DELETE":
                    svc.find(name)
                    if svc.is_running(name):
                        raise Conflict("stop the running task before deleting this job")
                    svc.mutate_jobs(lambda jobs: [j for j in jobs if j["name"] != name])
                    log.info("web: deleted job %s", name)
                    return self._send(200, {"deleted": name})
            raise NotFound("not found")

        def do_GET(self): self._dispatch("GET")
        def do_POST(self): self._dispatch("POST")
        def do_PUT(self): self._dispatch("PUT")
        def do_DELETE(self): self._dispatch("DELETE")

    return Handler


def start_web(svc: Service, host: str, port: int) -> ThreadingHTTPServer | None:
    if not port:
        return None
    try:
        httpd = ThreadingHTTPServer((host, port), make_handler(svc))
    except OSError as e:
        log.error("web UI disabled: cannot listen on %s:%d (%s)", host, port, e)
        return None
    threading.Thread(target=httpd.serve_forever, name="web", daemon=True).start()
    log.info("web UI: http://%s:%d (login with the token from `prompt_tool.py token`)", host, port)
    if not ipaddress.IPv4Address(host).is_loopback:
        log.warning("web UI is reachable from the network over plain HTTP; use a trusted network or Tailscale")
    return httpd


def serve(config_path: Path, state_dir: Path) -> None:
    svc = Service(config_path, state_dir)
    def request_shutdown(*_):
        # Signal handlers must remain signal-safe: the main loop cancels jobs below.
        svc.stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, request_shutdown)

    cfg = svc.cfg
    start = datetime.now(cfg.tz)
    log.info("started: %d job(s), tz=%s, claude=%s", len(cfg.jobs), cfg.tz, cfg.claude_bin)
    for job in cfg.jobs:
        log.info("  %-24s %s %-8s -> %s  next: %s%s", job.name, job.at.strftime("%H:%M"), job.model, job.folder,
                 next_due(job, start), "" if job.enabled else "  (disabled)")
    httpd = start_web(svc, cfg.web_host, cfg.web_port)

    while not svc.stop.is_set():
        svc.tick()
        svc.reload_if_changed()
        svc.stop.wait(15)

    # Restart/stop should not wait forever for tasks with timeout_seconds=0.
    with svc.running_lock:
        for ctl in svc.controls.values():
            ctl.shutdown()
    log.info("stopping; cancelling running jobs and waiting for cleanup")
    if httpd:
        httpd.shutdown()
    svc.pool.shutdown(wait=True)


# --------------------------------------------------------------------------- CLI


def setup_logging(verbose: bool, to_file: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger(APP)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if to_file:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(STATE_DIR / "service.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog=APP, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", type=Path, default=DEFAULT_CONFIG, help=f"config file (default: {DEFAULT_CONFIG})")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the scheduler and the web UI (24/7 service)")
    sub.add_parser("check", help="validate the config and show each job's next run + resolved session")
    t = sub.add_parser("token", help="print the web UI access token (created on first use)")
    t.add_argument("--rotate", action="store_true", help="generate a new token (restart the service afterwards)")
    r = sub.add_parser("run", help="run one job right now (for testing)")
    r.add_argument("name")
    r.add_argument("--dry-run", action="store_true", help="print the command instead of running it")
    args = ap.parse_args(argv)
    if args.cmd == "token":
        print(load_token(STATE_DIR, rotate=args.rotate))
        return 0

    setup_logging(args.verbose, to_file=args.cmd == "serve")
    try:
        if args.cmd == "serve":
            ensure_config(args.config)
        cfg = load_config(args.config)
    except ConfigError as e:
        log.error("%s", e)
        return 2

    if args.cmd == "serve":
        serve(args.config, STATE_DIR)
        return 0

    if args.cmd == "check":
        now = datetime.now(cfg.tz)
        bad = 0
        if not Path(cfg.claude_bin).exists():
            log.error("claude binary not found: %s", cfg.claude_bin)
            bad += 1
        for job in cfg.jobs:
            try:
                if not job.folder.is_dir():
                    raise RuntimeError(f"folder does not exist: {job.folder}")
                status = f"session={job.session_id or newest_session(job.folder)}"
            except RuntimeError as e:
                status, bad = f"PROBLEM: {e}", bad + 1
            print(f"{job.name:24} {job.at.strftime('%H:%M')} {job.model:8} next={next_due(job, now)}  {status}")
        return 1 if bad else 0

    job = next((j for j in cfg.jobs if j.name == args.name), None)
    if not job:
        log.error("no such job %r; have: %s", args.name, ", ".join(j.name for j in cfg.jobs))
        return 2
    return 0 if run_job(cfg, job, dry_run=args.dry_run).ok else 1


if __name__ == "__main__":
    sys.exit(main())
