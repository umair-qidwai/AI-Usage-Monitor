import asyncio
import json
import logging
import os
import re
import signal
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from playwright.async_api import BrowserContext, Page, async_playwright

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "browser-data"
STATE_FILE = ROOT / "state.json"
LOG_FILE = ROOT / "traffic.ndjson"
TRAFFIC_LOG_MAX_BYTES = max(0, int(os.getenv("TRAFFIC_LOG_MAX_BYTES", str(10 * 1024 * 1024))))
DATA.mkdir(exist_ok=True)

CODEX_URL = os.getenv("CODEX_USAGE_URL", "https://chatgpt.com/codex/settings/usage")
CLAUDE_URL = os.getenv("CLAUDE_USAGE_URL", "https://claude.ai/settings/usage")
HEADLESS = os.getenv("HEADLESS", "1") not in {"0", "false", "no"}
DISCOVER = os.getenv("DISCOVER", "0") in {"1", "true", "yes"}
CODEX_POLL_SECONDS = max(1, int(os.getenv("CODEX_POLL_SECONDS", "5")))
CLAUDE_REFRESH_SECONDS = max(1, int(os.getenv("CLAUDE_REFRESH_SECONDS", "60")))

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ai-usage-monitor")

EMPTY = {"five_hour_used": None, "five_hour_remaining": None, "weekly_used": None,
         "weekly_remaining": None, "five_hour_reset": None, "weekly_reset": None,
         "last_provider_update": None, "last_detected_change": None}

def initial_state():
    return {"codex": dict(EMPTY), "claude": dict(EMPTY)}

try:
    state = json.loads(STATE_FILE.read_text())
except Exception:
    state = initial_state()
for provider in ("codex", "claude"):
    state.setdefault(provider, {}).update({k: v for k, v in EMPTY.items() if k not in state[provider]})

state_lock = asyncio.Lock()
clients: set[WebSocket] = set()
health = {"browser": "starting", "codex": "starting", "claude": "starting", "last_error": None}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def safe_url(url: str) -> str:
    # Query strings and challenge paths can contain tokens; keep only safe URL metadata.
    parts = urlsplit(url)
    path = parts.path
    if "/cdn-cgi/" in path or parts.netloc.endswith("challenges.cloudflare.com"):
        path = "/<redacted-challenge>"
    return f"{parts.scheme}://{parts.netloc}{path}"


