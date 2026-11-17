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
- Failed runs are retried (`retries`, `retry_delay_seconds`). Set `timeout_seconds: 0` for no per-run limit; the web UI has a stop button for running jobs.
- Stopping a job terminates its Claude process tree and cancels remaining retries. Service shutdown/restart also cancels running jobs; unlimited tasks cannot block a restart forever.

## Web UI

Open **http://127.0.0.1:8765** once the service is running (log in with the token, see below). You can add, edit, enable/disable, delete and test-run jobs:

- **Folder**: picked from the folders Claude Code already has sessions for, or any absolute path.
- **Session**: newest of the folder (recommended) or a specific one, listed with its title and size.
- **Time, days, combo**: the combos are read from 9router's database (read-only), with the fallback chain shown.
- Everything is saved to `~/.config/prompt-tool/config.yaml` (previous version kept as `config.yaml.bak`) and applies
  immediately. You can also edit that file by hand.

### Access from other machines and login

`web_host` in the config selects where the UI listens: `127.0.0.1` (default, this machine only) or `0.0.0.0` (every
network interface, so other machines can open `http://<this-machine-ip>:8765`). Login with an access token is
**always** required:

```bash
python3 -I prompt_tool.py token            # print the token (created on first use, stored 0600 in ~/.local/state/prompt-tool/token)
python3 -I prompt_tool.py token --rotate   # new token; restart the service, all browsers are logged out
```

- The browser keeps a 30-day `HttpOnly`, `SameSite=Strict` cookie; 5 wrong tokens from one address lock it for 5 minutes.
- State-changing requests must come from the page itself (same-origin check).
- Traffic is **plain HTTP**: the token and cookie are readable by anyone on the same network segment. Use it only on a
  network you trust, or reach it through Tailscale/VPN, or put an HTTPS reverse proxy in front.
- Anyone who logs in can create jobs that run Claude with the permissions you allow in the job, so treat the token like
  a password.

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
