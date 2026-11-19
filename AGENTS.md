# AGENTS.md — prompt-tool

Hướng dẫn cho AI coding agent làm việc trong repo này. Giữ file `CLAUDE.md` đồng bộ với file này.

## Mục đích
Dịch vụ chạy 24/7 kèm web UI: đúng giờ đã đặt, gửi một prompt vào **session Claude Code mới nhất của một thư mục**
qua 9router (combo `tier1`/`tier2`/`tier3`), để mở sớm cửa sổ quota 5 giờ. Chi tiết dùng: `README.md`.

## Cấu trúc
- `prompt_tool.py` — toàn bộ logic (một file, chỉ dùng stdlib + PyYAML): parse config, lịch (`latest_due`/`next_due`),
  tìm session, chạy `claude --resume ... -p`, `RunControl` (dừng job, kill cả process group), `State`/`History`,
  token đăng nhập web, HTTP handler, CLI (`serve`, `check`, `run`, `token`).
- `web/index.html`, `web/login.html` — giao diện, không build step.
- `config.example.yaml` — mẫu cấu hình; cấu hình thật ở `~/.config/prompt-tool/config.yaml` (đừng commit).
- `systemd/prompt-tool.service` — user unit.
- `tests/test_cancellation.py` — unittest.

## Lệnh
```bash
python3 -I -m unittest discover -s tests   # chạy test
python3 -I prompt_tool.py check            # kiểm tra config
python3 -I prompt_tool.py run <job> --dry-run
python3 -I prompt_tool.py serve
```

## Quy tắc
- Giữ phụ thuộc tối thiểu (stdlib + PyYAML); không thêm framework web.
- Luôn chạy Python với `-I`.
- Không bao giờ commit `config.yaml`, token (`~/.local/state/prompt-tool/token`), hay `state.json`/`runs.json`.
- Web UI luôn yêu cầu token; giữ kiểm tra same-origin, giới hạn thử sai, `HttpOnly` + `SameSite=Strict`.
- Dừng job phải kill cả cây process của `claude` và huỷ các lần retry còn lại; không để task không giới hạn chặn restart.
- Sửa logic lịch hoặc chạy job thì thêm/cập nhật test trong `tests/`.
- Commit nhỏ, thông điệp theo kiểu `feat:`/`fix:`/`docs:`.
