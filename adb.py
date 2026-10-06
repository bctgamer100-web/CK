import glob
import os
import random
import shutil
import socket
import string
import subprocess
import time

import cv2
import numpy as np

GAME_PACKAGE = "com.devsisters.crg"

# adb executables bundled with common emulators (relative to a drive root)
_ADB_SEARCH_PATTERNS = (
    r"Program Files\Netease\MuMu*\shell\adb.exe",
    r"Program Files\Netease\MuMu*\nx_main\adb.exe",
    r"Program Files\Netease\MuMu*\emulator\nemu\vmonitor\bin\adb_server.exe",
    r"Program Files\MuMu*\shell\adb.exe",
    r"Program Files\MuMu*\nx_main\adb.exe",
    r"Program Files (x86)\Netease\MuMu*\shell\adb.exe",
    r"Program Files (x86)\Netease\MuMu*\emulator\nemu\vmonitor\bin\adb_server.exe",
    r"Netease\MuMu*\shell\adb.exe",
    r"Netease\MuMu*\nx_main\adb.exe",
    r"MuMu*\shell\adb.exe",
    r"MuMu*\nx_main\adb.exe",
    r"LDPlayer\LDPlayer*\adb.exe",
    r"LDPlayer*\adb.exe",
    r"Program Files\LDPlayer\LDPlayer*\adb.exe",
    r"XuanZhi\LDPlayer*\adb.exe",
    r"Program Files\BlueStacks*\HD-Adb.exe",
    r"Program Files (x86)\BlueStacks*\HD-Adb.exe",
    r"Program Files\Nox\bin\nox_adb.exe",
    r"Program Files (x86)\Nox\bin\nox_adb.exe",
    r"Nox\bin\nox_adb.exe",
    r"Program Files\Microvirt\MEmu\adb.exe",
    r"Microvirt\MEmu\adb.exe",
    r"platform-tools\adb.exe",
)

# Default adb ports of common emulators (first few instances of each)
_COMMON_PORTS = (
    [7555]                                    # MuMu 6
    + [16384 + 32 * i for i in range(8)]      # MuMu 12
    + [5555 + 2 * i for i in range(8)]        # LDPlayer / BlueStacks / AVD
    + [5565 + 10 * i for i in range(4)]       # BlueStacks extra instances
    + [62001] + [62025 + i for i in range(4)] # Nox
    + [21503 + 10 * i for i in range(4)]      # MEmu
)

def _run(*args, **kwargs):
    # CREATE_NO_WINDOW: adb must not flash a console window when the bot runs as a windowed app
    if os.name == "nt":
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
    return subprocess.run(*args, **kwargs)


# When True, taps and swipes are skipped (the bot only logs what it would do)
DRY_RUN = False

# When several emulators are used, each bot process is told which one is its own (see web.py)
DEVICE_SERIAL = os.environ.get("BOT_DEVICE") or None

_adb_path = None
_resolved_serials = {}  # "ip:port" from config -> serial that actually works


