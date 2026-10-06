"""Treasure tools started from the web UI: upgrade every treasure to +9, farm Magic Powder,
open Free Treasure Tickets and farm Mystic Shards.

Every tool starts and ends at the lobby. It only taps what it has recognised on screen and stops
(saving the screen to results/) as soon as it sees something it does not know."""
import json
import os
import random
import time

import cv2
import numpy as np

import adb
from adb import device_capture_screen, device_connect, device_tap
from bot import RESULTS_DIR, emit_stat, save_shot
from config import DEVICE_IP, DEVICE_PORT, TEMPLATE_DIR
from detection import (_get_template_gray, _load_digit_templates, _normalize_glyph, _normalize_gray, _split_touching,
                       _trim_glyph, detect_stage)

THRESHOLD = 0.8

# name -> (template files, search region (x1, y1, x2, y2))
T = {
    "LOBBY_BUTTON":     (["TREASURE_LOBBY_BUTTON_1.png"], (1000, 510, 1180, 590)),
    "MAIN":             (["TREASURE_MAIN_1.png"], (520, 600, 760, 690)),
    "UPGRADE":          (["TREASURE_UPGRADE_1.png"], (690, 120, 1070, 195)),
    "UPGRADE_PICKER":   (["TREASURE_UPGRADE_PICKER_1.png"], (440, 55, 840, 112)),
    "DRAW":             (["TREASURE_DRAW_1.png"], (560, 40, 870, 100)),
    "CABINET":          (["TREASURE_CABINET_1.png"], (460, 65, 690, 130)),
    "EXTRACT":          (["TREASURE_EXTRACT_1.png"], (440, 55, 840, 112)),
    "EVOLVE":           (["TREASURE_EVOLVE_1.png"], (720, 35, 1060, 100)),
    "EVOLVE_PICKER":    (["TREASURE_EVOLVE_PICKER_1.png"], (440, 55, 840, 112)),
    "SEARCH":           (["TREASURE_SEARCH_1.png"], (420, 200, 860, 280)),
    "RECEIVED":         (["TREASURE_RECEIVED_1.png"], (430, 315, 850, 380)),
    "NO_SPACE":         (["TREASURE_NO_SPACE_1.png"], (440, 225, 840, 305)),
    "UPGRADE_SUCCESS":  (["TREASURE_UPGRADE_SUCCESS_1.png"], (380, 165, 900, 225)),
    "POPUP_CLOSE":      (["TREASURE_POPUP_CLOSE_1.png"], (950, 70, 1070, 160)),
    "CONFIRM":          (["TREASURE_CONFIRM_1.png"], (440, 380, 840, 640)),
    "EXTRACT_CONFIRM":  (["TREASURE_EXTRACT_CONFIRM_1.png"], (300, 195, 980, 265)),
    "EXTRACT_SUCCESS":  (["TREASURE_EXTRACT_SUCCESS_1.png"], (420, 275, 860, 345)),
    "FREE_TICKET":      (["TREASURE_FREE_TICKET_1.png"], (190, 520, 390, 600)),
    "COIN_BOX":         (["TREASURE_COIN_BOX_1.png"], (195, 290, 385, 365)),
    "REGULAR":          (["TREASURE_REGULAR_1.png"], (690, 370, 870, 445)),
    "MISSING":          (["TREASURE_MISSING_INGREDIENTS_1.png"], (800, 630, 1090, 710)),
    "MISSING_TREASURE": (["TREASURE_MISSING_TREASURE_1.png"], (800, 630, 1090, 710)),
    "EVOLVE_SELECT":    (["TREASURE_EVOLVE_SELECT_1.png"], (840, 635, 1060, 710)),
    "MAX_LEVEL":        (["TREASURE_MAX_LEVEL_1.png"], (760, 580, 1120, 645)),
    "FULLY_UPGRADED":   (["TREASURE_FULLY_UPGRADED_1.png"], (700, 400, 1040, 550)),
    "CRAFT_PLUS":       (["TREASURE_CRAFT_PLUS_1.png", "TREASURE_CRAFT_PLUS_2.png"], (820, 500, 1160, 640)),
    "EVOLVE_READY":     (["TREASURE_EVOLVE_COST_1.png"], (840, 360, 1110, 450)),  # "Regular (coin) 26,000"
    "EVO_SUCCESS":      (["TREASURE_EVOLVE_SUCCESS_1.png"], (420, 300, 860, 400)),
    "CRAFT_BUTTON":     (["TREASURE_CRAFT_BUTTON_1.png"], (440, 550, 840, 640)),
    "CRAFT_CONFIRM":    (["TREASURE_CRAFT_CONFIRM_1.png"], (380, 240, 900, 370)),
    "CRAFT_DONE":       (["TREASURE_CRAFT_DONE_1.png"], (440, 270, 840, 350)),
    "SORT_ASC":         (["TREASURE_SORT_ASC_ARROW_1.png"], (350, 75, 405, 115)),
    "SORT_DESC":        (["TREASURE_SORT_DESC_ARROW_1.png"], (350, 75, 405, 115)),
}

# Close (X) button of each window, used to walk back to the lobby
CLOSE = {
    "EXTRACT": (1143, 105), "UPGRADE_PICKER": (1143, 105), "EVOLVE_PICKER": (1143, 105),
    "CABINET": (1125, 98), "DRAW": (1125, 100), "UPGRADE": (1130, 93), "EVOLVE": (1108, 84),
    "MAIN": (1123, 100),
}
# Deepest window first: a window on top hides the ones below it
SCREENS = ("EXTRACT", "UPGRADE_PICKER", "EVOLVE_PICKER", "CABINET", "DRAW", "UPGRADE", "EVOLVE", "MAIN")

LOBBY_TREASURE_BUTTON = (1088, 548)
MAIN_DRAW_BUTTON = (300, 645)
MAIN_UPGRADE_BUTTON = (630, 645)
MAIN_EVOLVE_BUTTON = (930, 645)
MAIN_CABINET_BUTTON = (228, 103)
DRAW_CABINET_BUTTON = (233, 100)
DRAW_COIN_BOX_BUTTON = (287, 328)
DRAW_FREE_TICKET_BUTTON = (288, 555)
RESULT_CONFIRM_BUTTON = (640, 568)
NO_SPACE_CONFIRM_BUTTON = (640, 460)  # "Not enough space!" popup
UPGRADE_SELECT_BUTTON = (380, 650)
UPGRADE_REGULAR_BUTTON = (877, 408)
PICKER_SELECT_BUTTON = (936, 607)
PICKER_SEARCH_BUTTON = (160, 95)
PICKER_SEARCH_CLEAR = (446, 94)
SEARCH_FIELD = (640, 366)
SEARCH_CONFIRM = (790, 482)
CABINET_EXTRACT_BUTTON = (600, 685)
EXTRACT_BUTTON = (940, 673)
EXTRACT_CONFIRM_BUTTON = (640, 519)
EXTRACT_SUCCESS_CONFIRM = (640, 458)
SORT_BUTTON = (340, 95)
SORT_TIER_BUTTON = (235, 330)
EVOLVE_RECIPE_BUTTON = (370, 302)
EVOLVE_SELECT_BUTTON = (948, 672)
EVOLVE_REGULAR_BUTTON = (893, 405)
INGREDIENT_SLOTS = ((885, 560), (990, 560), (1095, 560))
INGREDIENT_INFO_CLOSE = (927, 100)
MAGIC_POWDER_REGION = (204, 680, 314, 702)  # below the "Magic Powder" label, whose outline would touch the digits
MYSTIC_SHARD_REGION = (420, 680, 488, 702)

