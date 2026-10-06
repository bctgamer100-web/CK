import atexit
import json
import os
import subprocess
import sys
import re
import threading
import time
import urllib.request
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import adb
from bot import BOOST_CHOICES, HEARTS_NOW_FILE, RESULTS_DIR, STAT_MARKER
from config import DEVICE_IP, DEVICE_PORT

APP_NAME = "CookieRunBot"
APP_VERSION = "1.1.0"
HOST = "127.0.0.1"
PORT = 8000
WINDOW_SIZE = "1040,720"
_quit_event = threading.Event()  # set to close the whole app (window closed or "quit" pressed)

# The app closes when its window does. The browser process we start can hand the window over to another
# process and exit, so the window is tracked by the page itself: it polls /status while open ("seen")
# and says /bye when it is closed or reloaded.
APP_WINDOW_GONE_SECONDS = 20   # no poll for this long and no browser process left: the app window is gone
BYE_GRACE_SECONDS = 4      # a reload says bye and then polls again right away
_window = {"seen": time.time(), "bye": None}
FROZEN = getattr(sys, "frozen", False)  # True when running as the packaged CookieRunBot.exe
# Folder holding index.html, templates/ and web-config.json (next to the exe when packaged)
BASE_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
MAX_LOG_LINES = 2000
CONFIG_PATH = os.path.join(BASE_DIR, "web-config.json")  # options last chosen in the web UI
HISTORY_PATH = os.path.join(BASE_DIR, "stats-history.json")  # one summary per finished bot session
RESULTS_PATH = os.path.join(BASE_DIR, RESULTS_DIR)  # result screens saved by the bot
MAX_ROUND_DETAILS = 50
MAX_HISTORY = 100
MAX_CONFIG_BYTES = 2_000_000  # the settings include the user's saved option sets (about 800 bytes each)

# Options the web UI may send; keys that are not sent keep the bot's original behaviour
BOOL_OPTIONS = (
    "dry_run",
    "buy_hp_extension",
    "buy_power_jelly",
    "buy_double_xp",
    "buy_fast_start",
    "buy_cookie_relay",
    "auto_jump",
    "stop_jump_when_done",
    "exit_after_relay",
    "send_hearts",
    "mail_hearts",
    "close_popups",
    "restart_on_lost",
    "hearts_just_sent",
    "stop_rounds",
    "stop_minutes",
    "stop_hearts",
    "stop_coins",
)
INT_OPTIONS = {  # key -> (min, max)
    "jump_limit": (1, 100000),
    "heart_hours": (1, 1000),
    "mail_minutes": (0, 100000),  # 0: every time the bot is back at the main menu
    "next_game_delay_min": (0, 3600),
    "next_game_delay_max": (0, 3600),
    "result_delay": (0, 600),
    "stop_rounds_n": (1, 100000),
    "stop_minutes_n": (1, 100000),
    "stop_hearts_n": (0, 100000),
    "stop_coins_n": (1, 2_000_000_000),
}

# Runs the unmodified bot in a child process, answering the option prompts from BOT_OPTIONS
RUNNER = """
import json, os
import bot, main
options = json.loads(os.environ["BOT_OPTIONS"])
index = options.pop("boost_index")
name, template = bot.BOOST_CHOICES[index] if options["use_desired_random_boost"] else (None, None)
options["desired_boost_name"] = name
options["desired_boost_template"] = template
bot.prompt_user_options = lambda: options
main.main()
"""
# Runs one treasure tool (treasure.py) in a child process with the options from TOOL_OPTIONS
TOOL_RUNNER = """
import json, os
import main, treasure
treasure.run(json.loads(os.environ["TOOL_OPTIONS"]))
"""
TOOL_NAMES = ("upgrade", "powder", "tickets", "shard", "hearts")
KEEP_CHOICES = ("cookie", "pill", "pearl", "hammer", "horse")
AFTER_CHOICES = ("bot", "lobby", "quit")

_lock = threading.Lock()
DEVICE_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{0,64}")  # an adb serial; "" = let the bot find the emulator