def _find_adb() -> str:
    env_path = os.environ.get("ADB_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path

    found = shutil.which("adb")
    if found:
        return found

    exe = "adb.exe" if os.name == "nt" else "adb"
    here = os.path.dirname(os.path.abspath(__file__))
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(here, exe),
        os.path.join(here, "platform-tools", exe),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk", "platform-tools", exe),
        os.path.join(os.environ.get("ANDROID_HOME", ""), "platform-tools", exe),
        os.path.join(os.environ.get("ANDROID_SDK_ROOT", ""), "platform-tools", exe),
        os.path.join(home, "Library", "Android", "sdk", "platform-tools", exe),
        os.path.join(home, "Android", "Sdk", "platform-tools", exe),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate

    if os.name == "nt":
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if not os.path.exists(root):
                continue
            for pattern in _ADB_SEARCH_PATTERNS:
                matches = sorted(glob.glob(os.path.join(root, pattern)), reverse=True)
                if matches:
                    return matches[0]

    raise Exception(
        "❌ adb not found. Install Android platform-tools (or an emulator), add adb to PATH, "
        "put adb.exe next to this script, or set the ADB_PATH environment variable."
    )


def _adb() -> str:
    global _adb_path
    if _adb_path is None:
        _adb_path = _find_adb()
        print(f"🔧 Using adb: {_adb_path}")
    return _adb_path


def _serial(ip: str, port: int) -> str:
    if DEVICE_SERIAL:
        return DEVICE_SERIAL
    key = f"{ip}:{port}"
    return _resolved_serials.get(key, key)


def _adb_run(args, timeout: int = 20):
    try:
        return _run(
            [_adb(), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 1, "", "timeout")


def _is_device_ready(serial: str, retries: int = 6) -> bool:
    for _ in range(retries):
        if _adb_run(["-s", serial, "get-state"]).stdout.strip() == "device":
            return True
        time.sleep(0.5)
    return False


def _is_port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def _list_ready_devices():
    lines = _adb_run(["devices"]).stdout.splitlines()[1:]
    return [line.split()[0] for line in lines if line.strip().endswith("device")]


def _auto_detect_device(ip: str, skip: str):
    candidates = [s for s in _list_ready_devices() if s != skip]

    hosts = [ip] if ip == "127.0.0.1" else [ip, "127.0.0.1"]
    for host in hosts:
        for port in _COMMON_PORTS:
            serial = f"{host}:{port}"
            if serial == skip or serial in candidates or not _is_port_open(host, port):
                continue
            _adb_run(["connect", serial])
            if _is_device_ready(serial, retries=3):
                candidates.append(serial)

    if not candidates:
        return None
    # Prefer the device that actually has the game installed
    for serial in candidates:
        if _adb_run(["-s", serial, "shell", "pm", "path", GAME_PACKAGE]).stdout.startswith("package:"):
            return serial
    return candidates[0]


def _serial_preference(serial: str):
    """Sort key: "host:port" names first (highest port first), "emulator-5554" style names last."""
    host, _, port = serial.rpartition(":")
    return (0, -int(port)) if host and port.isdigit() else (1, 0)


def discover_devices(ip: str = "127.0.0.1"):
    """Finds every running emulator/device. One emulator is often reachable under several names
    (e.g. 127.0.0.1:16384, 127.0.0.1:7555 and emulator-5554), so names are grouped by the boot id
    of the Android system behind them. Returns [{"serial", "aliases", "game"}], one per emulator."""
    known = set(_list_ready_devices())
    for port in _COMMON_PORTS:
        serial = f"{ip}:{port}"
        if serial not in known and _is_port_open(ip, port):
            _adb_run(["connect", serial])
    groups = {}
    for serial in _list_ready_devices():
        boot_id = _adb_run(["-s", serial, "shell", "cat", "/proc/sys/kernel/random/boot_id"]).stdout.strip()
        groups.setdefault(boot_id or serial, []).append(serial)
    devices = []
    for names in groups.values():
        names.sort(key=_serial_preference)
        game = _adb_run(["-s", names[0], "shell", "pm", "path", GAME_PACKAGE]).stdout.startswith("package:")
        devices.append({"serial": names[0], "aliases": names[1:], "game": game})
    # lowest port first, so the first emulator instance is listed first
    devices.sort(key=lambda device: (_serial_preference(device["serial"])[0], -_serial_preference(device["serial"])[1],
                                     device["serial"]))
    return devices


def capture_png_serial(serial: str) -> bytes:
    result = _run(
        [_adb(), "-s", serial, "exec-out", "screencap", "-p"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20
    )
    if not result.stdout.startswith(b"\x89PNG"):
        raise Exception(f"screencap failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def device_connect(ip: str, port: int):
    if DEVICE_SERIAL:
        if ":" in DEVICE_SERIAL:
            _adb_run(["connect", DEVICE_SERIAL])
        if not _is_device_ready(DEVICE_SERIAL):
            raise Exception(f"❌ Emulator {DEVICE_SERIAL} is not available. Is it running with ADB enabled?")
        print(f"🔌 Connected to {DEVICE_SERIAL}")
        return
    result = _adb_run(["connect", f"{ip}:{port}"])
    print(f"🔌 {result.stdout.strip().capitalize()}")
    if ("connected" in result.stdout or "already connected" in result.stdout) and _is_device_ready(f"{ip}:{port}"):
        return

    print(f"🔍 {ip}:{port} is not available, searching for a running emulator/device...")
    serial = _auto_detect_device(ip, skip=f"{ip}:{port}")
    if serial is None:
        raise Exception(
            f"❌ Failed to connect to {ip}:{port} and no other emulator/device was found.\n"
            f"{result.stderr.strip()}\n"
            "Make sure the emulator is running and ADB debugging is enabled."
        )
    _resolved_serials[f"{ip}:{port}"] = serial
    print(f"🔌 Connected to auto-detected device: {serial}")


def device_capture_screen(ip: str, port: int):
    result = _run(
        [_adb(), "-s", _serial(ip, port), "exec-out", "screencap", "-p"],
        stdout=subprocess.PIPE,
        check=True
    )
    img = np.frombuffer(result.stdout, dtype=np.uint8)
    return cv2.imdecode(img, cv2.IMREAD_COLOR)


def device_capture_png(ip: str, port: int) -> bytes:
    result = _run(
        [_adb(), "-s", _serial(ip, port), "exec-out", "screencap", "-p"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20
    )
    if not result.stdout.startswith(b"\x89PNG"):
        raise Exception(f"screencap failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def device_tap(ip: str, port: int, x: int, y: int):
    if DRY_RUN:
        return
    _run(
        [_adb(), "-s", _serial(ip, port),"shell", "input", "tap", str(x), str(y)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )


def safe_device_tap(ip: str, port: int, x: int, y: int):
    jitter_x = x + random.randint(-15, 15)
    jitter_y = y + random.randint(-15, 15)
    if DRY_RUN:
        return
    _run(
        [_adb(), "-s", _serial(ip, port),"shell", "input", "tap", str(jitter_x), str(jitter_y)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )


def safe_device_scroll(ip: str, port: int, x: int, y: int, direction: str = "up", distance: int = 500, duration: int = 300):
    jx = x + random.randint(-15, 15)
    jy = y + random.randint(-15, 15)
    direction_map = {
        "up":    (jx, jy + distance, jx, jy - distance),
        "down":  (jx, jy - distance, jx, jy + distance),
        "left":  (jx + distance, jy, jx - distance, jy),
        "right": (jx - distance, jy, jx + distance, jy),
    }
    if direction not in direction_map:
        raise ValueError(f"Invalid direction '{direction}'. Use: up, down, left, right.")
    x1, y1, x2, y2 = direction_map[direction]
    if DRY_RUN:
        return
    _run(
        [_adb(), "-s", _serial(ip, port),"shell", "input", "swipe",
         str(x1), str(y1), str(x2), str(y2), str(duration)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )


def device_is_app_running(ip: str, port: int, package: str) -> bool:
    result = _run(
        [_adb(), "-s", _serial(ip, port),"shell", "pidof", package],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    return bool(result.stdout.strip())


def device_reset_app(ip: str, port: int, package: str = "com.devsisters.crg", max_retries: int = 5):
    print(f"🔄 Resetting app {package} on device at {ip}:{port}...")
    _run(
        [_adb(), "-s", _serial(ip, port),"shell", "cmd", "activity", "force-stop", package],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    print(f"⏳ Waiting 15 seconds for app {package} to stop...")
    time.sleep(15)

    for attempt in range(1, max_retries + 1):
        print(f"📱 Restarting app {package} on device at {ip}:{port} (attempt {attempt}/{max_retries})...")
        _run(
            [_adb(), "-s", _serial(ip, port),"shell", "monkey", "-p", package, "1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        print(f"⏳ Waiting 15 seconds to check if app started...")
        time.sleep(15)

        if device_is_app_running(ip, port, package):
            print(f"📊 App {package} is running, verifying stability...")
            stable = True
            for check in range(1, 4):
                time.sleep(20)
                if not device_is_app_running(ip, port, package):
                    print(f"💥 App {package} crashed during stability check ({check}/3).")
                    stable = False
                    break
                print(f"✅ Stability check {check}/3 passed.")
            if stable:
                print(f"✅ App {package} is stable.")
                return

        print(f"💥 App {package} appears to have crashed after launch.")
        if attempt < max_retries:
            print(f"🔁 Retrying in 5 seconds...")
            time.sleep(5)

    raise Exception(f"❌ Failed to start {package} after {max_retries} attempts.")