# Treasure grid of the pickers and of the cabinet (4 x 4 visible slots)
SLOT_X = (208, 345, 483, 620)
SLOT_Y = (185, 320, 457, 592)
SLOTS = [(x, y) for y in SLOT_Y for x in SLOT_X]

# Treasures the user can ask to keep: name -> icon templates (missing files are skipped)
KEEP_TEMPLATES = {
    "cookie": ["KEEP_COOKIE_1.png", "KEEP_COOKIE_2.png"],
    "pill": ["KEEP_PILL_1.png"],
    "pearl": ["KEEP_PEARL_1.png"],
    "hammer": ["KEEP_HAMMER_1.png"],
    "horse": ["KEEP_HORSE_1.png"],
}
KEEP_NAMES = {"cookie": "Famous Choco Chip Cookie", "pill": "ยาเหลือง", "pearl": "ไข่มุก",
              "hammer": "ค้อน", "horse": "ม้า"}

SHARDS_PER_COOKIE = 9
DEFAULT_DRAWS_BEFORE_EXTRACT = 10
UNKNOWN_SCREEN_SECONDS = 12


class ToolStop(Exception):
    """Stops the tool with a message for the log (not an error in the code)."""


# ---------- screen ----------
def screen():
    """The emulator screen. Raw screencap is about twice as fast as the PNG one (no compression)."""
    try:
        import struct
        import subprocess
        data = adb._run([adb._adb(), "-s", adb._serial(DEVICE_IP, DEVICE_PORT), "exec-out", "screencap"],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10).stdout
        width, height, _ = struct.unpack("<III", data[:12])
        rgba = np.frombuffer(data, np.uint8, offset=len(data) - width * height * 4).reshape(height, width, 4)
        return cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)
    except Exception:
        return device_capture_screen(DEVICE_IP, DEVICE_PORT)


def tap(point, jitter=6, wait=0.9):
    x, y = point
    device_tap(DEVICE_IP, DEVICE_PORT, x + random.randint(-jitter, jitter), y + random.randint(-jitter, jitter))
    time.sleep(wait + random.uniform(0, 0.3))


