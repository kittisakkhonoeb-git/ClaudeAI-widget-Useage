"""
Claude Code Usage Widget
A small always-on-top desktop widget showing your real Claude usage:
- current 5-hour session window (% used, reset time)
- current 7-day window (% used, reset time)

Reads the OAuth access token Claude Code already stores locally at
~/.claude/.credentials.json and calls Anthropic's own usage endpoint
(the same data claude.ai shows you). The token never leaves this
machine and is only used for one GET request per poll.

Built with plain Tkinter (no WebView2) for reliability.
"""

import ctypes
import json
import math
import shutil
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

CRED_PATH = Path.home() / ".claude" / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
REFRESH_URL = "https://platform.claude.com/v1/oauth/token"
REFRESH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REFRESH_BUFFER_SECONDS = 900  # refresh if the access token expires within 15 minutes
POLL_SECONDS = 60
ERROR_RETRY_SECONDS = 15  # first retry after an error; grows with each consecutive failure
BACKOFF_SECONDS = 300  # cap for the retry backoff
UI_REFRESH_MS = 2000

TRANSPARENT_KEY = "#010203"  # chroma-keyed to true transparency via -transparentcolor
PANEL_BG = "#1c1730"
BORDER = "#3a3355"
TEXT_MAIN = "#eae6f7"
TEXT_DIM = "#8c7fb5"
TEXT_SUB = "#a897e0"
TEXT_VAL = "#d8d0f0"
ACCENT = "#CC785C"
ACCENT_LIGHT = "#F4F0E8"
COLOR_OK = "#6ba8ff"
COLOR_WARN = "#e0b357"
COLOR_CRIT = "#e06b6b"

# Logical (96-DPI) sizes; actual pixel sizes are scaled at runtime to the
# display's real DPI so text/shapes render crisp instead of OS-upscaled.
BASE_COLLAPSED = (56, 56)
BASE_EXPANDED = (190, 172)

DRAG_THRESHOLD = 4


def _enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _get_dpi_scale():
    try:
        hdc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, hdc)
        return dpi / 96.0 if dpi else 1.0
    except Exception:
        return 1.0


