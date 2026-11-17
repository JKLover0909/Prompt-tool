# prompt-tool

A small 24/7 service with a web UI. At the time you choose it sends a prompt to the **newest Claude Code session of a
folder**, through **9router** with a combo model (`tier1` / `tier2` / `tier3`). Use it to start the 5-hour quota
window early (for example at 05:00) so that it resets while you are working.

## How it works

At the scheduled time it runs, inside the job's folder:

```
claude --resume <session-id> -p --model <combo> --permission-mode <mode> --output-format json   # prompt on stdin
```

- Session = newest `*.jsonl` in `~/.claude/projects/<folder>`, or a fixed session you pick in the UI.
- 9router is reached through `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` in `~/.claude/settings.json`.
- The prompt and the reply are appended to that session, so they appear when you resume it.
- `~/.local/state/prompt-tool/state.json` records each job's last run: restarts never repeat or skip a run, and a run
  missed by more than `catch_up_minutes` is skipped, not replayed late. Results go to `runs.json` (last 200).
- Failed runs are retried (`retries`, `retry_delay_seconds`).

## Web UI

Open **http://127.0.0.1:8765** once the service is running. You can add, edit, enable/disable, delete and test-run jobs:

- **Folder**: picked from the folders Claude Code already has sessions for, or any absolute path.
- **Session**: newest of the folder (recommended) or a specific one, listed with its title and size.
- **Time, days, combo**: the combos are read from 9router's database (read-only), with the fallback chain shown.
- Everything is saved to `~/.config/prompt-tool/config.yaml` (previous version kept as `config.yaml.bak`) and applies
  immediately. You can also edit that file by hand.

The UI only listens on `127.0.0.1`, refuses requests with a foreign `Host`/`Origin`, and has no login: anyone who can
reach localhost on this machine can create jobs that run Claude, so do not expose the port.

## Install

Requires Python 3 with PyYAML (`python3 -c "import yaml"`) and `claude` on `PATH`.

```bash
mkdir -p ~/.config/systemd/user && cp systemd/prompt-tool.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now prompt-tool      # start now + start at every login/boot
```

`systemctl --user` manages services that run under your own account (no root). `enable` makes it start automatically,
`--now` also starts it immediately. With `loginctl enable-linger $USER` it keeps running when you are logged out.

```bash
systemctl --user status prompt-tool            # is it running?
journalctl --user -u prompt-tool -f            # live log (also ~/.local/state/prompt-tool/service.log)
systemctl --user restart prompt-tool           # after updating the code
systemctl --user disable --now prompt-tool     # stop and remove from autostart
```

Without systemd: `python3 -I prompt_tool.py serve`.

## CLI

```bash
python3 -I prompt_tool.py check                 # validate config, show next run + resolved session
python3 -I prompt_tool.py run <job> --dry-run   # show the exact command
python3 -I prompt_tool.py run <job>             # send it for real, once
```

## Notes

- `permission_mode: bypassPermissions` lets the prompt run tools without asking. For a pure quota warm-up use a harmless
  prompt, or `plan`.
- Resuming re-sends the whole conversation, so a big session costs more quota per ping than a small one. A small
  dedicated session per folder is cheaper.
- Do not use a session that you have open and active at the same time: two writers append to the same file.
- The machine must be awake at the scheduled time.