def score(img, name, region=None):
    """Best match (0..1) of a template inside its region, and the centre of that match."""
    files, default_region = T[name] if name in T else (KEEP_TEMPLATES[name], None)
    region = region or default_region
    gray = _normalize_gray(img)
    if gray is None:
        return 0.0, None
    x1, y1 = (region[0], region[1]) if region else (0, 0)
    area = gray[region[1]:region[3], region[0]:region[2]] if region else gray
    best, center = 0.0, None
    for filename in files:
        template = _get_template_gray(filename)
        if template is None or area.shape[0] < template.shape[0] or area.shape[1] < template.shape[1]:
            continue
        _, value, _, loc = cv2.minMaxLoc(cv2.matchTemplate(area, template, cv2.TM_CCOEFF_NORMED))
        if value > best:
            th, tw = template.shape[:2]
            best, center = value, (x1 + loc[0] + tw // 2, y1 + loc[1] + th // 2)
    return best, center


def seen(img, name, region=None):
    return score(img, name, region)[0] >= THRESHOLD


# The three pickers share one banner ("Select a Treasure to ...!"): the best match decides
PICKERS = ("EXTRACT", "UPGRADE_PICKER", "EVOLVE_PICKER")


def where(img):
    """Which treasure window is on top, "LOBBY", or None."""
    scores = {name: score(img, name)[0] for name in PICKERS}
    best = max(scores, key=scores.get)
    if scores[best] >= THRESHOLD:
        return best
    for name in SCREENS:
        if name not in PICKERS and seen(img, name):
            return name
    if detect_stage(img, ["MAINMENU"]) == "MAINMENU" or seen(img, "LOBBY_BUTTON"):
        return "LOBBY"
    return None


def wait_for(names, timeout=10.0):
    """Waits until one of the windows/popups is on screen. Returns (name, screen) or (None, last screen)."""
    end = time.time() + timeout
    img = None
    while time.time() < end:
        img = screen()
        for name in names:
            if name == "LOBBY" or name in PICKERS:
                if where(img) == name:
                    return name, img
            elif seen(img, name):
                return name, img
        time.sleep(0.4)
    return None, img


def give_up(message, img=None):
    shot = save_shot("tool", img if img is not None else screen())
    raise ToolStop(message + (f" (บันทึกหน้าจอไว้ที่ {RESULTS_DIR}/{shot})" if shot else ""))


def expect(names, timeout=10.0, what="หน้าจอที่ต้องการ"):
    name, img = wait_for(names if isinstance(names, (list, tuple)) else [names], timeout)
    if name is None:
        give_up(f"ไม่เจอ{what} หยุดทำงานเพื่อความปลอดภัย", img)
    return name, img


# ---------- navigation ----------
def close_popups(img):
    """Taps away a result popup if one is open. Returns True when it tapped something."""
    for name, point in (("EXTRACT_SUCCESS", EXTRACT_SUCCESS_CONFIRM), ("RECEIVED", RESULT_CONFIRM_BUTTON),
                        ("NO_SPACE", NO_SPACE_CONFIRM_BUTTON)):
        if seen(img, name):
            tap(point)
            return True
    if seen(img, "UPGRADE_SUCCESS") or seen(img, "POPUP_CLOSE"):
        tap(score(img, "POPUP_CLOSE")[1] or (1008, 113))
        return True
    if seen(img, "SEARCH"):
        tap((489, 482))  # Cancel
        return True
    if seen(img, "EXTRACT_CONFIRM"):
        tap((998, 114))  # X of "Extract from selected Treasures?": never confirm an extraction we did not prepare
        return True
    return False


def to_lobby():
    for _ in range(15):
        img = screen()
        if close_popups(img):
            continue
        place = where(img)
        if place == "LOBBY":
            return
        if place in CLOSE:
            tap(CLOSE[place])
            continue
        time.sleep(1.5)
    give_up("กลับหน้าล็อบบี้ไม่ได้")


def to_main():
    """Opens the Treasure window (Cabinet / Draw / Upgrade / Evolve buttons) from wherever we are."""
    for _ in range(15):
        img = screen()
        if close_popups(img):
            continue
        place = where(img)
        if place == "MAIN":
            return
        if place == "LOBBY":
            tap(LOBBY_TREASURE_BUTTON, wait=1.5)
            continue
        if place in CLOSE:
            tap(CLOSE[place])
            continue
        time.sleep(1.5)
    give_up("เปิดหน้าสมบัติ (Treasure) ไม่ได้ ให้เปิดเกมค้างไว้ที่หน้าล็อบบี้ก่อนเริ่ม")


def open_from_main(button, name, what):
    to_main()
    tap(button, wait=1.2)
    expect(name, what=what)


def open_cabinet():
    to_main()
    tap(MAIN_CABINET_BUTTON, wait=1.2)
    expect("CABINET", what="หน้าคลังสมบัติ (Cabinet)")


def _read_text(img, region, light=False):
    """The characters of digit size that share one baseline in a region, left to right: digits matched
    against the digit shapes used for the Result screen, "?" for anything else (such as a "/").
    `light` is for white text on a dark background."""
    gray = _normalize_gray(img)
    if gray is None:
        return ""
    x1, y1, x2, y2 = region
    area = gray[y1:y2, x1:x2]
    mask = (area > 200 if light else area < 140).astype(np.uint8)
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    blobs = [stats[i] for i in range(1, count) if 14 <= stats[i][3] <= 26 and 4 <= stats[i][2] <= 60]
    if not blobs:
        return ""
    baseline = max(set(b[1] + b[3] for b in blobs), key=lambda v: sum(abs(b[1] + b[3] - v) <= 2 for b in blobs))
    digits = sorted((b for b in blobs if abs(b[1] + b[3] - baseline) <= 3), key=lambda b: b[0])
    # the common top row of the digits: a digit touching an outline above it is cut back to it
    top = max(set(b[1] for b in digits), key=lambda v: sum(abs(b[1] - v) <= 1 for b in digits))
    templates = _load_digit_templates()
    text = ""
    for x, y, w, h, _ in digits:
        glyph = mask[max(y, top):y + h, x:x + w].copy()
        # keep only the biggest piece: bits of the outline that were joined through the cut rows fall away
        n, labels, parts, _ = cv2.connectedComponentsWithStats(glyph, connectivity=8)
        if n > 2:
            glyph = (labels == 1 + int(np.argmax(parts[1:, cv2.CC_STAT_AREA]))).astype(np.uint8)
        glyph = _trim_glyph(glyph)
        if not glyph.size:
            continue
        for glyph in _split_touching(glyph):  # digits such as "50" can touch
            shape = _normalize_glyph(glyph).astype(np.float32)
            scores = {d: 1 - float(np.abs(shape - t).mean()) / 255 for d, t in templates.items()}
            best = max(scores, key=scores.get)
            text += best if scores[best] >= 0.75 else "?"  # a digit cut from a touching pair matches a little worse
    return text


def read_counter(img, region):
    """Reads a counter such as "301,935" (None when unreadable)."""
    text = _read_text(img, region)
    return int(text) if text and text.isdigit() and len(text) <= 9 else None


CABINET_SIZE_REGION = (930, 82, 1085, 116)  # "201 / 224" at the top of the cabinet


def read_cabinet_size(img=None):
    """(treasures in the cabinet, cabinet size) from the "201 / 224" counter, or None when unreadable."""
    img = img if img is not None else screen()
    parts = [p for p in _read_text(img, CABINET_SIZE_REGION, light=True).split("?") if p]
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    used, size = int(parts[0]), int(parts[1])
    return (used, size) if 0 <= used <= size <= 2000 else None


def read_counts(img=None):
    """Magic Powder and Mystic Shard shown at the bottom of the cabinet (None when unreadable)."""
    img = img if img is not None else screen()
    powder, shard = read_counter(img, MAGIC_POWDER_REGION), read_counter(img, MYSTIC_SHARD_REGION)
    if powder is not None or shard is not None:
        emit_stat("treasure", powder=powder, shard=shard)
    return powder, shard


def search(text):
    """Filters the open picker by treasure name."""
    serial = adb._serial(DEVICE_IP, DEVICE_PORT)
    tap(PICKER_SEARCH_BUTTON)
    expect("SEARCH", what="ช่องค้นหาสมบัติ")
    tap(SEARCH_FIELD, jitter=3)
    # the field can still hold the last search: go to its end and delete everything first
    adb._adb_run(["-s", serial, "shell", "input", "keyevent", "123"] + ["67"] * 40)
    adb._adb_run(["-s", serial, "shell", "input", "text", text.replace(" ", "%s")])
    time.sleep(0.8)
    adb._adb_run(["-s", serial, "shell", "input", "keyevent", "66"])  # Enter closes the keyboard
    time.sleep(0.8)
    tap(SEARCH_CONFIRM, jitter=4, wait=1.2)


# ---------- treasure slots ----------
def _rgb(img, x, y):
    b, g, r = (int(v) for v in img[y, x][:3])
    return r, g, b


def _frame(color):
    r, g, b = color
    if 140 <= r <= 215 and g > 225 and b < 120:
        return "selected"  # the lime frame of a picked treasure (its shading goes down to about 164,248,42)
    if r < 175 and 180 <= g <= 225 and b > 220:
        return "B"
    if r > 240 and g > 205 and b < 150:
        return "A"
    if all(c > 228 for c in color) and max(color) - min(color) < 20:
        return "C"
    if 150 <= r <= 192 and max(color) - min(color) <= 4:
        return "C"  # the silver-grey frame of C treasures: pure grey, unlike the warm grey of an empty slot
    if all(180 <= c <= 225 for c in color) and max(color) - min(color) < 15:
        return "empty"  # empty slot, or the bare list background after a search
    return None


def slot_kind(img, slot):
    """Grade of the treasure in a slot from its frame colour: "C", "B", "A", "S", "empty",
    "selected" (lime frame of a picked treasure) or "other" (dimmed, e.g. the treasure in use)."""
    x, y = slot
    samples = [_frame(_rgb(img, x + dx, y + dy)) for dx in (-50, 50) for dy in (-15, 0)]
    if samples.count("selected") >= 3:
        return "selected"  # one point on the frame's shading must not make a picked slot look like something else
    kinds = set(samples)
    if len(kinds) == 1 and None not in kinds:
        return kinds.pop()
    if None in kinds:
        # rainbow frames change colour around the slot; a dimmed slot (treasure in use) is dark
        return "other" if max(_rgb(img, x - 50, y)) < 140 else "S"
    return "other"


def has_plus9(img, slot):
    """The "+9" badge in the bottom-right corner of a slot (the treasure is at the highest level)."""
    x, y = slot
    return _match_files(img, ["TREASURE_PLUS9_1.png"], (x - 10, y - 10, x + 62, y + 62)) >= 0.7


LIST_BACKGROUND = (218, 216, 211)


def _is_background(color):
    # tight: the inside of an empty slot is only a few shades darker than the list background
    return all(abs(c - t) <= 5 for c, t in zip(color, LIST_BACKGROUND))


def grid_slots(img):
    """Slots of the rows that are fully visible after scrolling a list. The rows are found from the
    background-coloured gaps between them, since a scroll never stops exactly on a row."""
    gap_rows = [y for y in range(108, 682) if all(_is_background(_rgb(img, x, y)) for x in SLOT_X)]
    gaps, start = [], None
    for prev, y in zip([None] + gap_rows, gap_rows):
        if prev is None or y != prev + 1:
            if start is not None:
                gaps.append((start, prev))
            start = y
    if start is not None:
        gaps.append((start, gap_rows[-1]))
    rows = []
    for (_, top_end), (bottom_start, _) in zip(gaps, gaps[1:]):
        if 100 <= bottom_start - top_end <= 125:
            rows.append((top_end + bottom_start) // 2)
    return [(x, y) for y in rows for x in SLOT_X]


def slot_id(img, slot):
    """A small fingerprint of the treasure picture in a slot, to recognise it again after scrolling."""
    x, y = slot
    gray = _normalize_gray(img)[y - 40:y + 30, x - 40:x + 40]
    return cv2.resize(gray, (8, 8), interpolation=cv2.INTER_AREA).tobytes()


def is_favorite(img, slot):
    """The star badge in the bottom-left corner of a slot."""
    x, y = slot
    return _match_files(img, ["TREASURE_FAVORITE_1.png"], (x - 66, y, x, y + 62)) >= THRESHOLD


def _match_files(img, files, region):
    gray = _normalize_gray(img)
    area = gray[max(0, region[1]):region[3], max(0, region[0]):region[2]]
    best = 0.0
    for filename in files:
        if not os.path.exists(os.path.join(TEMPLATE_DIR, filename)):
            continue  # an optional picture nobody has added yet
        template = _get_template_gray(filename)
        if template is None or area.shape[0] < template.shape[0] or area.shape[1] < template.shape[1]:
            continue
        best = max(best, cv2.minMaxLoc(cv2.matchTemplate(area, template, cv2.TM_CCOEFF_NORMED))[1])
    return best


def kept(img, slot, keep):
    x, y = slot
    for name in keep:
        if _match_files(img, KEEP_TEMPLATES.get(name, []), (x - 56, y - 56, x + 56, y + 56)) >= THRESHOLD:
            return name
    return None


def warn_missing_keep_templates(keep):
    for name in keep:
        if not any(os.path.exists(os.path.join(TEMPLATE_DIR, f)) for f in KEEP_TEMPLATES.get(name, [])):
            print(f"⚠️ ยังไม่มีรูปของ {KEEP_NAMES.get(name, name)} ให้บอทจำ: ถ้าไม่อยากให้ย่อย ให้กดปักดาวในเกมไว้")


# ---------- extract ----------
def ensure_sort_ascending():
    """Lowest tier first, so the B/C treasures are at the top of the list and no scrolling is needed."""
    for _ in range(3):
        img = screen()
        up, down = score(img, "SORT_ASC")[0], score(img, "SORT_DESC")[0]
        if up >= THRESHOLD and up > down:
            return
        tap(SORT_BUTTON, wait=1.0)
        tap(SORT_TIER_BUTTON, wait=1.2)
    give_up("ตั้งให้เรียงสมบัติจากระดับต่ำไปสูงไม่ได้")


def extract_junk(keep, include_gold):
    """Extracts the low-tier treasures (C/B, and A when include_gold) into Magic Powder.
    Favorites are never touched (the game does not let them be picked). Returns how many were extracted."""
    grades = {"C", "B", "A"} if include_gold else {"C", "B"}
    total = 0
    while True:
        open_cabinet()
        tap(CABINET_EXTRACT_BUTTON, wait=1.2)
        expect("EXTRACT", what="หน้าย่อยสมบัติ (Extract)")
        ensure_sort_ascending()
        picked = select_junk(keep, include_gold, grades)
        if not picked:
            tap(CLOSE["EXTRACT"])
            return total
        tap(EXTRACT_BUTTON, wait=1.2)
        expect("EXTRACT_CONFIRM", what="หน้ายืนยันการย่อย")
        tap(EXTRACT_CONFIRM_BUTTON, wait=1.5)
        expect("EXTRACT_SUCCESS", timeout=15, what="ผลการย่อย")
        tap(EXTRACT_SUCCESS_CONFIRM, wait=1.2)
        total += picked
        print(f"⚗️ ย่อยสมบัติ {picked} ชิ้น (รวม {total})")
        # the list starts from the top again: loop until nothing wanted is left


MAX_EXTRACT_PAGES = 30


def select_junk(keep, include_gold, grades):
    """In extract mode (lowest tier first): taps every wanted treasure, scrolling down the list, until the
    first treasure that must stay (S, or A without include_gold) or the end of the list. Each page is checked:
    every tapped slot must show the lime "selected" frame and nothing else may have become selected.
    Returns how many treasures are selected."""
    count = 0
    for _ in range(MAX_EXTRACT_PAGES):
        img = screen()
        slots = grid_slots(img) or SLOTS
        before = {slot for slot in slots if slot_kind(img, slot) == "selected"}  # chosen on an earlier page
        tapped, done = [], False
        for slot in slots:
            kind = slot_kind(img, slot)
            # S (rainbow) treasures are never extracted by the powder and ticket tools; with the list in
            # ascending order they come last, so the first one ends the search
            if kind in ("S", "empty") or (kind == "A" and not include_gold):
                done = True
                break
            if kind not in grades or is_favorite(img, slot) or kept(img, slot, keep):
                continue
            tap(slot, jitter=4, wait=0.6)
            tapped.append(slot)
        if tapped:
            img = screen()
            selected = {slot for slot in slots if slot_kind(img, slot) == "selected"}
            unexpected = selected - before - set(tapped)
            missed = set(tapped) - selected
            if unexpected:
                tap(CLOSE["EXTRACT"])  # leaving extract mode clears the selection
                give_up(f"มีสมบัติถูกเลือกโดยบอทไม่ได้กด ({len(unexpected)} ชิ้น) จึงไม่ย่อย", img)
            count += len(tapped) - len(missed)
            if missed:
                print(f"⚠️ เลือกเพิ่มไม่ได้อีก {len(missed)} ชิ้น (เกมอาจจำกัดจำนวนต่อครั้ง) ย่อยที่เลือกไว้ก่อน")
                break
        if done or not _scroll_list():
            break
    return count


# ---------- draw ----------
class CabinetFull(Exception):
    """The game said "Not enough space!" instead of opening the chest (nothing was used up)."""


ANIMATION_SKIP_SPOT = (640, 200)  # empty space above the opening chest (and above the result popup)
MAX_ANIMATION_SKIPS = 12


def handle_draw_result():
    """After tapping a chest: waits for "Treasure received!" and confirms it. Taps an empty spot above the
    chest meanwhile, which skips its opening animation (about 6 s down to 2 s).
    Raises CabinetFull when the cabinet has no room left."""
    start = time.time()
    skips = 0
    while time.time() - start < 20:
        img = screen()
        if seen(img, "RECEIVED"):
            tap(RESULT_CONFIRM_BUTTON, wait=0.2)
            expect("DRAW", what="หน้าสุ่มสมบัติหลังรับของ")
            return
        if seen(img, "NO_SPACE"):
            tap(NO_SPACE_CONFIRM_BUTTON, wait=0.5)
            expect("DRAW", what="หน้าสุ่มสมบัติหลังปิดแจ้งเตือนคลังเต็ม")
            raise CabinetFull()
        if time.time() - start > 0.25 and skips < MAX_ANIMATION_SKIPS:
            device_tap(DEVICE_IP, DEVICE_PORT, ANIMATION_SKIP_SPOT[0] + random.randint(-20, 20),
                       ANIMATION_SKIP_SPOT[1] + random.randint(-10, 10))
            skips += 1
    give_up("ไม่เจอหน้ารับสมบัติหลังเปิดกล่อง")


def draw_coin_box():
    img = screen()
    if not seen(img, "COIN_BOX"):
        give_up("ไม่เจอปุ่มกล่อง 5,000 เหรียญ", img)
    tap(DRAW_COIN_BOX_BUTTON, jitter=8, wait=0.1)
    handle_draw_result()


def draw_free_ticket():
    """Opens one Free Treasure Ticket. Returns False when no ticket is left (the button is gone)."""
    img = screen()
    if not seen(img, "FREE_TICKET"):
        return False
    tap(DRAW_FREE_TICKET_BUTTON, jitter=6, wait=0.1)
    handle_draw_result()
    return True


# ---------- upgrade ----------
UPGRADE_RESULT_SECONDS = 6   # no result popup this long after Regular: the treasure is at the highest level
# Each treasure is upgraded until it reaches +9; this only catches an endless loop (e.g. coins run out
# and the game keeps answering with the same popup)
MAX_TRIES_PER_TREASURE = 1000
MAX_PICKER_PAGES = 40


def upgrade_selected(max_tries=None):
    """Taps Regular on the treasure shown on the upgrade screen until a tap brings no result popup
    (the highest level, +9). Returns (tries, successes)."""
    tries = successes = 0
    while max_tries is None or tries < max_tries:
        img = screen()
        if not seen(img, "REGULAR"):
            # at +9 the buttons are replaced by "This Treasure is fully upgraded!": this one is done
            if seen(img, "FULLY_UPGRADED") or wait_for(["FULLY_UPGRADED"], 2)[0]:
                return tries, successes
            img = screen()
            if not seen(img, "REGULAR"):
                give_up("ไม่เจอปุ่ม Regular (อัพเกรดด้วยเหรียญ)", img)
        tap(UPGRADE_REGULAR_BUTTON, jitter=8, wait=1.0)
        end = time.time() + UPGRADE_RESULT_SECONDS
        result = None
        while result is None and time.time() < end:
            img = screen()
            if seen(img, "UPGRADE_SUCCESS"):
                result = "success"
                tap(score(img, "POPUP_CLOSE")[1] or (1008, 113))
            elif seen(img, "POPUP_CLOSE"):
                result = "fail"
                tap(score(img, "POPUP_CLOSE")[1])
            elif seen(img, "CONFIRM"):
                result = "fail"
                tap(score(img, "CONFIRM")[1])
            else:
                time.sleep(0.4)
        if result is None:
            if where(screen()) == "UPGRADE":
                return tries, successes  # nothing happened: already +9
            give_up("หลังกดอัพเกรดเจอหน้าจอที่ไม่รู้จัก (เหรียญหมด?)")
        tries += 1
        successes += result == "success"
        print(f"🔨 อัพเกรดครั้งที่ {tries}: {'สำเร็จ' if result == 'success' else 'ไม่สำเร็จ'}")
        expect("UPGRADE", what="หน้าอัพเกรดหลังปิดผล")
        if tries >= MAX_TRIES_PER_TREASURE:
            give_up(f"อัพเกรดชิ้นเดียวกันไป {tries} ครั้งแล้วยังไม่ครบ +9 (เหรียญหมด?)")
    return tries, successes


def _known(skip, fingerprint):
    probe = np.frombuffer(fingerprint, np.uint8).astype(np.int16)
    return any(np.abs(probe - np.frombuffer(f, np.uint8).astype(np.int16)).mean() < 10 for f in skip)


def _scroll_list():
    """Scrolls the open list down by about three rows. Returns False when the list did not move (its end)."""
    before = _normalize_gray(screen())[120:670, 150:680]
    serial = adb._serial(DEVICE_IP, DEVICE_PORT)
    # starts between two columns, so the touch never lands on a treasure; slow, so the list does not fling
    adb._adb_run(["-s", serial, "shell", "input", "swipe", "414", "620", "414", "215", "1500"])
    time.sleep(1.0)
    after = _normalize_gray(screen())[120:670, 150:680]
    return float(np.abs(before.astype(np.int16) - after.astype(np.int16)).mean()) > 3


def pick_upgradable(skip):
    """With the upgrade list open: selects the first treasure below +9, scrolling down as needed.
    Returns True when the upgrade screen shows it, False when no such treasure is left."""
    for _ in range(MAX_PICKER_PAGES):
        img = screen()
        for slot in grid_slots(img):
            kind = slot_kind(img, slot)
            if kind == "empty":
                tap(CLOSE["UPGRADE_PICKER"])
                return False
            fingerprint = slot_id(img, slot)
            if has_plus9(img, slot) or _known(skip, fingerprint):
                continue
            tap(slot, jitter=4, wait=0.8)
            if seen(screen(), "MAX_LEVEL"):
                skip.append(fingerprint)  # +9 without a readable badge
                continue
            tap(PICKER_SELECT_BUTTON, jitter=6, wait=1.2)
            if wait_for(["UPGRADE"], 4)[0]:
                return True
            skip.append(fingerprint)  # this treasure cannot be upgraded
        if not _scroll_list():
            break
    tap(CLOSE["UPGRADE_PICKER"])
    return False


def tool_upgrade(options):
    """Upgrades (Regular, coins) every treasure below +9, one at a time, until each one reaches +9."""
    open_from_main(MAIN_UPGRADE_BUTTON, "UPGRADE", "หน้าอัพเกรดสมบัติ")
    max_tries = options.get("max_tries")  # not in the web UI: for testing a few tries only
    skip, done, tries, successes, stuck = [], 0, 0, 0, 0
    while max_tries is None or tries < max_tries:
        tap(UPGRADE_SELECT_BUTTON, wait=1.2)
        expect("UPGRADE_PICKER", what="รายการสมบัติที่อัพเกรดได้")
        if not pick_upgradable(skip):
            print("✅ ไม่มีสมบัติที่ต่ำกว่า +9 เหลือแล้ว")
            break
        n, ok = upgrade_selected(None if max_tries is None else max_tries - tries)
        tries, successes = tries + n, successes + ok
        stuck = stuck + 1 if n == 0 else 0
        if stuck >= 2:
            give_up("เลือกสมบัติแล้วแต่กดอัพเกรดไม่ได้")
        if n and (max_tries is None or tries < max_tries):
            done += 1
            print(f"⭐ ครบ +9 แล้ว {done} ชิ้น")
    print(f"🔨 อัพเกรดทั้งหมด {tries} ครั้ง สำเร็จ {successes} ครั้ง ครบ +9 {done} ชิ้น")


# ---------- tools ----------


def _draws_per_round(options):
    """Boxes to draw before extracting; None (the option set to 0) = until the cabinet is full."""
    return options.get("draws_before_extract") or None


def tool_powder(options):
    target = options["powder_target"]
    keep, gold = options["keep"], options["extract_gold"]
    warn_missing_keep_templates(keep)
    open_cabinet()
    start, _ = read_counts()
    print(f"🧪 Magic Powder ตอนเริ่ม: {start if start is not None else 'อ่านไม่ได้'} เป้าหมาย +{target:,}")
    gained = boxes = 0
    while gained < target:
        open_from_main(MAIN_DRAW_BUTTON, "DRAW", "หน้าสุ่มสมบัติ")
        per_round = _draws_per_round(options)
        full = False
        try:
            drawn = 0
            while per_round is None or drawn < per_round:
                draw_coin_box()
                drawn += 1
                boxes += 1
                if boxes % 10 == 0:
                    print(f"🎁 สุ่มกล่อง 5,000 ไปแล้ว {boxes} กล่อง")
        except CabinetFull:
            full = True
            print(f"📦 คลังเต็มแล้ว (สุ่มไปแล้ว {boxes} กล่อง) ย่อยก่อนสุ่มต่อ")
        extracted = extract_junk(keep, gold)
        if full and extracted == 0:
            give_up("คลังเต็มแต่ไม่มีสมบัติที่ย่อยได้ (ทุกชิ้นเป็นของที่เก็บไว้/ปักดาว/ระดับสูง) จึงหยุด")
        open_cabinet()
        now, _ = read_counts()
        # a B treasure gives 15 powder, an A one a little more: a reading far from that is a misread
        estimate = gained + extracted * 15
        if start is not None and now is not None and estimate <= now - start <= gained + extracted * 200 + 100:
            gained = now - start
        else:
            gained = estimate
        print(f"🧪 ได้ผงเพิ่ม {gained:,} / {target:,}")
    print(f"✅ ปั๊มผงครบเป้า: สุ่ม {boxes} กล่อง ได้ผงเพิ่ม {gained:,}")


def tool_tickets(options):
    """Opens tickets until the cabinet is full ("Not enough space!"), extracts the junk, and goes on
    until no ticket is left."""
    keep, gold = options["keep"], options["extract_gold"]
    warn_missing_keep_templates(keep)
    opened = 0
    while True:
        open_from_main(MAIN_DRAW_BUTTON, "DRAW", "หน้าสุ่มสมบัติ")
        try:
            while draw_free_ticket():
                opened += 1
                if opened % 10 == 0:
                    print(f"🎫 เปิดตั๋วไปแล้ว {opened} ใบ")
        except CabinetFull:
            print(f"📦 คลังเต็มแล้ว (เปิดไป {opened} ใบ) ย่อยสมบัติแล้วเปิดต่อ")
            if extract_junk(keep, gold) == 0:
                give_up("คลังเต็มแต่ไม่มีสมบัติที่ย่อยได้ (ทุกชิ้นเป็นของที่เก็บไว้/ปักดาว/ระดับสูง) จึงหยุด")
            continue
        break
    print(f"✅ เปิดตั๋วหมดแล้ว รวม {opened} ใบ")
    extract_junk(keep, gold)  # whatever the last tickets gave


def upgrade_named(text):
    """Upgrades every treasure whose name contains `text` to +9. Returns how many tries it took."""
    open_from_main(MAIN_UPGRADE_BUTTON, "UPGRADE", "หน้าอัพเกรดสมบัติ")
    skip, tries = [], 0
    while True:
        tap(UPGRADE_SELECT_BUTTON, wait=1.2)
        expect("UPGRADE_PICKER", what="รายการสมบัติที่อัพเกรดได้")
        search(text)
        found = pick_upgradable(skip)
        if not found:
            # pick_upgradable closed the list; the search text stays for next time and is cleared by search()
            return tries
        n, _ = upgrade_selected()
        tries += n


def farm_for_cookies(keep):
    """Draws 5,000-coin boxes until the cabinet is full, then extracts the white/blue junk (keeping the
    Famous Choco Chip Cookies and the other kept treasures) to make room for the next round."""
    open_from_main(MAIN_DRAW_BUTTON, "DRAW", "หน้าสุ่มสมบัติ")
    drawn = 0
    try:
        while True:
            draw_coin_box()
            drawn += 1
            if drawn % 10 == 0:
                print(f"🎁 สุ่มกล่อง 5,000 ไปแล้ว {drawn} กล่อง")
    except CabinetFull:
        print(f"📦 คลังเต็มแล้ว (สุ่มไป {drawn} กล่อง) ย่อยของขยะ เก็บคุกกี้ไว้อีโว")
    extracted = extract_junk(keep, False)
    if drawn == 0 and extracted == 0:
        give_up("คลังเต็มแต่ไม่มีสมบัติที่ย่อยได้ จึงหยุด (ย่อยหรือขยายคลังก่อน)")


def tool_shard(options):
    """Only ever extracts the Famous Honey Chip Cookie this tool has just evolved: no other treasure,
    and none of the Famous Honey Chip Cookies that were already in the cabinet before it started."""
    target, craft, buy = options["shard_target"], options["craft_missing"], options["shard_buy_boxes"]
    keep = list(dict.fromkeys(["cookie"] + list(options.get("keep") or [])))  # the cookies are never extracted
    open_cabinet()
    _, start = read_counts()
    print(f"💎 Mystic Shard ตอนเริ่ม: {start if start is not None else 'อ่านไม่ได้'} เป้าหมาย +{target}")
    owned = count_named("Famous Honey")
    if owned:
        print(f"🍪 มี Famous Honey Chip Cookie เดิมอยู่แล้ว {owned} ชิ้น บอทจะไม่ย่อยชิ้นเดิม")
    # The evolved cookies are counted on the first page of a name search (16 slots), so at most
    # 16 - owned of them are made before extracting them all at once.
    batch_cap = PAGE_SLOTS - owned
    if batch_cap < 1:
        give_up(f"มี Famous Honey Chip Cookie เดิมอยู่แล้ว {owned} ชิ้น (เต็มหน้า) บอทนับชิ้นที่อีโวใหม่ไม่ได้ ย่อยหรือปักดาวชิ้นเดิมก่อน")
    gained = 0
    out_of_cookies = False
    if buy and not options.get("shard_use_cabinet", True):
        farm_for_cookies(keep)  # asked to farm first: the cabinet's cookies are used together with the new ones
    upgrade_named("Famous Choco")  # every Famous Choco Chip Cookie to +9 first
    while gained < target and not out_of_cookies:
        wanted = min(batch_cap, -(-(target - gained) // SHARDS_PER_COOKIE))
        made = 0
        # 1. evolve every cookie this batch needs, each one again and again until it works
        while made < wanted:
            # the evolve list is searched by the name of what the treasure evolves into
            result = evolve_until_success("Famous Honey", craft)
            if result == NO_TREASURE:
                if not buy:
                    print("⚠️ ไม่มี Famous Choco Chip Cookie ให้อีโวแล้ว")
                    out_of_cookies = True
                    break
                # out of cookies: farm like the powder tool (draw 5,000 boxes until the cabinet is full,
                # extract the junk but keep the cookies), then upgrade and evolve the new cookies
                farm_for_cookies(keep)
                upgrade_named("Famous Choco")
                continue
            if not result:
                out_of_cookies = True
                break
            made += 1
            print(f"🍪 อีโวแล้ว {made} / {wanted} ชิ้น")
        if not made:
            break
        # 2. then extract all of them in one go (never the ones that were there before)
        extracted = extract_named_many("Famous Honey", owned, made)
        if not extracted:
            give_up("อีโวติดแล้วแต่หา Famous Honey Chip Cookie ที่เพิ่งอีโวในหน้าย่อยไม่เจอ")
        open_cabinet()
        _, now = read_counts()
        gained = now - start if start is not None and now is not None else gained + extracted * SHARDS_PER_COOKIE
        print(f"💎 ย่อย {extracted} ชิ้น ได้ Mystic Shard เพิ่ม {gained} / {target}")
    print(f"✅ ปั๊ม Mystic Shard จบ: ได้เพิ่ม {gained}")


def has_named_in_evolve(text):
    """True when the recipe that evolves into `text` can be used: the treasure to evolve is in the cabinet.
    Every discovered recipe is listed even without that treasure, so the recipe itself is opened."""
    open_from_main(MAIN_EVOLVE_BUTTON, "EVOLVE", "หน้าอีโวสมบัติ")
    tap(EVOLVE_RECIPE_BUTTON, wait=1.2)
    expect("EVOLVE_PICKER", what="รายการสูตรอีโว")
    search(text)
    found = slot_kind(screen(), SLOTS[0]) != "empty"
    if found:
        tap(SLOTS[0], jitter=4, wait=1.0)
        found = not seen(screen(), "MISSING_TREASURE")
    tap(PICKER_SEARCH_CLEAR)
    tap(CLOSE["EVOLVE_PICKER"])
    return found


CRAFT_BUTTON = (635, 596)          # "Craft (powder)" in the ingredient window
CRAFT_CONFIRM_BUTTON = (793, 460)  # "Use N units Magic Powder to craft ...?" -> Confirm
CRAFT_DONE_BUTTON = (640, 460)     # "Crafting successful!" -> Confirm


def craft_ingredient(plus):
    """Taps the "+" of a missing ingredient, then Craft, Confirm and the success popup's Confirm."""
    tap(plus, jitter=3, wait=1.0)
    name, img = wait_for(["CRAFT_BUTTON"], 5)
    if name is None:
        tap(INGREDIENT_INFO_CLOSE, wait=1.0)
        give_up("กดปุ่ม + แล้วไม่เจอปุ่ม Craft", img)
    tap(CRAFT_BUTTON, jitter=6, wait=0.8)
    name, img = wait_for(["CRAFT_CONFIRM", "CRAFT_DONE"], 5)
    if name is None:
        tap(INGREDIENT_INFO_CLOSE, wait=1.0)
        give_up("กด Craft แล้วไม่เจอหน้ายืนยัน (ผงไม่พอ?)", img)
    if name == "CRAFT_CONFIRM":
        tap(CRAFT_CONFIRM_BUTTON, jitter=6, wait=1.0)
        name, img = wait_for(["CRAFT_DONE"], 8)
        if name is None:
            give_up("ยืนยัน craft แล้วไม่เจอหน้าสำเร็จ (ผงไม่พอ?)", img)
    tap(CRAFT_DONE_BUTTON, jitter=6, wait=1.0)
    if wait_for(["EVOLVE_PICKER"], 5)[0] is None:
        tap(INGREDIENT_INFO_CLOSE, wait=1.0)  # the ingredient window may still be open


_evolve_search = None  # what this run last typed into the evolve list's search


def _search_bar_filled(img):
    """True when a picker list is filtered by a search (its search bar turns yellow with the text in it)."""
    r, g, b = _rgb(img, 300, 95)
    return r > 230 and g > 210 and b < 170


def evolve_named(text, craft):
    """Evolves the first treasure whose name contains `text`. Returns False when it cannot (missing ingredients)."""
    # already on the evolve screen (after the previous evolution): straight to "Select Recipe"
    if where(screen()) != "EVOLVE":
        open_from_main(MAIN_EVOLVE_BUTTON, "EVOLVE", "หน้าอีโวสมบัติ")
    tap(EVOLVE_RECIPE_BUTTON, wait=1.0)
    _, img = expect("EVOLVE_PICKER", what="รายการสูตรอีโว")
    # the game keeps the last search: skip typing it again only when this run typed this very text
    global _evolve_search
    if not (_evolve_search == text and _search_bar_filled(img)):
        search(text)
        _evolve_search = text
    img = screen()
    if slot_kind(img, SLOTS[0]) == "empty":
        tap(PICKER_SEARCH_CLEAR)
        tap(CLOSE["EVOLVE_PICKER"])
        return False
    tap(SLOTS[0], jitter=4, wait=1.0)
    for _ in range(len(INGREDIENT_SLOTS) + 1):
        img = screen()
        if seen(img, "EVOLVE_SELECT"):
            break
        if seen(img, "MISSING_TREASURE"):
            tap(PICKER_SEARCH_CLEAR)
            tap(CLOSE["EVOLVE_PICKER"])
            return NO_TREASURE
        if not seen(img, "MISSING"):
            give_up("ไม่รู้ว่าสูตรอีโวพร้อมหรือยัง", img)
        if not craft:
            print("⚠️ วัตถุดิบอีโวไม่พอ และไม่ได้เปิดให้ craft ด้วยผง หยุดทำงาน")
            tap(PICKER_SEARCH_CLEAR)
            tap(CLOSE["EVOLVE_PICKER"])
            return False
        # the green "+" on a missing ingredient crafts it with Magic Powder (dim or bright, depending on the screen)
        found, center = score(img, "CRAFT_PLUS")
        if found < THRESHOLD:
            give_up("วัตถุดิบไม่พอแต่หาปุ่ม craft ไม่เจอ", img)
        print("🧪 craft วัตถุดิบที่ขาดด้วย Magic Powder")
        craft_ingredient(center)
    tap(EVOLVE_SELECT_BUTTON, jitter=6, wait=1.5)
    _, img = expect("EVOLVE", what="หน้าอีโวหลังเลือกสูตร")
    if not seen(img, "REGULAR", (700, 365, 920, 450)):
        give_up("ไม่เจอปุ่ม Regular (อีโวด้วยเหรียญ)", img)
    return press_evolve()


def press_evolve():
    """Taps Regular on the evolve screen (recipe already chosen) and reads the result popup."""
    tap(EVOLVE_REGULAR_BUTTON, jitter=6, wait=2.0)
    end = time.time() + 25
    success = False
    while True:
        img = screen()
        # the result shows right here: "Congratulations! Evolution successful!" or a failure popup
        success = success or seen(img, "EVO_SUCCESS")
        if close_popups(img):
            break
        if seen(img, "CONFIRM"):
            tap(score(img, "CONFIRM")[1])
            break
        if time.time() > end:
            give_up("หลังกดอีโวเจอหน้าจอที่ไม่รู้จัก (เหรียญหมด?)", img)
        time.sleep(0.3)
    expect("EVOLVE", what="หน้าอีโวหลังปิดผล")
    return "success" if success else "fail"


def count_named(text):
    """How many treasures whose name contains `text` are in the cabinet (view only, nothing is picked)."""
    open_cabinet()
    tap(CABINET_EXTRACT_BUTTON, wait=1.2)
    expect("EXTRACT", what="หน้าย่อยสมบัติ (Extract)")
    search(text)
    img = screen()
    count = sum(slot_kind(img, slot) != "empty" for slot in (grid_slots(img) or SLOTS))
    tap(PICKER_SEARCH_CLEAR)
    tap(CLOSE["EXTRACT"])
    return count


def evolve_until_success(text, craft):
    """Evolves the treasure that becomes `text` until one evolution succeeds (a failed one keeps the
    treasure, so it is simply tried again). Returns False when it cannot go on (treasure or ingredients missing)."""
    tries = 0
    result = None
    while True:
        if result == "fail":
            # a failed evolution keeps the recipe on the evolve screen: tap Regular again, never "Select Recipe"
            name, img = wait_for(["EVOLVE_READY"], 5)
            if name is None:
                give_up("อีโวไม่ติดแล้วปุ่ม Regular ไม่พร้อมให้กดซ้ำ (เหรียญหมด?)", img)
            result = press_evolve()
        else:
            result = evolve_named(text, craft)
        if not result or result == NO_TREASURE:
            return result
        tries += 1
        if result == "success":  # read from the result popup: no trip to the extract screen
            print(f"✨ อีโวติดแล้ว (ครั้งที่ {tries})")
            return True
        print(f"🎲 อีโวไม่ติด ({tries} ครั้ง) อีโวต่อจนกว่าจะติด")


PAGE_SLOTS = 16
NO_TREASURE = "no_treasure"  # the recipe is there but no treasure to evolve is left in the cabinet


def extract_named_many(text, keep_count, count):
    """Extracts up to `count` treasures whose name contains `text` in one go, always leaving `keep_count`
    of them (the user's own). Favorites and treasures in use are skipped. Returns how many were extracted."""
    open_cabinet()
    tap(CABINET_EXTRACT_BUTTON, wait=1.2)
    expect("EXTRACT", what="หน้าย่อยสมบัติ (Extract)")
    search(text)
    img = screen()
    slots = grid_slots(img) or SLOTS
    listed = [slot for slot in slots if slot_kind(img, slot) != "empty"]
    choices = [slot for slot in listed if slot_kind(img, slot) not in ("other", "selected") and not is_favorite(img, slot)]
    n = min(count, len(listed) - keep_count, len(choices))
    if n <= 0:
        tap(PICKER_SEARCH_CLEAR)
        tap(CLOSE["EXTRACT"])
        return 0
    picked = choices[:n]
    for slot in picked:
        tap(slot, jitter=4, wait=0.5)
    img = screen()
    if sorted(s for s in slots if slot_kind(img, s) == "selected") != sorted(picked):
        tap(CLOSE["EXTRACT"])
        give_up("สมบัติที่ถูกเลือกไม่ตรงกับที่บอทกด จึงไม่ย่อย", img)
    tap(EXTRACT_BUTTON, wait=1.2)
    expect("EXTRACT_CONFIRM", what="หน้ายืนยันการย่อย")
    tap(EXTRACT_CONFIRM_BUTTON, wait=1.5)
    expect("EXTRACT_SUCCESS", timeout=15, what="ผลการย่อย")
    tap(EXTRACT_SUCCESS_CONFIRM, wait=1.2)
    return n


def extract_named(text, keep_count):
    """Extracts ONE treasure whose name contains `text`, only while more than `keep_count` of them are in
    the cabinet (those are the user's own and are left alone). Favorites and treasures in use are skipped."""
    open_cabinet()
    tap(CABINET_EXTRACT_BUTTON, wait=1.2)
    expect("EXTRACT", what="หน้าย่อยสมบัติ (Extract)")
    search(text)
    img = screen()
    slots = grid_slots(img) or SLOTS
    listed = [slot for slot in slots if slot_kind(img, slot) != "empty"]
    choices = [slot for slot in listed if slot_kind(img, slot) not in ("other", "selected") and not is_favorite(img, slot)]
    if len(listed) <= keep_count or not choices:
        tap(PICKER_SEARCH_CLEAR)
        tap(CLOSE["EXTRACT"])
        return False
    slot = choices[0]
    tap(slot, jitter=4, wait=0.8)
    img = screen()
    if [s for s in slots if slot_kind(img, s) == "selected"] != [slot]:
        tap(CLOSE["EXTRACT"])
        give_up("สมบัติที่ถูกเลือกไม่ตรงกับที่บอทกด จึงไม่ย่อย", img)
    tap(EXTRACT_BUTTON, wait=1.2)
    expect("EXTRACT_CONFIRM", what="หน้ายืนยันการย่อย")
    tap(EXTRACT_CONFIRM_BUTTON, wait=1.5)
    expect("EXTRACT_SUCCESS", timeout=15, what="ผลการย่อย")
    tap(EXTRACT_SUCCESS_CONFIRM, wait=1.2)
    return True


LOBBY_FRIENDS_TAB = (248, 187)


def tool_hearts(options):
    """Sends a free life to every friend: scrolls the friend list to the top first, then goes down it."""
    from actions import handle_send_friend_life
    to_lobby()
    tap(LOBBY_FRIENDS_TAB, wait=1.2)  # the Friends tab of the leaderboard
    handle_send_friend_life()
    to_lobby()


TOOLS = {
    "upgrade": ("อัพเกรดสมบัติทุกชิ้นให้ +9", tool_upgrade),
    "powder": ("ปั๊มผง", tool_powder),
    "tickets": ("เปิดตั๋วเหลือง", tool_tickets),
    "shard": ("ปั๊ม Mystic Shard", tool_shard),
    "hearts": ("ส่งหัวใจให้เพื่อน", tool_hearts),
}


def snapshot(prefix):
    """Cabinet before/after the tool, for the "before/after" pictures in the web UI."""
    try:
        open_cabinet()
        img = screen()
        read_counts(img)
        name = save_shot(prefix, img)
        if name:
            emit_stat(prefix, shot=name)
    except ToolStop as e:
        print(f"⚠️ ถ่ายภาพคลังไม่ได้: {e}")


def run(options):
    name, tool = TOOLS[options["tool"]]
    print(f"🧰 เริ่ม: {name}")
    device_connect(DEVICE_IP, DEVICE_PORT)
    try:
        cabinet_tool = options["tool"] != "hearts"  # sending hearts does not touch the cabinet
        if cabinet_tool:
            snapshot("toolbefore")
        tool(options)
        if cabinet_tool:
            snapshot("toolafter")
        to_lobby()
        print(f"✅ {name} เสร็จแล้ว")
    except ToolStop as e:
        print(f"❌ {e}")
        try:
            to_lobby()
        except ToolStop:
            pass
        raise SystemExit(2)


if __name__ == "__main__":
    run(json.loads(os.environ["TOOL_OPTIONS"]))