def _safe_name(device: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", device)


def _new_stats() -> dict:
    return {"started": time.time(), "ended": None, "rounds": [], "round_count": 0,
            "cards_seen": 0, "cards_solved": 0, "boxes": 0, "coins": 0, "xp": 0,
            "box_types": {"1": 0, "2": 0, "3": 0, "4": 0}}  # copper / silver / gold / rainbow


class Session:
    """One bot process and everything it reports, for one emulator ("" = the bot finds the emulator itself)."""

    def __init__(self, device: str):
        self.device = device
        self.proc = None
        self.logs = deque(maxlen=MAX_LOG_LINES)
        self.log_total = 0
        self.rounds = 0  # finished games since the bot was last started
        self.stats = _new_stats()
        self.stats["ended"] = self.stats["started"]  # nothing has run yet
        # each emulator keeps its result screens and its "send hearts now" request apart from the others
        self.results_dir = RESULTS_DIR + ("/" + _safe_name(device) if device else "")
        self.hearts_file = HEARTS_NOW_FILE + ("-" + _safe_name(device) if device else "")
        self.tool = None        # name of the treasure tool running instead of the bot, or None
        self.after_tool = None  # (action, bot options) to carry out when the tool finishes on its own
        self.treasure = {}      # last Magic Powder / Mystic Shard counts and before/after shots of a tool

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


_sessions = {}


def get_session(device: str = "") -> Session:
    with _lock:
        if device not in _sessions:
            _sessions[device] = Session(device)
        return _sessions[device]


def _append_log(session: Session, line: str):
    with _lock:
        session.logs.append(line)
        session.log_total += 1
        if "Detected Stage: GAME_COMPLETE" in line:
            session.rounds += 1


TOTALS_PATH = os.path.join(BASE_DIR, "stats-totals.json")  # everything ever collected, across sessions and emulators
_totals = None  # loaded on first use; guarded by _lock


def _empty_totals() -> dict:
    return {"rounds": 0, "coins": 0, "xp": 0, "boxes": 0, "box_types": {"1": 0, "2": 0, "3": 0, "4": 0}}


def _load_totals() -> dict:
    """The running totals; the first time, they start from the sessions already in the run history."""
    global _totals
    if _totals is None:
        try:
            with open(TOTALS_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            _totals = _empty_totals()
            for key in ("rounds", "coins", "xp", "boxes"):
                if isinstance(loaded.get(key), int):
                    _totals[key] = loaded[key]
            for kind, count in (loaded.get("box_types") or {}).items():
                if kind in _totals["box_types"] and isinstance(count, int):
                    _totals["box_types"][kind] = count
        except (OSError, ValueError, AttributeError):
            _totals = _empty_totals()
            for entry in load_history():
                if not isinstance(entry, dict):
                    continue
                for key, source in (("rounds", "round_count"), ("coins", "coins"), ("xp", "xp"), ("boxes", "boxes")):
                    if isinstance(entry.get(source), int):
                        _totals[key] += entry[source]
                for kind, count in (entry.get("box_types") or {}).items():
                    if kind in _totals["box_types"] and isinstance(count, int):
                        _totals["box_types"][kind] += count
            _save_totals()
    return _totals


def _save_totals():
    try:
        with open(TOTALS_PATH, "w", encoding="utf-8") as f:
            json.dump(_totals, f, indent=2)
    except OSError:
        pass


def _add_totals(rounds=0, coins=0, xp=0, boxes=0, box_type=None):
    """Adds one statistics event to the running totals (the caller holds _lock)."""
    totals = _load_totals()
    totals["rounds"] += rounds
    totals["coins"] += coins
    totals["xp"] += xp
    totals["boxes"] += boxes
    if box_type in totals["box_types"]:
        totals["box_types"][box_type] += 1
    _save_totals()


def _totals_copy() -> dict:
    with _lock:
        return json.loads(json.dumps(_load_totals()))


def _handle_stat(session: Session, data: dict):
    """Applies one statistics event printed by the bot (see emit_stat in bot.py)."""
    with _lock:
        stats = session.stats
        event = data.get("event")
        if event == "round":
            stats["round_count"] += 1
            coins, xp, shot = data.get("coins"), data.get("xp"), data.get("shot")
            stats["coins"] += coins if isinstance(coins, int) else 0
            stats["xp"] += xp if isinstance(xp, int) else 0
            _add_totals(rounds=1, coins=coins if isinstance(coins, int) else 0, xp=xp if isinstance(xp, int) else 0)
            stats["rounds"].append({
                "round": stats["round_count"],
                "time": time.time(),
                "seconds": data.get("seconds"),
                # path below /results/, including this emulator's sub-folder
                "shot": (session.results_dir + "/" + shot)[len(RESULTS_DIR) + 1:] if shot else None,
                "coins": coins,
                "xp": xp,
            })
            del stats["rounds"][:-MAX_ROUND_DETAILS]
        elif event == "card_seen":
            stats["cards_seen"] += 1
        elif event == "card_solved":
            stats["cards_solved"] += 1
        elif event == "box":
            stats["boxes"] += 1
            kind = str(data.get("kind"))
            if kind in stats["box_types"]:
                stats["box_types"][kind] += 1
            _add_totals(boxes=1, box_type=kind)
        elif event == "treasure":
            for key in ("powder", "shard"):
                if isinstance(data.get(key), int):
                    session.treasure[key] = data[key]
        elif event in ("toolbefore", "toolafter") and isinstance(data.get("shot"), str):
            session.treasure[event] = (session.results_dir + "/" + data["shot"])[len(RESULTS_DIR) + 1:]


def _stats_summary(session: Session) -> dict:
    with _lock:
        stats = session.stats
        end = stats["ended"] or time.time()
        summary = {key: stats[key] for key in ("started", "round_count", "cards_seen", "cards_solved", "boxes", "coins", "xp")}
        summary["box_types"] = dict(stats["box_types"])
        summary["elapsed"] = round(end - stats["started"])
        summary["round_details"] = len(stats["rounds"])
        summary["last_seconds"] = stats["rounds"][-1]["seconds"] if stats["rounds"] else None
        return summary


def load_history() -> list:
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            history = json.load(f)
        return history if isinstance(history, list) else []
    except (OSError, ValueError):
        return []


_history_lock = threading.Lock()


def _finish_stats(session: Session):
    """Called when a bot process ends: freezes the timer and adds the session to the run history."""
    with _lock:
        session.stats["ended"] = time.time()
        if session.tool:
            return  # treasure tools do not play rounds
    summary = _stats_summary(session)
    if summary["round_count"] == 0 and summary["elapsed"] < 60:
        return  # not worth a history entry
    summary.pop("round_details")
    summary["device"] = session.device
    with _history_lock:
        history = load_history()
        history.append(summary)
        try:
            with open(HISTORY_PATH, "w", encoding="utf-8") as f:
                json.dump(history[-MAX_HISTORY:], f, indent=2)
        except OSError:
            pass


def _read_output(session: Session, proc):
    for line in proc.stdout:
        line = line.rstrip("\r\n")
        if STAT_MARKER in line:
            try:
                _handle_stat(session, json.loads(line.split(STAT_MARKER, 1)[1]))
            except ValueError:
                pass
            continue
        _append_log(session, line)
    proc.wait()
    _finish_stats(session)
    if session.tool:
        _append_log(session, f"⏹️ Tool stopped (exit code {proc.returncode}).")
        _after_tool(session, proc.returncode)
    else:
        _append_log(session, f"⏹️ Bot stopped (exit code {proc.returncode}).")


def _after_tool(session: Session, returncode: int):
    """What the user chose to happen once a tool has finished by itself (not when it failed or was stopped)."""
    with _lock:
        action, bot_options = session.after_tool or ("lobby", None)
        session.after_tool = None
    if returncode != 0 or action == "lobby":
        return
    if action == "bot" and bot_options is not None:
        _append_log(session, "▶️ เครื่องมือเสร็จแล้ว เริ่มบอทต่อ")
        start_bot(bot_options, session.device)
    elif action == "quit":
        _append_log(session, "⏏️ เครื่องมือเสร็จแล้ว ปิดเกม")
        try:
            serial = session.device or adb._serial(DEVICE_IP, DEVICE_PORT)
            adb._adb_run(["-s", serial, "shell", "am", "force-stop", adb.GAME_PACKAGE])
        except Exception as e:
            _append_log(session, f"⚠️ ปิดเกมไม่ได้: {e}")


def is_running(device=None) -> bool:
    """True if the bot of that emulator is running; with no device, if any bot is running."""
    with _lock:
        sessions = list(_sessions.values())
    return any(session.running() for session in sessions if device is None or session.device == device)


def _spawn(session: Session, flag: str, runner: str, env_extra: dict):
    """Starts the child process of a session (the caller holds _lock)."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["BOT_DEVICE"] = session.device
    env["BOT_RESULTS_DIR"] = session.results_dir
    env["BOT_HEARTS_FILE"] = session.hearts_file
    env.update(env_extra)
    session.proc = subprocess.Popen(
        [sys.executable, flag] if FROZEN else [sys.executable, "-u", "-c", runner],
        cwd=BASE_DIR,
        env=env,
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    threading.Thread(target=_read_output, args=(session, session.proc), daemon=True).start()


def start_bot(options: dict, device: str = "") -> bool:
    session = get_session(device)
    with _lock:
        if session.running():
            return False
        session.rounds = 0
        session.stats = _new_stats()
        session.tool = None
        _spawn(session, "--run-bot", RUNNER, {"BOT_OPTIONS": json.dumps(options)})
    return True


def start_tool(options: dict, device: str = "", after=("lobby", None)) -> bool:
    session = get_session(device)
    with _lock:
        if session.running():
            return False
        session.rounds = 0
        session.stats = _new_stats()
        session.tool = options["tool"]
        session.after_tool = after
        session.treasure = {}
        _spawn(session, "--run-tool", TOOL_RUNNER, {"TOOL_OPTIONS": json.dumps(options)})
    return True


def _int_option(data: dict, key: str, low: int, high: int, default: int) -> int:
    value = int(data.get(key, default))
    if not low <= value <= high:
        raise ValueError(f"Invalid {key}")
    return value


def parse_tool_options(data: dict) -> dict:
    tool = data.get("tool")
    if tool not in TOOL_NAMES:
        raise ValueError("Invalid tool")
    keep = data.get("keep") or []
    if not isinstance(keep, list) or any(k not in KEEP_CHOICES for k in keep):
        raise ValueError("Invalid keep")
    return {
        "tool": tool,
        "keep": keep,
        "extract_gold": bool(data.get("extract_gold")),
        "powder_target": _int_option(data, "powder_target", 1, 10_000_000, 1000),
        "draws_before_extract": _int_option(data, "draws_before_extract", 0, 1000, 0),
        "shard_target": _int_option(data, "shard_target", 1, 10_000, 9),
        "shard_buy_boxes": bool(data.get("shard_buy_boxes")),
        "shard_use_cabinet": bool(data.get("shard_use_cabinet", True)),
        "craft_missing": bool(data.get("craft_missing", True)),
    }


def stop_bot(device=None) -> bool:
    """Stops the bot of one emulator, or every bot when no device is given."""
    with _lock:
        sessions = [s for s in _sessions.values() if s.running() and (device is None or s.device == device)]
        for session in sessions:
            session.proc.terminate()
    return bool(sessions)


def parse_options(data: dict) -> dict:
    index = int(data.get("boost_index", 0))
    if not 0 <= index < len(BOOST_CHOICES):
        raise ValueError("Invalid boost_index")
    options = {
        "use_fast_start": bool(data.get("use_fast_start")),
        "use_cookie_relay": bool(data.get("use_cookie_relay")),
        "use_desired_random_boost": bool(data.get("use_desired_random_boost")),
        "detect_relic": bool(data.get("detect_relic")),
        "boost_index": index,
    }
    for key in BOOL_OPTIONS:
        if key in data:
            options[key] = bool(data[key])
    for key, (low, high) in INT_OPTIONS.items():
        if key in data:
            value = int(data[key])
            if not low <= value <= high:
                raise ValueError(f"Invalid {key}")
            options[key] = value
    options["report_stats"] = True  # the bot reports rounds, card games and boxes back to this server
    return options


_adb_lock = threading.Lock()
_adb_connected = False


def capture_png(device: str = "") -> bytes:
    global _adb_connected
    if device:
        return adb.capture_png_serial(device)
    with _adb_lock:
        try:
            if not _adb_connected:
                adb.device_connect(DEVICE_IP, DEVICE_PORT)
                _adb_connected = True
            return adb.device_capture_png(DEVICE_IP, DEVICE_PORT)
        except Exception:
            _adb_connected = False
            raise


class LiveStream:
    """Real-time view of one emulator: `adb screenrecord` streams H.264, which is decoded here and handed to
    the page as an MJPEG stream (one JPEG per frame). Without PyAV it falls back to quick raw screenshots.
    It stops by itself a few seconds after the last viewer has gone."""
    FPS = 30
    IDLE_SECONDS = 5

    def __init__(self, device: str):
        self.device = device
        self.jpeg = None
        self.frame_id = 0
        self.viewers = 0
        self.last_viewer = time.time()
        self.cond = threading.Condition()
        self.thread = None

    def _serial(self) -> str:
        if self.device:
            return self.device
        capture_png("")  # makes sure the default emulator is connected
        return adb._serial(DEVICE_IP, DEVICE_PORT)

    def _publish(self, bgr):
        import cv2
        ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if ok:
            with self.cond:
                self.jpeg = buf.tobytes()
                self.frame_id += 1
                self.cond.notify_all()

    def _idle(self) -> bool:
        return self.viewers == 0 and time.time() - self.last_viewer > self.IDLE_SECONDS

    def _run_video(self, serial: str):
        import av
        while not self._idle():
            proc = subprocess.Popen(
                [adb._adb(), "-s", serial, "exec-out", "screenrecord", "--output-format=h264", "--bit-rate", "6000000", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            codec = av.CodecContext.create("h264", "r")
            last = 0.0
            got_frame = False
            try:
                while not self._idle():
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break  # screenrecord stops after 3 minutes: start it again
                    for packet in codec.parse(chunk):
                        for frame in codec.decode(packet):
                            got_frame = True
                            now = time.time()
                            if now - last >= 1 / self.FPS:
                                last = now
                                self._publish(frame.to_ndarray(format="bgr24"))
            finally:
                proc.kill()
            if not got_frame:
                raise RuntimeError("screenrecord gave no video")

    def _run_screenshots(self, serial: str):
        import cv2
        import numpy as np
        import struct
        while not self._idle():
            data = adb._run([adb._adb(), "-s", serial, "exec-out", "screencap"], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=10).stdout
            if len(data) < 16:
                time.sleep(0.5)
                continue
            width, height, _ = struct.unpack("<III", data[:12])
            offset = len(data) - width * height * 4
            rgba = np.frombuffer(data, np.uint8, offset=offset).reshape(height, width, 4)
            self._publish(cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR))

    def _run(self):
        try:
            serial = self._serial()
            try:
                self._run_video(serial)
            except Exception:
                self._run_screenshots(serial)
        except Exception:
            pass
        finally:
            with self.cond:
                self.thread = None
                self.cond.notify_all()

    def frames(self):
        """Yields JPEG frames for one viewer, as they come."""
        with self.cond:
            self.viewers += 1
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()
        seen = -1
        try:
            while True:
                with self.cond:
                    self.cond.wait_for(lambda: (self.jpeg is not None and self.frame_id != seen) or self.thread is None,
                                       timeout=10)
                    if self.thread is None or self.jpeg is None or self.frame_id == seen:
                        return
                    seen, jpeg = self.frame_id, self.jpeg
                yield jpeg
        finally:
            with self.cond:
                self.viewers -= 1
                self.last_viewer = time.time()


_live_streams = {}


def live_stream(device: str) -> LiveStream:
    with _lock:
        if device not in _live_streams:
            _live_streams[device] = LiveStream(device)
        return _live_streams[device]


def request_hearts_now() -> bool:
    """Asks every running bot to send friend lives at its next main menu."""
    with _lock:
        sessions = [session for session in _sessions.values() if session.running()]
    for session in sessions:
        with open(session.hearts_file, "w") as f:
            f.write("1")
    return bool(sessions)


_devices_lock = threading.Lock()
_devices = None  # emulators found by the last search


def find_devices(refresh: bool = False) -> list:
    global _devices
    with _devices_lock:
        if refresh or _devices is None:
            _devices = adb.discover_devices(DEVICE_IP)
        return _devices


def request_devices(data: dict) -> list:
    """The emulators a /start or /stop request names; [""] (the bot finds one itself) when it names none."""
    devices = data.get("devices") or [""]
    if not isinstance(devices, list) or len(devices) > 32:
        raise ValueError("Invalid devices")
    for device in devices:
        if not isinstance(device, str) or not DEVICE_PATTERN.fullmatch(device):
            raise ValueError("Invalid device")
    return devices


def query_device(url) -> str:
    device = parse_qs(url.query).get("device", [""])[0]
    if not DEVICE_PATTERN.fullmatch(device):
        raise ValueError("Invalid device")
    return device


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
        return config if isinstance(config, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(config: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


PAGE = """<!doctype html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CookieRun Classic Bot</title>
<style>
  :root { --bg:#f6f3ee; --card:#fff; --text:#2b2520; --muted:#7a6f66; --line:#e3dcd3; --accent:#c8621f; --stop:#b3261e; --log:#1e1b18; --logtext:#e9e2d8; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#181513; --card:#231f1c; --text:#eee7de; --muted:#a2978c; --line:#3a332e; --accent:#e08a4a; --stop:#e5675f; --log:#0f0d0c; --logtext:#e9e2d8; }
  }
  * { box-sizing: border-box; }
  body { margin:0; padding:16px; background:var(--bg); color:var(--text); font-family:"Segoe UI","Leelawadee UI",Tahoma,sans-serif; }
  main { max-width:860px; margin:0 auto; }
  h1 { font-size:22px; margin:8px 0 16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; margin-bottom:16px; }
  label.opt { display:flex; align-items:center; gap:10px; padding:8px 0; cursor:pointer; }
  input[type=checkbox] { width:18px; height:18px; accent-color:var(--accent); }
  select { margin:4px 0 8px 28px; padding:6px 8px; border-radius:8px; border:1px solid var(--line); background:var(--card); color:var(--text); font:inherit; max-width:100%; }
  .row { display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-top:12px; }
  button { font:inherit; font-weight:600; padding:10px 22px; border-radius:10px; border:0; cursor:pointer; color:#fff; }
  button:disabled { opacity:.4; cursor:not-allowed; }
  #start { background:var(--accent); }
  #stop { background:var(--stop); }
  #clear { background:transparent; color:var(--muted); border:1px solid var(--line); padding:6px 12px; font-weight:400; }
  #status { color:var(--muted); }
  #status.on { color:var(--accent); font-weight:600; }
  .loghead { display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }
  #log { background:var(--log); color:var(--logtext); border-radius:8px; padding:12px; height:420px; overflow:auto; font:13px/1.5 Consolas,"Cascadia Mono",monospace; white-space:pre-wrap; word-break:break-word; margin:0; }
  .note { color:var(--muted); font-size:13px; margin:8px 0 0; }
</style>
</head>
<body>
<main>
  <h1>🍪 CookieRun Classic Bot</h1>
  <div class="card">
    <label class="opt"><input type="checkbox" id="fast"> ⚡ ใช้ Fast Start (ซื้อ + ใช้)</label>
    <label class="opt"><input type="checkbox" id="relay"> 🍪 ใช้ Cookie Relay (ซื้อ + ใช้)</label>
    <label class="opt"><input type="checkbox" id="boost"> 🎲 ใช้ Random Boost ที่ต้องการ (ซื้อ + ใช้)</label>
    <select id="boostIndex" disabled></select>
    <label class="opt"><input type="checkbox" id="relic"> 🏺 ตรวจจับ Relic (เปิด + รับ)</label>
    <div class="row">
      <button id="start">เริ่มบอท</button>
      <button id="stop" disabled>หยุดบอท</button>
      <span id="status">กำลังโหลด...</span>
    </div>
    <p class="note">จออีมูต้องเป็น 1280x720 และเปิดเกมค้างไว้ที่หน้าเมนูหลักก่อนกดเริ่ม</p>
  </div>
  <div class="card">
    <div class="loghead"><strong>Log</strong><button id="clear">ล้างหน้าจอ</button></div>
    <pre id="log"></pre>
  </div>
</main>
<script>
const $ = id => document.getElementById(id);
const inputs = ["fast", "relay", "boost", "relic"];
let since = 0, boostsLoaded = false;

function setRunning(running) {
  $("start").disabled = running;
  $("stop").disabled = !running;
  inputs.forEach(id => $(id).disabled = running);
  $("boostIndex").disabled = running || !$("boost").checked;
  $("status").textContent = running ? "● กำลังทำงาน" : "○ หยุดอยู่";
  $("status").className = running ? "on" : "";
}

async function poll() {
  try {
    const res = await fetch("/status?since=" + since);
    const data = await res.json();
    if (!boostsLoaded) {
      data.boosts.forEach((name, i) => $("boostIndex").add(new Option(name, i)));
      boostsLoaded = true;
    }
    if (data.lines.length) {
      const log = $("log");
      const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
      log.textContent += data.lines.join("\\n") + "\\n";
      if (atBottom) log.scrollTop = log.scrollHeight;
    }
    since = data.total;
    setRunning(data.running);
  } catch (e) {
    $("status").textContent = "⚠️ ติดต่อ web.py ไม่ได้ (ปิดไปแล้วหรือเปล่า?)";
    $("status").className = "";
  }
}

async function post(path, body) {
  await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  poll();
}

$("boost").onchange = () => $("boostIndex").disabled = !$("boost").checked;
$("start").onclick = () => post("/start", {
  use_fast_start: $("fast").checked,
  use_cookie_relay: $("relay").checked,
  use_desired_random_boost: $("boost").checked,
  boost_index: Number($("boostIndex").value || 0),
  detect_relic: $("relic").checked,
});
$("stop").onclick = () => post("/stop");
$("clear").onclick = () => $("log").textContent = "";

poll();
setInterval(poll, 1000);
</script>
</body>
</html>
"""


# ---------- remote control (phone / another computer) ----------
# Off by default. When on, a second server listens on all network interfaces (REMOTE_PORT) and every
# request from another device needs a login with the password set on this computer. The app window
# itself (127.0.0.1) never needs to log in.
REMOTE_PATH = os.path.join(BASE_DIR, "remote.json")
REMOTE_PORT = 8080
SESSION_COOKIE = "crb_session"
SESSION_DAYS = 30
LOGIN_MAX_FAILURES = 5
LOGIN_LOCK_SECONDS = 60
_remote_lock = threading.Lock()
_remote_server = None
_login_tokens = {}    # token -> expiry time
_login_failures = {}  # client ip -> (failures, time of the last one)


def load_remote() -> dict:
    try:
        with open(REMOTE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def save_remote(data: dict):
    with open(REMOTE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _hash_password(password: str, salt: str) -> str:
    import hashlib
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()


def remote_enabled() -> bool:
    return _remote_server is not None


def local_addresses() -> list:
    """IPv4 addresses of this computer that other devices can use (Wi-Fi/LAN and Tailscale's 100.x)."""
    import socket
    found = set()
    try:
        for address in socket.gethostbyname_ex(socket.gethostname())[2]:
            found.add(address)
    except OSError:
        pass
    try:  # the address used for the internet connection, in case the host name does not list it
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            found.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(a for a in found if not a.startswith("127.") and not a.startswith("169.254."))


def start_remote() -> str:
    """Starts the remote server; returns "" or an error message."""
    global _remote_server
    with _remote_lock:
        if _remote_server is not None:
            return ""
        try:
            server = Server(("0.0.0.0", REMOTE_PORT), Handler)
        except OSError as e:
            return f"เปิดพอร์ต {REMOTE_PORT} ไม่ได้: {e}"
        _remote_server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
    return ""


def stop_remote():
    global _remote_server
    with _remote_lock:
        server, _remote_server = _remote_server, None
    if server is not None:
        server.shutdown()
        server.server_close()
    _login_tokens.clear()  # everyone has to log in again next time


def remote_status() -> dict:
    data = load_remote()
    return {"enabled": remote_enabled(), "has_password": bool(data.get("hash")), "port": REMOTE_PORT,
            "addresses": local_addresses()}


LOGIN_PAGE = """<!doctype html><html lang="th"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>CookieRunBot - เข้าสู่ระบบ</title>
<style>
:root { --bg:#fff5e3; --card:#fffdf9; --text:#33261a; --muted:#85735f; --accent:#ff8a00; --border:#f3dfbd; --err:#c0341d; }
@media (prefers-color-scheme: dark) { :root { --bg:#18130e; --card:#241d16; --text:#f3e9dc; --muted:#ab9a86; --accent:#ff9a1f; --border:#3d3125; --err:#ff8f7a; } }
* { box-sizing:border-box; }
body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center; padding:16px;
  background:var(--bg); color:var(--text); font-family:"Prompt","Leelawadee UI","Segoe UI",Tahoma,sans-serif; }
form { width:min(360px,100%); background:var(--card); border-radius:16px; padding:22px; box-shadow:0 1px 0 var(--border); }
h1 { font-size:20px; margin:0 0 4px; } p { margin:0 0 16px; color:var(--muted); font-size:14px; }
input { width:100%; font:inherit; padding:10px 12px; border:1.5px solid var(--border); border-radius:10px; background:var(--bg); color:var(--text); }
button { width:100%; margin-top:12px; font:inherit; font-weight:600; padding:10px; border:0; border-radius:10px; background:var(--accent); color:#fff; cursor:pointer; }
.err { color:var(--err); font-size:14px; margin-top:10px; }
</style></head><body>
<form method="post" action="/login">
  <h1>🍪 CookieRunBot</h1><p>ใส่รหัสผ่านที่ตั้งไว้ในโปรแกรมบนคอมที่บ้าน</p>
  <input type="password" name="password" autocomplete="current-password" placeholder="รหัสผ่าน" autofocus required>
  <button>เข้าสู่ระบบ</button>
  <!--ERROR-->
</form></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _is_local(self) -> bool:
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _logged_in(self) -> bool:
        from http.cookies import SimpleCookie
        try:
            cookie = SimpleCookie(self.headers.get("Cookie") or "")
        except Exception:
            return False
        token = cookie[SESSION_COOKIE].value if SESSION_COOKIE in cookie else ""
        expiry = _login_tokens.get(token)
        return bool(token) and expiry is not None and expiry > time.time()

    def _allowed(self) -> bool:
        """Requests from this computer are always allowed; others need the remote server and a login."""
        if self._is_local():
            return True
        return remote_enabled() and self._logged_in()

    def _login_page(self, status=200, error=""):
        body = LOGIN_PAGE.replace("<!--ERROR-->", f'<div class="err">{error}</div>' if error else "")
        self._send(status, body.encode("utf-8"), "text/html; charset=utf-8")

    def _handle_login(self):
        ip = self.client_address[0]
        failures, last = _login_failures.get(ip, (0, 0))
        if failures >= LOGIN_MAX_FAILURES and time.time() - last < LOGIN_LOCK_SECONDS:
            self._login_page(429, "ใส่รหัสผิดหลายครั้ง รอ 1 นาทีแล้วลองใหม่")
            return
        length = min(int(self.headers.get("Content-Length") or 0), 4096)
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        password = (form.get("password") or [""])[0]
        data = load_remote()
        import hmac
        if not data.get("hash") or not hmac.compare_digest(_hash_password(password, data["salt"]), data["hash"]):
            _login_failures[ip] = (failures + 1 if time.time() - last < LOGIN_LOCK_SECONDS else 1, time.time())
            self._login_page(401, "รหัสผ่านไม่ถูกต้อง")
            return
        _login_failures.pop(ip, None)
        token = os.urandom(24).hex()
        _login_tokens[token] = time.time() + SESSION_DAYS * 86400
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", f"{SESSION_COOKIE}={token}; Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; SameSite=Strict")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, data: dict):
        self._send(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        url = urlparse(self.path)
        if not self._allowed():
            if remote_enabled() and url.path in ("/", "/index.html", "/login"):
                self._login_page()
            else:
                self._send_json(401, {"error": "login required"})
            return
        if url.path == "/remote":
            if not self._is_local():
                self._send_json(403, {"error": "only on the computer running the bot"})
                return
            self._send_json(200, remote_status())
            return
        if url.path in ("/", "/index.html"):
            # index.html is the page; PAGE is the built-in fallback if the file is missing
            try:
                with open(os.path.join(BASE_DIR, "index.html"), "rb") as f:
                    body = f.read()
            except OSError:
                body = PAGE.encode("utf-8")
            self._send(200, body, "text/html; charset=utf-8")
        elif url.path == "/status":
            if self._is_local():  # only the app window on this computer keeps the app alive
                _window["seen"] = time.time()
                _window["bye"] = None
            try:
                since = int(parse_qs(url.query).get("since", ["0"])[0])
                session = get_session(query_device(url))
            except ValueError:
                self._send_json(400, {"error": "invalid request"})
                return
            with _lock:
                total = session.log_total
                count = max(0, min(total - since, len(session.logs)))
                lines = list(session.logs)[len(session.logs) - count:] if count else []
                sessions = list(_sessions.values())
            self._send_json(200, {
                "running": session.running(),       # the emulator being looked at
                "any_running": is_running(),        # any emulator
                "tool": session.tool if session.running() else None,  # treasure tool running instead of the bot
                "treasure": session.treasure,
                "total": total,
                "lines": lines,
                "boosts": [name for name, _ in BOOST_CHOICES],
                "rounds": session.rounds,
                "app": APP_NAME,
                "version": APP_VERSION,
                "stats": _stats_summary(session),
                "totals": _totals_copy(),
                # one line per emulator that has been started, for the emulator switcher and the grand total
                "sessions": [dict(_stats_summary(s), device=s.device, running=s.running()) for s in sessions],
            })
        elif url.path == "/rounds":
            try:
                session = get_session(query_device(url))
            except ValueError:
                self._send_json(400, {"error": "invalid request"})
                return
            with _lock:
                rounds = list(session.stats["rounds"])
            self._send_json(200, {"rounds": rounds})
        elif url.path == "/devices":
            try:
                self._send_json(200, {"devices": find_devices("refresh=1" in url.query)})
            except Exception as e:
                self._send_json(502, {"error": str(e)})
        elif url.path == "/history":
            self._send_json(200, {"history": load_history()})
        elif re.fullmatch(r"/(logo|box[1-4])\.png|/tool-img/[a-z]+\.png", url.path):
            try:
                with open(os.path.join(BASE_DIR, *url.path[1:].split("/")), "rb") as f:
                    self._send(200, f.read(), "image/png")
            except OSError:
                self._send_json(404, {"error": "not found"})
        elif re.fullmatch(r"/results/([A-Za-z0-9_][A-Za-z0-9_.-]*/)?(round|box|tool|toolbefore|toolafter)-[0-9-]+\.jpg", url.path):
            try:
                with open(os.path.join(RESULTS_PATH, *url.path.split("/")[2:]), "rb") as f:
                    self._send(200, f.read(), "image/jpeg")
            except OSError:
                self._send_json(404, {"error": "not found"})
        elif url.path == "/live.h264":
            # Raw H.264 straight from `adb screenrecord`: the page decodes it itself (WebCodecs), which is
            # far smoother than the JPEG stream below. screenrecord stops every 3 minutes, so it is restarted.
            # the emulator encodes small sizes much faster: 640x360 ~50 fps, 800x450 ~30 fps, full size ~20 fps
            size = {"smooth": ["--size", "640x360", "--bit-rate", "4000000"],
                    "balanced": ["--size", "800x450", "--bit-rate", "6000000"],
                    "sharp": ["--bit-rate", "8000000"]}.get(parse_qs(url.query).get("q", ["smooth"])[0])
            try:
                device = query_device(url)
                serial = device or (capture_png("") and adb._serial(DEVICE_IP, DEVICE_PORT))
                if size is None:
                    raise ValueError("Invalid quality")
            except Exception as e:
                self._send_json(502, {"error": str(e)})
                return
            self.send_response(200)
            self.send_header("Content-Type", "video/h264")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while True:
                    proc = subprocess.Popen(
                        [adb._adb(), "-s", serial, "exec-out", "screenrecord", "--output-format=h264", *size, "-"],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                    sent = 0
                    try:
                        while True:
                            chunk = proc.stdout.read(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            sent += len(chunk)
                    finally:
                        proc.kill()
                    if not sent:
                        return  # screenrecord does not work on this emulator: the page falls back to /live
            except OSError:
                pass  # the page stopped watching
        elif url.path == "/live":
            try:
                stream = live_stream(query_device(url))
            except ValueError:
                self._send_json(400, {"error": "invalid request"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                for jpeg in stream.frames():
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                     + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
            except OSError:
                pass  # the page stopped watching
        elif url.path == "/screenshot":
            try:
                self._send(200, capture_png(query_device(url)), "image/png")
            except Exception as e:
                self._send_json(502, {"error": str(e)})
        elif url.path == "/config":
            self._send_json(200, load_config())
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        # Only accept requests made from this page itself
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            self._send_json(403, {"error": "forbidden"})
            return
        if self.path == "/login" and not self._is_local() and remote_enabled():
            self._handle_login()
            return
        if not self._allowed():
            self._send_json(401, {"error": "login required"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_CONFIG_BYTES:
            self._send_json(413, {"error": "request too large"})
            return
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._send_json(400, {"error": "invalid json"})
            return
        if not isinstance(data, dict):
            self._send_json(400, {"error": "expected a json object"})
            return

        if self.path == "/config":
            save_config(data)
            self._send_json(200, {"saved": True})
            return
        if self.path == "/hearts-now":
            self._send_json(200, {"requested": request_hearts_now()})
            return
        if self.path == "/bye":
            if self._is_local():  # a phone closing its page must not close the app
                _window["bye"] = time.time()
            self._send_json(200, {"bye": True})
            return
        if self.path == "/remote":
            if not self._is_local():
                self._send_json(403, {"error": "only on the computer running the bot"})
                return
            password = data.get("password")
            if isinstance(password, str) and password:
                if len(password) < 6:
                    self._send_json(400, {"error": "รหัสผ่านต้องยาวอย่างน้อย 6 ตัว"})
                    return
                salt = os.urandom(16).hex()
                settings = load_remote()
                settings.update(salt=salt, hash=_hash_password(password, salt))
                save_remote(settings)
                _login_tokens.clear()  # a new password logs everyone out
            settings = load_remote()
            if data.get("enabled") is True:
                if not settings.get("hash"):
                    self._send_json(400, {"error": "ตั้งรหัสผ่านก่อน"})
                    return
                error = start_remote()
                if error:
                    self._send_json(500, {"error": error})
                    return
            elif data.get("enabled") is False:
                stop_remote()
            if isinstance(data.get("enabled"), bool):
                settings["enabled"] = data["enabled"]
                save_remote(settings)
            self._send_json(200, remote_status())
            return
        if self.path == "/quit":
            self._send_json(200, {"quitting": True})
            _quit_event.set()
            return

        if self.path == "/start":
            try:
                options = parse_options(data)
                devices = request_devices(data)
            except (TypeError, ValueError) as e:
                self._send_json(400, {"error": str(e)})
                return
            started = [device for device in devices if start_bot(options, device)]
            self._send_json(200, {"started": bool(started), "devices": started})
        elif self.path == "/tool":
            try:
                options = parse_tool_options(data)
                devices = request_devices(data)
                after = data.get("after", "lobby")
                if after not in AFTER_CHOICES:
                    raise ValueError("Invalid after")
                bot_options = parse_options(data["bot"]) if after == "bot" else None
            except (TypeError, ValueError, KeyError) as e:
                self._send_json(400, {"error": str(e)})
                return
            started = [device for device in devices if start_tool(options, device, (after, bot_options))]
            self._send_json(200, {"started": bool(started), "devices": started})
        elif self.path == "/stop":
            try:
                devices = request_devices(data) if data.get("devices") else [None]
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
                return
            self._send_json(200, {"stopped": any([stop_bot(device) for device in devices])})
        else:
            self._send_json(404, {"error": "not found"})


class Server(ThreadingHTTPServer):
    # On Windows, address reuse would let a second web.py share the port with one that is already running
    allow_reuse_address = False


def find_running_instance():
    """URL of a CookieRunBot server that is already running on this computer, or None."""
    for port in range(PORT, PORT + 10):
        try:
            with urllib.request.urlopen(f"http://{HOST}:{port}/status?since=999999999", timeout=0.5) as res:
                if json.load(res).get("app") == APP_NAME:
                    return f"http://{HOST}:{port}"
        except (OSError, ValueError):
            continue
    return None


def find_app_browser():
    """Edge or Chrome, used to show the UI in its own window (no tabs or address bar)."""
    roots = [os.environ.get(name, "") for name in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    for relative in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
        for root in roots:
            path = os.path.join(root, relative)
            if root and os.path.isfile(path):
                return path
    return None


def window_profile_dir() -> str:
    """Private browser profile of the app window, so it does not mix with the user's own browser."""
    return os.path.join(os.environ.get("LOCALAPPDATA") or BASE_DIR, APP_NAME, "window-profile")


def app_window_alive() -> bool:
    # Edge/Chrome keep a "lockfile" in the profile folder for exactly as long as they run with it
    return os.path.exists(os.path.join(window_profile_dir(), "lockfile"))


def note_exit(reason: str):
    """Records why the app closed itself, to make an unexpected exit explainable afterwards."""
    try:
        with open(os.path.join(os.path.dirname(window_profile_dir()), "last-exit.txt"), "w", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {reason}\n")
    except OSError:
        pass


def open_app_window(url: str):
    """Opens the UI as a standalone app window. Returns the window's process, or None if not possible."""
    browser = find_app_browser()
    if browser is None:
        return None
    profile = window_profile_dir()
    try:
        return subprocess.Popen([
            browser,
            f"--app={url}",
            f"--user-data-dir={profile}",
            f"--window-size={WINDOW_SIZE}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate,msEdgeTranslate",  # no "Translate page from Thai?" popup
            # keep the page polling at full speed while the window is minimized or covered
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
        ])
    except OSError:
        return None


def show_error(message: str):
    print(message)
    if FROZEN and os.name == "nt":  # the packaged app has no console to print to
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)


def main():
    # The bot's messages contain emoji; never let a console that cannot show them crash the server
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    use_window = "--no-browser" not in sys.argv

    # Already running: just show its window again instead of starting a second copy
    existing = find_running_instance() if use_window else None
    if existing:
        if open_app_window(existing) is None:
            webbrowser.open(existing)
        return

    server = None
    for port in range(PORT, PORT + 10):
        try:
            server = Server((HOST, port), Handler)
            break
        except OSError:
            continue
    if server is None:
        show_error(f"Could not open a port between {PORT} and {PORT + 9}.")
        return

    atexit.register(stop_bot)
    if load_remote().get("enabled") and load_remote().get("hash"):
        error = start_remote()
        print(f"Remote control: {error or f'on, port {REMOTE_PORT}'}")
    url = f"http://{HOST}:{server.server_address[1]}"
    print(f"CookieRun Classic Bot web UI: {url}")
    threading.Thread(target=server.serve_forever, daemon=True).start()

    window = open_app_window(url) if use_window else None
    if window is None and use_window:
        webbrowser.open(url)
    print("Close the app window to quit." if use_window else "Press Ctrl+C to quit.")
    _window["seen"] = time.time() + 30  # extra time for the first page load
    try:
        while not _quit_event.wait(0.5):
            if not use_window:
                continue
            if remote_enabled():
                # used from other devices: closing the window here leaves the bot running
                # (open CookieRunBot again to get the window back, or use "ปิดโปรแกรม")
                _window["bye"] = None
                continue
            now = time.time()
            bye = _window["bye"]
            if bye is not None and now - bye > BYE_GRACE_SECONDS:
                note_exit("window closed (page said bye)")
                break  # the page said it was closing and did not come back
            # Missing polls alone prove nothing (a minimized window may be put to sleep),
            # so the app only quits when the window's browser process is gone as well.
            if window is not None and now - _window["seen"] > APP_WINDOW_GONE_SECONDS and not app_window_alive():
                note_exit("window process gone")
                break
    except KeyboardInterrupt:
        pass
    finally:
        stop_bot()
        stop_remote()
        server.shutdown()
        server.server_close()
        if window is not None and window.poll() is None:
            window.terminate()


def run_bot_child(runner: str):
    """Packaged app only: the exe re-launches itself with --run-bot / --run-tool to run the bot or a tool."""
    os.chdir(BASE_DIR)
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    exec(runner, {"__name__": "__bot_runner__"})


if __name__ == "__main__":
    if "--run-bot" in sys.argv:
        run_bot_child(RUNNER)
    elif "--run-tool" in sys.argv:
        run_bot_child(TOOL_RUNNER)
    else:
        try:
            main()
        except Exception as e:
            show_error(f"{APP_NAME} could not start:\n{e}")
            raise