def _read_credentials():
    with open(CRED_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_credentials(data):
    """Atomically write the credential file back, keeping a one-shot backup."""
    backup_path = CRED_PATH.with_suffix(CRED_PATH.suffix + ".bak")
    try:
        shutil.copy2(CRED_PATH, backup_path)
    except OSError:
        pass
    tmp_path = CRED_PATH.with_suffix(CRED_PATH.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    tmp_path.replace(CRED_PATH)


def _refresh_access_token(data):
    """POST the refresh token to Anthropic's OAuth endpoint and persist the new tokens."""
    oauth = data["claudeAiOauth"]
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": oauth["refreshToken"],
        "client_id": REFRESH_CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        REFRESH_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "claude-usage-widget/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read().decode())

    oauth["accessToken"] = result["access_token"]
    if result.get("refresh_token"):
        oauth["refreshToken"] = result["refresh_token"]
    oauth["expiresAt"] = int((time.time() + result["expires_in"]) * 1000)
    _write_credentials(data)
    return oauth["accessToken"]


def _get_valid_access_token():
    data = _read_credentials()
    oauth = data["claudeAiOauth"]
    expires_at_s = (oauth.get("expiresAt") or 0) / 1000
    remaining = expires_at_s - time.time()
    if remaining < REFRESH_BUFFER_SECONDS:
        try:
            return _refresh_access_token(data)
        except Exception:
            # Refresh failed (offline, revoked, etc). Fall back to the existing
            # token; the usage request below will surface the real error.
            return oauth["accessToken"]
    return oauth["accessToken"]


def _fetch_usage():
    token = _get_valid_access_token()
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _fmt_time(iso_str):
    return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone()


class UsagePoller:
    """Polls the usage endpoint on a background thread and caches the result."""

    def __init__(self):
        self._lock = threading.Lock()
        self._latest = {"state": "loading"}
        self._stop = False
        self._wake = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop = True
        self._wake.set()

    def refresh_now(self):
        """Interrupt the current wait and poll immediately."""
        with self._lock:
            if self._latest.get("state") != "ok":
                self._latest = {"state": "loading"}
        self._wake.set()

    def _run(self):
        interval = 0  # poll immediately on startup
        error_streak = 0
        while not self._stop:
            self._wake.wait(interval)
            self._wake.clear()
            if self._stop:
                break
            try:
                snapshot = self._build_snapshot(_fetch_usage())
                with self._lock:
                    self._latest = snapshot
                interval = POLL_SECONDS
                error_streak = 0
            except urllib.error.HTTPError as e:
                with self._lock:
                    self._latest = {"state": "error", "detail": f"HTTP {e.code}"}
                error_streak += 1
                interval = min(ERROR_RETRY_SECONDS * error_streak, BACKOFF_SECONDS)
            except Exception as exc:
                with self._lock:
                    self._latest = {"state": "error", "detail": str(exc)}
                error_streak += 1
                interval = min(ERROR_RETRY_SECONDS * error_streak, BACKOFF_SECONDS)

    @staticmethod
    def _build_snapshot(raw):
        five_hour = raw.get("five_hour") or {}
        seven_day = raw.get("seven_day") or {}

        session_pct = five_hour.get("utilization")
        week_pct = seven_day.get("utilization")

        session_reset = _fmt_time(five_hour["resets_at"]) if five_hour.get("resets_at") else None
        week_reset = _fmt_time(seven_day["resets_at"]) if seven_day.get("resets_at") else None

        now = datetime.now().astimezone()
        session_remaining_min = int((session_reset - now).total_seconds() // 60) if session_reset else None

        return {
            "state": "ok",
            "session_pct": round(session_pct) if session_pct is not None else None,
            "session_reset": session_reset.strftime("%H:%M") if session_reset else "--",
            "session_remaining_hours": (session_remaining_min // 60) if session_remaining_min is not None else None,
            "session_remaining_mins": (session_remaining_min % 60) if session_remaining_min is not None else None,
            "week_pct": round(week_pct) if week_pct is not None else None,
            "week_reset": week_reset.strftime("%a %H:%M") if week_reset else "--",
        }

    def get(self):
        with self._lock:
            return dict(self._latest)


def severity_color(pct):
    if pct is None:
        return TEXT_DIM
    if pct >= 95:
        return COLOR_CRIT
    if pct >= 80:
        return COLOR_WARN
    return COLOR_OK


def round_rect_points(x1, y1, x2, y2, r):
    return [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]


def draw_sunburst(canvas, cx, cy, r_inner, r_outer, count, color, width=3):
    for i in range(count):
        angle = math.pi * 2 * i / count
        x1 = cx + r_inner * math.cos(angle)
        y1 = cy + r_inner * math.sin(angle)
        x2 = cx + r_outer * math.cos(angle)
        y2 = cy + r_outer * math.sin(angle)
        canvas.create_line(x1, y1, x2, y2, fill=color, width=width, capstyle=tk.ROUND)


class Widget:
    def __init__(self, poller, scale=1.0):
        self.poller = poller
        self.scale = scale
        self.expanded = False
        self.drag_data = None
        self.dragged = False
        self._button_hit = False

        self.collapsed_size = (self.s(BASE_COLLAPSED[0]), self.s(BASE_COLLAPSED[1]))
        self.expanded_size = (self.s(BASE_EXPANDED[0]), self.s(BASE_EXPANDED[1]))

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.wm_attributes("-transparentcolor", TRANSPARENT_KEY)
        self.root.configure(bg=TRANSPARENT_KEY)

        self.screen_w = self.root.winfo_screenwidth()
        self.screen_h = self.root.winfo_screenheight()
        margin = self.s(20)
        # icon_x/icon_y is the anchor: the collapsed icon's top-left corner.
        # The expanded panel is always derived from it so it never runs off-screen.
        self.icon_x = self.screen_w - self.collapsed_size[0] - margin
        self.icon_y = self.screen_h - self.collapsed_size[1] - margin - self.s(48)

        self.canvas = tk.Canvas(
            self.root, width=self.collapsed_size[0], height=self.collapsed_size[1],
            bg=TRANSPARENT_KEY, highlightthickness=0,
        )
        self.canvas.pack()

        self._apply_geometry(self.collapsed_size, self.icon_x, self.icon_y)

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)

        self.draw_collapsed()
        self.root.after(200, self._tick)

    def s(self, v):
        """Scale a logical length to device pixels."""
        return round(v * self.scale)

    def _apply_geometry(self, size, x, y):
        w, h = size
        self.canvas.config(width=w, height=h)
        self.root.geometry(f"{w}x{h}+{int(x)}+{int(y)}")

    def _expanded_geometry(self):
        cw, _ = self.collapsed_size
        ew, eh = self.expanded_size
        ch = self.collapsed_size[1]
        icon_center_x = self.icon_x + cw / 2
        if icon_center_x > self.screen_w / 2:
            # icon lives on the right half -> pop open to the left
            x = self.icon_x + cw - ew
        else:
            # icon lives on the left half -> pop open to the right
            x = self.icon_x
        x = max(0, min(x, self.screen_w - ew))
        y = max(0, self.icon_y - (eh - ch))
        return x, y

    def _on_press(self, event):
        self.drag_data = (event.x_root, event.y_root, self.icon_x, self.icon_y)
        self.dragged = False

    def _on_motion(self, event):
        if not self.drag_data:
            return
        sx, sy, ox, oy = self.drag_data
        dx, dy = event.x_root - sx, event.y_root - sy
        if abs(dx) > DRAG_THRESHOLD or abs(dy) > DRAG_THRESHOLD:
            self.dragged = True
        if self.dragged:
            self.icon_x, self.icon_y = ox + dx, oy + dy
            if self.expanded:
                x, y = self._expanded_geometry()
            else:
                x, y = self.icon_x, self.icon_y
            self.root.geometry(f"+{int(x)}+{int(y)}")

    def _on_release(self, event):
        if self._button_hit:
            self._button_hit = False
            self.drag_data = None
            return
        if not self.dragged:
            self._toggle(event)
        self.drag_data = None

    def _toggle(self, event):
        if self.expanded:
            self.collapse()
        else:
            self.expand()

    def _on_close_click(self, event):
        self._button_hit = True
        self.drag_data = None
        self.poller.stop()
        self.root.destroy()
        return "break"

    def _on_refresh_click(self, event):
        self._button_hit = True
        self.drag_data = None
        self.poller.refresh_now()
        self._update_expanded_values()
        self._flash_refresh_icon()
        return "break"

    def _flash_refresh_icon(self, step=0):
        try:
            color = ACCENT_LIGHT if step % 2 == 0 else TEXT_SUB
            self.canvas.itemconfig(self._refresh_icon_id, fill=color)
        except tk.TclError:
            return  # panel was collapsed/closed mid-animation
        if step < 5:
            self.root.after(110, lambda: self._flash_refresh_icon(step + 1))
        else:
            self.canvas.itemconfig(self._refresh_icon_id, fill=TEXT_SUB)

    def expand(self):
        x, y = self._expanded_geometry()
        self._apply_geometry(self.expanded_size, x, y)
        self.expanded = True
        self.draw_expanded()

    def collapse(self):
        self._apply_geometry(self.collapsed_size, self.icon_x, self.icon_y)
        self.expanded = False
        self.draw_collapsed()

    def draw_collapsed(self):
        c = self.canvas
        c.delete("all")
        w, h = self.collapsed_size
        cx, cy = w / 2, h / 2
        r = self.s(25)

        stats = self.poller.get()
        pct = stats.get("session_pct") if stats.get("state") == "ok" else None
        color = severity_color(pct)
        fill = color if pct is not None else ACCENT

        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=fill, outline="#0f0c1a", width=self.s(1.5))

        label = str(pct) if pct is not None else ("…" if stats.get("state") == "loading" else "?")
        font_size = 20 if pct is not None and pct < 100 else 16
        c.create_text(cx, cy, text=label, anchor="center", fill=ACCENT_LIGHT, font=("Segoe UI", font_size, "bold"))

    def draw_expanded(self):
        c = self.canvas
        c.delete("all")
        w, h = self.expanded_size
        pad = self.s(14)
        c.create_polygon(
            round_rect_points(self.s(1), self.s(1), w - self.s(1), h - self.s(1), self.s(14)),
            smooth=True, fill=PANEL_BG, outline=BORDER,
        )

        # header
        ic_r = self.s(12.5)
        ic_cx, ic_cy = self.s(18.5), self.s(18.5)
        c.create_oval(ic_cx - ic_r, ic_cy - ic_r, ic_cx + ic_r, ic_cy + ic_r, fill=ACCENT, outline="")
        draw_sunburst(c, ic_cx, ic_cy, self.s(2.5), self.s(6), 8, ACCENT_LIGHT, width=self.s(1.5))
        c.create_text(self.s(31), self.s(18), text="CLAUDE USAGE", anchor="w", fill=TEXT_SUB, font=("Segoe UI", 9, "bold"))

        refresh_cx = w - self.s(32)
        hit = self.s(9)
        c.create_rectangle(refresh_cx - hit, self.s(18) - hit, refresh_cx + hit, self.s(18) + hit, fill=PANEL_BG, outline="", tags="refresh_btn")
        self._refresh_icon_id = c.create_text(refresh_cx, self.s(18), text="↻", anchor="center", fill=TEXT_SUB, font=("Segoe UI", 12, "bold"), tags="refresh_btn")
        c.tag_bind("refresh_btn", "<ButtonPress-1>", lambda e: "break")
        c.tag_bind("refresh_btn", "<ButtonRelease-1>", self._on_refresh_click)

        close_cx = w - self.s(14)
        c.create_rectangle(close_cx - hit, self.s(18) - hit, close_cx + hit, self.s(18) + hit, fill=PANEL_BG, outline="", tags="close_btn")
        c.create_text(close_cx, self.s(18), text="✕", anchor="center", fill=TEXT_VAL, font=("Segoe UI", 11, "bold"), tags="close_btn")
        c.tag_bind("close_btn", "<ButtonPress-1>", lambda e: "break")
        c.tag_bind("close_btn", "<ButtonRelease-1>", self._on_close_click)

        self._pct_id = c.create_text(pad, self.s(47), text="--", anchor="w", fill=TEXT_MAIN, font=("Segoe UI", 25, "bold"))
        self._timeleft_id = c.create_text(self.s(95), self.s(52), text="", anchor="w", fill=TEXT_SUB, font=("Segoe UI", 10, "bold"))

        bar_y1, bar_y2 = self.s(66), self.s(70)
        c.create_rectangle(pad, bar_y1, w - pad, bar_y2, fill="#332c52", outline="")
        self._bar_id = c.create_rectangle(pad, bar_y1, pad, bar_y2, fill=COLOR_OK, outline="")

        y = self.s(84)
        c.create_line(pad, y, w - pad, y, fill=BORDER)
        c.create_text(pad, y + self.s(14), text="Session (5h)", anchor="w", fill=TEXT_SUB, font=("Segoe UI", 10, "bold"))
        self._session_reset_id = c.create_text(pad, y + self.s(28), text="--", anchor="w", fill="#8577a8", font=("Segoe UI", 9))
        self._session_pct_id = c.create_text(w - pad, y + self.s(18), text="--", anchor="e", fill=TEXT_MAIN, font=("Segoe UI", 12, "bold"))

        y2 = y + self.s(44)
        c.create_line(pad, y2, w - pad, y2, fill=BORDER)
        c.create_text(pad, y2 + self.s(14), text="This week", anchor="w", fill=TEXT_SUB, font=("Segoe UI", 10, "bold"))
        self._week_reset_id = c.create_text(pad, y2 + self.s(28), text="--", anchor="w", fill="#8577a8", font=("Segoe UI", 9))
        self._week_pct_id = c.create_text(w - pad, y2 + self.s(18), text="--", anchor="e", fill=TEXT_MAIN, font=("Segoe UI", 12, "bold"))

        self._bar_x0 = pad
        self._bar_x1_max = w - pad
        self._bar_y1 = bar_y1
        self._bar_y2 = bar_y2

        self._update_expanded_values()

    def _update_expanded_values(self):
        stats = self.poller.get()
        c = self.canvas

        if stats.get("state") != "ok" or stats.get("session_pct") is None:
            label = "..." if stats.get("state") == "loading" else "?"
            c.itemconfig(self._pct_id, text=label)
            c.itemconfig(self._timeleft_id, text="offline" if stats.get("state") == "error" else "")
            return

        pct = stats["session_pct"]
        color = severity_color(pct)
        c.itemconfig(self._pct_id, text=f"{pct}%", fill=color if pct >= 80 else TEXT_MAIN)
        c.itemconfig(
            self._timeleft_id,
            text=f"{stats['session_remaining_hours']}h {stats['session_remaining_mins']}m left",
        )

        bar_w = max(0.0, min(1.0, pct / 100.0)) * (self._bar_x1_max - self._bar_x0)
        c.coords(self._bar_id, self._bar_x0, self._bar_y1, self._bar_x0 + bar_w, self._bar_y2)
        c.itemconfig(self._bar_id, fill=color)

        c.itemconfig(self._session_pct_id, text=f"{pct}%")
        c.itemconfig(self._session_reset_id, text=f"resets {stats['session_reset']}")

        week_pct = stats.get("week_pct")
        c.itemconfig(self._week_pct_id, text=(f"{week_pct}%" if week_pct is not None else "--"))
        c.itemconfig(self._week_reset_id, text=f"resets {stats['week_reset']}")

    def _tick(self):
        if self.expanded:
            self._update_expanded_values()
        else:
            self.draw_collapsed()
        self.root.after(UI_REFRESH_MS, self._tick)

    def run(self):
        self.root.mainloop()


def main():
    _enable_dpi_awareness()
    scale = _get_dpi_scale()

    poller = UsagePoller()
    poller.start()
    widget = Widget(poller, scale)
    widget.run()


if __name__ == "__main__":
    main()
