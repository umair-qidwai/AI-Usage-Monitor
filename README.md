# AI Usage Monitor

Self-hosted Codex/OpenAI and Claude subscription usage dashboard, using Python,
FastAPI, Playwright and Chromium. Intended for Linux/Raspberry Pi with Python
3.11+. Unofficial; not affiliated with OpenAI or Anthropic. Provider pages and
response formats may change. Use only accounts you own or are authorized to access.

## How it works

The monitor observes responses from the official usage pages, normalizes five-hour
and weekly usage, persists the last values, and broadcasts updates over WebSocket.
It makes no model calls and does not directly poll undocumented API endpoints.
Claude's native Refresh button is attempted every **5 seconds** by the launcher.
Codex is passively checked every **5 seconds**; this is not an upstream refresh.
Silent data/control failures can trigger recovery page reloads, rate-limited to
**300 seconds** per provider (`0` disables fallback reloads).

The UI redraws countdowns every second. “Last checked” is collector activity,
not provider freshness. `/api/usage` separately reports `last_provider_update`
(valid usage response) and `last_detected_change` (changed usage/reset values).
An unchanged valid response still persists and broadcasts its fresh timestamp.
A connected browser or WebSocket does not prove the provider is authenticated.

## Install

Place the source at `~/ai-usage-monitor` (the supplied user-service convention):

```sh
sudo apt update
sudo apt install -y python3-venv chromium xvfb xauth
cd ~/ai-usage-monitor
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
chmod +x run-xvfb.sh
cp .env.example .env
chmod 600 .env
```

The system Chromium executable is used; a separate Playwright browser download
is unnecessary. Set `CHROMIUM_PATH` for your distribution. The launcher resolves
its directory, supports paths with spaces, and permits `PYTHON_BIN` overrides.
Only Linux/X11 operation is covered; macOS below is a remote login client.

## Authenticate with a visible browser

The persistent `browser-data/` profile must stay on the monitor host. Never run
two Chromium instances against it. Stop the service before interactive login.
Xvfb provides an invisible display, not a desktop you can interact with.

For a Mac client, install/start XQuartz, enable “Allow connections from network
clients” if needed, and restart XQuartz after changing that preference. In the
Mac terminal, permit localhost only (never use unrestricted `xhost +`):

```sh
DISPLAY=localhost:0 /opt/X11/bin/xdpyinfo >/dev/null
DISPLAY=localhost:0 /opt/X11/bin/xhost +localhost
DISPLAY=localhost:0 ssh -Y -o ControlPath=none -p717 <pi-user>@<pi-host>
```

Replace placeholders; change `717` if your SSH server uses another port. Trusted
X11 forwarding should only be used with a trusted host. `ControlPath=none` avoids
reusing a connection without X forwarding. On the Pi, verify `DISPLAY` is set,
then run:

```sh
systemctl --user stop ai-usage-monitor.service
cd ~/ai-usage-monitor
chromium --disable-dev-shm-usage --user-data-dir="$PWD/browser-data" \
  https://chatgpt.com/codex/settings/usage https://claude.ai/settings/usage
```

Log in normally in both visible provider tabs; handle any verification in the
browser. Close Chromium completely, then start the service. On a local Linux
desktop, the same Chromium command works without SSH/XQuartz. An uninstalled
service may report “not found” at the stop step; proceed with login.

## Run / enable at boot

```sh
mkdir -p ~/.config/systemd/user
cp systemd/ai-usage-monitor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ai-usage-monitor.service
sudo loginctl enable-linger "$USER"
systemctl --user status ai-usage-monitor.service
```

Both supplied service templates are identical. If installing elsewhere, edit
`WorkingDirectory`, `EnvironmentFile`, and `ExecStart` in the installed unit.
`.env` is read by systemd, not automatically by Python or the launcher. To run
manually (with the service stopped):

```sh
cd ~/ai-usage-monitor
set -a; . ./.env; set +a
./run-xvfb.sh
```

Open `http://127.0.0.1:8765/` on the host, or use an SSH tunnel:
`ssh -L 8765:127.0.0.1:8765 -p717 <pi-user>@<pi-host>`.
The sample binds loopback; without configuration the application defaults to
`0.0.0.0`. To expose it on a trusted LAN explicitly change `BIND_HOST` and apply
firewall restrictions. **There is no authentication or TLS on the dashboard/API.**
Do not expose this service directly to the Internet.

Configuration: `PORT` (8765), `CHROMIUM_PATH` (/usr/bin/chromium),
`CODEX_POLL_SECONDS` (5), `CLAUDE_REFRESH_SECONDS` (5 via launcher; 60 when
running Python directly without configuration), `PROVIDER_FALLBACK_SECONDS`
(300), `DISCOVER` (0), `LOG_LEVEL` (INFO), and optional provider URL overrides.
The Xvfb launcher enforces `HEADLESS=0` and uses a dynamically allocated display.

## Verify and troubleshoot

```sh
curl -fsS http://127.0.0.1:8765/api/health
curl -fsS http://127.0.0.1:8765/api/usage
journalctl --user -u ai-usage-monitor.service -n 50
.venv/bin/python -m unittest discover -s tests -v
sh -n run-xvfb.sh
```

Routes: `/` dashboard, `/api/usage` normalized state, `/api/health` collector
status, `/ws` live JSON. Tests are offline and isolate import-time state/profile
I/O in temporary directories; no authenticated profiles or provider calls.
Tests cover parsers, URL redaction, timestamp-only persistence/broadcast, and
launcher portability/defaults. They do not assert live provider availability.

A running service with stale `last_provider_update` can mean expired login,
a challenge, or changed provider formats. Inspect local traffic statuses and
re-authenticate visibly; do not bypass challenges. Browser crashes terminate
the process for systemd recovery. For missing `xauth`/`xvfb-run`, install the
packages above. Reload/restart systemd after changing its unit/configuration.

## Privacy and publication

`browser-data/` contains authenticated sessions. `state.json`, `traffic.ndjson`,
`.env`, logs, and backups are private and ignored by Git. Traffic logging runs
even with `DISCOVER=0`: it stores response URLs stripped of query/fragment,
status, JSON shapes and candidate usage/reset values, not full response bodies
or request authorization/cookie headers. URL paths may contain organization IDs;
candidate fields can contain sensitive values. Logs are **not anonymized** and
currently have no automatic rotation. Never publish them or journal excerpts
without review. The dashboard caches usage in browser localStorage too.

Keep runtime files owner-only, restrict host access, and manage log retention.
Do not upload a whole working directory or authenticated browser profile.
Before publication, stage an explicit source allowlist, inspect `git diff
--cached`, and scan the staged content. `.gitignore` does not remove secrets
already tracked. No account credentials or real provider response fixtures
are required by this project.

## License

MIT — Copyright (c) 2026 Umair Qidwai. See [LICENSE](LICENSE).