def shape(value: Any, depth=0):
    if depth > 2:
        return type(value).__name__
    if isinstance(value, dict):
        return {str(k): shape(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return [shape(value[0], depth + 1)] if value else []
    if isinstance(value, (str, int, float, bool)) or value is None:
        return type(value).__name__
    return type(value).__name__


def extract_candidate(payload: Any):
    """Conservative discovery parser; exact mappings are recorded after traffic inspection."""
    found = {}
    def walk(v, path=""):
        if isinstance(v, dict):
            for k, x in v.items():
                key = str(k).lower()
                p = f"{path}.{k}" if path else str(k)
                if any(t in key for t in ("reset", "renew", "expires")) and isinstance(x, (str, int, float)):
                    found.setdefault("reset_candidates", []).append((p, x))
                if any(t in key for t in ("percent", "percentage", "utilization", "used", "remaining", "usage")) and isinstance(x, (str, int, float)):
                    found.setdefault("usage_candidates", []).append((p, x))
                walk(x, p)
        elif isinstance(v, list):
            for i, x in enumerate(v[:20]): walk(x, f"{path}[{i}]")
    walk(payload)
    return found


def iso_timestamp(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    return str(value)


def provider_response_error(provider, status, url):
    """Classify an access denial on a provider's official usage page."""
    if provider == "codex" and status in {401, 403} and "/codex/settings/usage" in url:
        return "needs_login"
    return None


def provider_updates(provider, url, body):
    """Parse only provider response shapes observed in traffic discovery."""
    out = {}
    path = urlsplit(url).path.rstrip("/")
    if provider == "codex" and path.endswith("/backend-api/wham/usage"):
        windows = (body.get("rate_limit") or {})
        for key, window in (("primary_window", windows.get("primary_window")),
                            ("secondary_window", windows.get("secondary_window"))):
            if not isinstance(window, dict):
                continue
            seconds = window.get("limit_window_seconds")
            prefix = "five_hour" if seconds == 18000 else "weekly" if seconds == 604800 else None
            if not prefix or window.get("used_percent") is None:
                continue
            used = float(window["used_percent"])
            out[prefix + "_used"] = used
            out[prefix + "_remaining"] = max(0.0, 100.0 - used)
            out[prefix + "_reset"] = iso_timestamp(window.get("reset_at"))
    elif provider == "claude" and "/api/organizations/" in path and path.endswith("/usage"):
        for source, prefix in (("five_hour", "five_hour"), ("seven_day", "weekly")):
            window = body.get(source) or {}
            if not isinstance(window, dict) or window.get("utilization") is None:
                continue
            used = float(window["utilization"])
            out[prefix + "_used"] = used
            out[prefix + "_remaining"] = max(0.0, 100.0 - used)
            out[prefix + "_reset"] = iso_timestamp(window.get("resets_at"))
    return out


def append_traffic(record):
    if TRAFFIC_LOG_MAX_BYTES and LOG_FILE.exists() and LOG_FILE.stat().st_size >= TRAFFIC_LOG_MAX_BYTES:
        rotated = LOG_FILE.with_suffix(LOG_FILE.suffix + ".1")
        LOG_FILE.replace(rotated)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=True) + "\n")


async def broadcast():
    message = json.dumps(state)
    dead = []
    for ws in clients:
        try: await ws.send_text(message)
        except Exception: dead.append(ws)
    for ws in dead: clients.discard(ws)


async def set_provider(provider, updates):
    changed = False
    provider_update_changed = False
    async with state_lock:
        for key, value in updates.items():
            if key == "last_provider_update":
                continue
            if value is not None and state[provider].get(key) != value:
                state[provider][key] = value
                changed = True
        if updates.get("last_provider_update"):
            if state[provider].get("last_provider_update") != updates["last_provider_update"]:
                state[provider]["last_provider_update"] = updates["last_provider_update"]
                provider_update_changed = True
        if changed:
            state[provider]["last_detected_change"] = utc_now()
        if changed or provider_update_changed:
            STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")
    # Broadcast timestamp-only provider responses too, so the dashboard age stays live
    # even when the usage percentages themselves have not changed.
    if changed or provider_update_changed:
        await broadcast()


def normalize_percent(value):
    if isinstance(value, str):
        m = re.search(r"(-?\d+(?:\.\d+)?)\s*%", value)
        if m: return float(m.group(1))
        try: value = float(value)
        except ValueError: return None
    if isinstance(value, (int, float)):
        return float(value * 100 if 0 <= value <= 1 else value)
    return None


def infer_updates(payload):
    # This intentionally handles only common shapes; traffic.ndjson is authoritative for refining mappings.
    out = {}
    def walk(v, labels=()):
        if isinstance(v, dict):
            for k, x in v.items(): walk(x, labels + (str(k).lower(),))
        elif isinstance(v, list):
            for x in v: walk(x, labels)
        else:
            text = " ".join(labels)
            pct = normalize_percent(v)
            if pct is None: return
            window = "weekly" if any(x in text for x in ("week", "7_day", "seven")) else "five_hour" if any(x in text for x in ("5_hour", "five_hour", "five-hour", "5h")) else None
            if not window: return
            if any(x in text for x in ("remaining", "left", "available")): out[window + "_remaining"] = pct
            elif any(x in text for x in ("used", "utilization", "usage", "percent")): out[window + "_used"] = pct
    walk(payload)
    return out


async def attach_page(page: Page, provider: str):
    # A worker reconnect can rediscover the same page; do not stack response handlers.
    if getattr(page, "_usage_monitor_attached", False):
        return
    page._usage_monitor_attached = True
    health[provider] = "waiting_for_login"
    await page.add_init_script("""
      (() => { window.__usage_monitor_mutations = []; const o = new MutationObserver(() => {
        window.__usage_monitor_mutations.push(Date.now());
      }); o.observe(document.documentElement, {subtree:true, childList:true, characterData:true}); })();
    """)
    async def response_handler(response):
        if response.request.resource_type not in {"fetch", "xhr", "document"}:
            return
        record = {"ts": utc_now(), "provider": provider, "kind": "response", "url": safe_url(response.url),
                  "status": response.status, "content_type": response.headers.get("content-type", "")}
        access_error = provider_response_error(provider, response.status, response.url)
        if access_error:
            health[provider] = access_error
            health["last_error"] = f"{provider}: HTTP {response.status} on usage page"
        try:
            if "json" in record["content_type"]:
                body = await response.json()
                record["json_shape"] = shape(body)
                record["candidates"] = extract_candidate(body)
                updates = provider_updates(provider, response.url, body)
                if updates:
                    updates["last_provider_update"] = utc_now()
                    await set_provider(provider, updates)
        except Exception as exc:
            record["body_read"] = type(exc).__name__
        append_traffic(record)
        log.info("%s response %s %s", provider, record["status"], record["url"])
    page.on("response", response_handler)
    page.on("crash", lambda: log.error("%s page crashed", provider))


async def click_claude_refresh(page: Page):
    """Use Claude's visible refresh control instead of reloading the whole app."""
    try:
        refresh = page.get_by_role("button", name=re.compile(r"refresh", re.I)).first
        if await refresh.count() and await refresh.is_visible():
            await refresh.click(timeout=10000)
            log.info("claude usage refresh button clicked")
            return True
    except Exception as exc:
        log.info("claude refresh button unavailable: %s", type(exc).__name__)
    return False


async def provider_worker(provider, url):
    global browser_context
    # Providers update on their own schedule. Claude has a visible refresh action;
    # Codex is observed passively and only gets a slow recovery reload if silent.
    fallback_seconds = max(0, int(os.getenv("PROVIDER_FALLBACK_SECONDS", "300")))
    action_seconds = CODEX_POLL_SECONDS if provider == "codex" else CLAUDE_REFRESH_SECONDS
    next_action = ((time.time() // action_seconds) + 1) * action_seconds
    next_fallback_at = time.monotonic()
    while True:
        try:
            page = next((p for p in browser_context.pages if provider in (p.url or "")), None)
            if not page:
                page = await browser_context.new_page()
                await attach_page(page, provider)
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            else:
                await attach_page(page, provider)
            health[provider] = "connected"
            while not page.is_closed():
                if next_action is None:
                    await asyncio.sleep(5)
                    continue
                await asyncio.sleep(max(0, next_action - time.time()))
                if time.time() < next_action:
                    continue
                # Record the collector check, not provider-data freshness.
                async with state_lock:
                    state[provider]["last_checked"] = utc_now()
                await broadcast()
                last_update = state.get(provider, {}).get("last_provider_update")
                age = float("inf")
                if last_update:
                    try:
                        age = (datetime.now(timezone.utc) - datetime.fromisoformat(last_update)).total_seconds()
                    except ValueError:
                        pass
                if provider == "claude":
                    clicked = await click_claude_refresh(page)
                    if not clicked and fallback_seconds and age >= fallback_seconds and time.monotonic() >= next_fallback_at:
                        log.info("claude refresh control unavailable; recovery reload after %.0fs", age)
                        await page.reload(wait_until="domcontentloaded", timeout=60000)
                        next_fallback_at = time.monotonic() + fallback_seconds
                elif fallback_seconds and age >= fallback_seconds and time.monotonic() >= next_fallback_at:
                    log.info("codex passive data stale for %.0fs; recovery reload", age)
                    await page.reload(wait_until="domcontentloaded", timeout=60000)
                    next_fallback_at = time.monotonic() + fallback_seconds
                next_action += action_seconds
                if DISCOVER:
                    log.info("%s open; passive observation active", provider)
            raise RuntimeError("page closed")
        except Exception as exc:
            message = str(exc)
            health[provider] = "needs_login" if "login" in message.lower() or "auth" in message.lower() else "reconnecting"
            health["last_error"] = f"{provider}: {type(exc).__name__}"
            log.warning("%s worker: %s", provider, exc)
            # A kernel-killed Chromium leaves Playwright alive but unusable.
            # Exit so systemd Restart=always creates a fresh browser context.
            if "browser has been closed" in message.lower() or "targetclosederror" in type(exc).__name__.lower():
                log.error("browser context is gone; exiting for supervised recovery")
                os.kill(os.getpid(), signal.SIGTERM)
                return
            await asyncio.sleep(5)
            next_action = ((time.time() // action_seconds) + 1) * action_seconds

browser_context: BrowserContext | None = None
playwright_instance = None
workers = []

@asynccontextmanager
async def lifespan(app):
    global browser_context, playwright_instance, workers
    playwright_instance = await async_playwright().start()
    browser_context = await playwright_instance.chromium.launch_persistent_context(
        str(DATA), headless=HEADLESS, executable_path=os.getenv("CHROMIUM_PATH", "/usr/bin/chromium"),
        args=["--disable-dev-shm-usage"], viewport={"width": 1280, "height": 900})
    health["browser"] = "running"
    workers = [asyncio.create_task(provider_worker("codex", CODEX_URL)), asyncio.create_task(provider_worker("claude", CLAUDE_URL))]
    try: yield
    finally:
        for task in workers: task.cancel()
        await browser_context.close()
        await playwright_instance.stop()

app = FastAPI(title="AI Usage Monitor", lifespan=lifespan)

@app.get("/api/usage")
async def usage():
    async with state_lock: return state

@app.get("/api/health")
async def api_health():
    return {**health, "headless": HEADLESS, "discover": DISCOVER,
            "codex_poll_seconds": CODEX_POLL_SECONDS, "claude_refresh_seconds": CLAUDE_REFRESH_SECONDS,
            "provider_fallback_seconds": max(0, int(os.getenv("PROVIDER_FALLBACK_SECONDS", "300"))), "traffic_log": str(LOG_FILE)}

@app.get("/")
async def dashboard(): return FileResponse(ROOT / "static" / "index.html")

@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await ws.accept(); clients.add(ws)
    try:
        await ws.send_text(json.dumps(state))
        while True: await ws.receive_text()
    except WebSocketDisconnect: clients.discard(ws)
    except Exception: clients.discard(ws)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("BIND_HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8765")))
