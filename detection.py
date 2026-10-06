import os

import cv2
import numpy as np

from config import (
    ANTI_BOT_CARD_HEIGHT,
    ANTI_BOT_CARD_POS_6,
    ANTI_BOT_CARD_WIDTH,
    ANTI_BOT_CARD_POS_1,
    ANTI_BOT_CARD_POS_2,
    ANTI_BOT_CARD_POS_3,
    ANTI_BOT_CARD_POS_4,
    ANTI_BOT_CARD_POS_5,
    ANTI_BOT_CARD_POS_6,
    BOOST_TEMPLATES,
    MATCH_THRESHOLD,
    PAUSE_CONTINUE_PROBE,
    PAUSE_QUIT_PROBE,
    RESULT_COINS_REGION,
    RESULT_XP_REGION,
    RUN_HP_ICON_PROBE,
    STAGE_REGIONS,
    STAGE_TEMPLATES,
    TEMPLATE_DIR,
)


_template_cache: dict = {}
_template_gray_cache: dict = {}


def _get_template(filename):
    """Return cached template image, loading from disk on first access."""
    if filename not in _template_cache:
        path = os.path.join(TEMPLATE_DIR, filename)
        _template_cache[filename] = _normalize(cv2.imread(path, cv2.IMREAD_UNCHANGED))
    return _template_cache[filename]


def _get_template_gray(filename):
    """Return cached grayscale template image, loading from disk on first access."""
    if filename not in _template_gray_cache:
        template = _get_template(filename)
        _template_gray_cache[filename] = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY) if template is not None else None
    return _template_gray_cache[filename]


def load_templates():
    """Pre-warm the template cache with all stage and boost templates at startup."""
    for template_files in STAGE_TEMPLATES.values():
        for filename in template_files:
            _get_template_gray(filename)
    for template_files in BOOST_TEMPLATES:
        for filename in template_files:
            _get_template_gray(filename)


def _normalize(img):
    """Ensure image is BGR uint8 (3-channel). Returns None if conversion fails."""
    if img is None:
        return None
    if img.dtype != np.uint8:
        return None
    if img.ndim == 2:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    if img.ndim == 3 and img.shape[2] == 4:
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    if img.ndim == 3 and img.shape[2] == 3:
        return img
    return None


def _normalize_gray(img):
    normalized = _normalize(img)
    if normalized is None:
        return None
    return cv2.cvtColor(normalized, cv2.COLOR_BGR2GRAY)


def _crop_region(img, region):
    if region is None:
        return img
    x1, y1, x2, y2 = region
    return img[y1:y2, x1:x2]


def detect_templates(screen, template_files, region=None):
    screen_gray = _normalize_gray(screen)
    if screen_gray is None:
        return []
    screen_gray = _crop_region(screen_gray, region)
    offset_x, offset_y = (region[0], region[1]) if region is not None else (0, 0)
    matches = []
    for filename in template_files:
        template = _get_template_gray(filename)
        if template is None:
            continue
        result = cv2.matchTemplate(screen_gray, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val >= MATCH_THRESHOLD:
            th, tw = template.shape[:2]
            x = max_loc[0] + offset_x
            y = max_loc[1] + offset_y
            matches.append((x, y, tw, th))
    return matches


def detect_stage(screen, stage_names=None, exclude=None):
    screen_gray = _normalize_gray(screen)
    if screen_gray is None:
        return None
    if stage_names is None:
        stage_names = STAGE_TEMPLATES.keys()
    if exclude:
        stage_names = [s for s in stage_names if s not in exclude]
    for stage_name in stage_names:
        template_files = STAGE_TEMPLATES.get(stage_name)
        if not template_files:
            continue
        search_area = _crop_region(screen_gray, STAGE_REGIONS.get(stage_name))
        for filename in template_files:
            template = _get_template_gray(filename)
            if template is None:
                continue
            if (
                search_area.shape[0] < template.shape[0]
                or search_area.shape[1] < template.shape[1]
            ):
                continue
            result = cv2.matchTemplate(search_area, template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)
            if max_val >= MATCH_THRESHOLD:
                return stage_name
    return None


def _pixel_rgb(screen, point):
    """Return the (R, G, B) colour at a 1280x720 point, or None if the screen is not usable."""
    if screen is None or screen.shape[0] != 720 or screen.shape[1] != 1280:
        return None
    x, y = point
    b, g, r = (int(v) for v in screen[y, x][:3])
    return r, g, b


def _probe_matches(screen, probe, tolerance=45):
    point, target = probe
    color = _pixel_rgb(screen, point)
    if color is None:
        return False
    return all(abs(c - t) <= tolerance for c, t in zip(color, target))


def detect_running(screen):
    """True while a run is in progress (the HP heart icon is on screen)."""
    return _probe_matches(screen, RUN_HP_ICON_PROBE)


def detect_pause_menu(screen):
    return _probe_matches(screen, PAUSE_CONTINUE_PROBE) and _probe_matches(screen, PAUSE_QUIT_PROBE)


def detect_boost_cell_selected(screen, probe_point):
    """Selected boost cells on the prep screen turn yellow. Returns None if the screen is not usable."""
    color = _pixel_rgb(screen, probe_point)
    if color is None:
        return None
    r, g, b = color
    return r > 225 and g > 225 and b < 90


# ----- Mystery Box type -----
# templates/MYSTERY_BOX_TYPE_<n>.png: 1 copper, 2 silver, 3 gold, 4 rainbow (transparent background)
MYSTERY_BOX_TYPES = 4
MYSTERY_BOX_REGION = (550, 315, 730, 465)          # the closed box in the middle of the screen
MYSTERY_BOX_OPEN_ALL_REGION = (500, 600, 780, 690)  # "Open all" button (the reward screen shows "Confirm")
_box_histograms = None


def _hs_histogram(img, mask=None):
    hist = cv2.calcHist([cv2.cvtColor(img, cv2.COLOR_BGR2HSV)], [0, 1], mask, [30, 16], [0, 180, 0, 256])
    cv2.normalize(hist, hist)
    return hist


def _load_box_histograms():
    global _box_histograms
    if _box_histograms is None:
        _box_histograms = {}
        for n in range(1, MYSTERY_BOX_TYPES + 1):
            image = cv2.imread(os.path.join(TEMPLATE_DIR, f"MYSTERY_BOX_TYPE_{n}.png"), cv2.IMREAD_UNCHANGED)
            if image is None or image.ndim != 3:
                continue
            mask = (image[:, :, 3] > 200).astype(np.uint8) if image.shape[2] == 4 else None
            _box_histograms[n] = _hs_histogram(image[:, :, :3], mask)
    return _box_histograms


def is_mystery_box_closed(screen):
    """True on the screen with the closed box and "Open all", False on the reward screen after it."""
    return bool(detect_templates(screen, ["MYSTERY_BOX_OPEN_ALL_1.png"], MYSTERY_BOX_OPEN_ALL_REGION))


BOXES_AREA = (80, 110, 1200, 600)   # where the closed boxes can be: one in the middle, a row of 3, two rows...
BOX_WINDOW = (150, 130)              # about the size of one box
BOX_STEP = 16
BOX_MIN_SCORE = 0.75


def classify_mystery_boxes(screen):
    """Types of every closed box on the screen (1 copper, 2 silver, 3 gold, 4 rainbow), left to right and
    top to bottom. A window the size of one box slides over the screen; where its colours match a box type
    well, a box is counted, and nearby matches of the same box are dropped."""
    if screen is None or screen.shape[0] != 720 or screen.shape[1] != 1280:
        return []
    refs = _load_box_histograms()
    hsv = cv2.cvtColor(_normalize(screen), cv2.COLOR_BGR2HSV)
    x1, y1, x2, y2 = BOXES_AREA
    w, h = BOX_WINDOW
    hits = []
    for y in range(y1, y2 - h, BOX_STEP):
        for x in range(x1, x2 - w, BOX_STEP):
            hist = cv2.calcHist([hsv[y:y + h, x:x + w]], [0, 1], None, [30, 16], [0, 180, 0, 256])
            cv2.normalize(hist, hist)
            scores = {n: cv2.compareHist(hist, ref, cv2.HISTCMP_CORREL) for n, ref in refs.items()}
            best = max(scores, key=scores.get)
            if scores[best] >= BOX_MIN_SCORE:
                hits.append((scores[best], best, x + w // 2, y + h // 2))
    boxes = []
    for score, kind, cx, cy in sorted(hits, reverse=True):
        if all(abs(cx - bx) > 120 or abs(cy - by) > 100 for _, bx, by in boxes):
            boxes.append((kind, cx, cy))
    boxes.sort(key=lambda b: (b[2] // 100, b[1]))
    return [kind for kind, _, _ in boxes]


def classify_mystery_box(screen):
    """1 copper, 2 silver, 3 gold, 4 rainbow, or None when the box does not clearly look like one of them.
    Boxes are told apart by their colours."""
    if screen is None or screen.shape[0] != 720 or screen.shape[1] != 1280:
        return None
    x1, y1, x2, y2 = MYSTERY_BOX_REGION
    hist = _hs_histogram(_normalize(screen)[y1:y2, x1:x2])
    scores = {n: cv2.compareHist(hist, ref, cv2.HISTCMP_CORREL) for n, ref in _load_box_histograms().items()}
    if not scores:
        return None
    best = max(scores, key=scores.get)
    return best if scores[best] >= 0.6 else None


# ----- reading the Coins / XP numbers on the Result screen -----
DIGIT_HEIGHT = 24
DIGIT_WIDTH = 26
DIGIT_MATCH_THRESHOLD = 0.85
_digit_templates = None


def _load_digit_templates():
    """Digit shapes (templates/DIGIT_0.png ... DIGIT_9.png), loaded once. Missing digits are skipped."""
    global _digit_templates
    if _digit_templates is None:
        _digit_templates = {}
        for digit in "0123456789":
            image = cv2.imread(os.path.join(TEMPLATE_DIR, f"DIGIT_{digit}.png"), cv2.IMREAD_GRAYSCALE)
            if image is not None and image.shape == (DIGIT_HEIGHT, DIGIT_WIDTH):
                _digit_templates[digit] = image.astype(np.float32)
    return _digit_templates


def _find_glyphs(screen, region):
    """Dark characters inside a region, left to right, each cropped to its own bounding box (0/1 image)."""
    gray = _normalize_gray(screen)
    if gray is None or gray.shape[0] != 720 or gray.shape[1] != 1280:
        return []
    mask = (_crop_region(gray, region) < 140).astype(np.uint8)
    columns = list(mask.sum(axis=0) > 0) + [False]
    glyphs, start = [], None
    for x, filled in enumerate(columns):
        if filled and start is None:
            start = x
        elif not filled and start is not None:
            glyph = _trim_glyph(mask[:, start:x])
            if glyph.shape[0] >= 3 and glyph.sum() >= 6:   # ignore specks
                glyphs.extend(_split_touching(glyph))
            start = None
    return glyphs


def _trim_glyph(glyph):
    rows = np.where(glyph.sum(axis=1) > 0)[0]
    columns = np.where(glyph.sum(axis=0) > 0)[0]
    if len(rows) == 0:
        return glyph[:0]
    return glyph[rows[0]:rows[-1] + 1, columns[0]:columns[-1] + 1]


def _split_touching(glyph):
    """Digits such as "33" can touch; a shape much wider than a digit is cut at its thinnest column."""
    height, width = glyph.shape
    if width <= height * 1.2:
        return [glyph]
    ink = glyph.sum(axis=0)
    low, high = int(width * 0.3), int(width * 0.7)
    cut = low + int(np.argmin(ink[low:high + 1]))
    if not 0 < cut < width:
        return [glyph]  # a thin line only a few pixels wide: nothing to cut
    parts = []
    for part in (_trim_glyph(glyph[:, :cut]), _trim_glyph(glyph[:, cut:])):
        if part.size:
            parts.extend(_split_touching(part))
    return parts


def _normalize_glyph(glyph):
    """Scales a character to DIGIT_HEIGHT (keeping its proportions) and centres it on a fixed-size canvas."""
    height, width = glyph.shape
    new_width = max(1, min(DIGIT_WIDTH, round(width * DIGIT_HEIGHT / height)))
    scaled = cv2.resize(glyph * 255, (new_width, DIGIT_HEIGHT), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((DIGIT_HEIGHT, DIGIT_WIDTH), np.uint8)
    left = (DIGIT_WIDTH - new_width) // 2
    canvas[:, left:left + new_width] = scaled
    return canvas


def read_number(screen, region):
    """Reads a number such as "10,131" from a region of the Result screen. Returns None if it is not sure."""
    templates = _load_digit_templates()
    glyphs = _find_glyphs(screen, region)
    if not templates or not glyphs:
        return None
    full_height = max(glyph.shape[0] for glyph in glyphs)
    digits = ""
    for glyph in glyphs:
        if glyph.shape[0] < full_height * 0.6:
            continue  # thousands separator
        shape = _normalize_glyph(glyph).astype(np.float32)
        scores = {digit: 1 - float(np.abs(shape - template).mean()) / 255 for digit, template in templates.items()}
        best = max(scores, key=scores.get)
        if scores[best] < DIGIT_MATCH_THRESHOLD:
            return None
        digits += best
    return int(digits) if digits and len(digits) <= 9 else None


HEARTS_REGION = (975, 14, 1036, 52)  # the "+ 601" next to the five heart icons at the top of the lobby
_lobby_digits = None


def read_hearts(screen):
    """The extra lives shown as "+ N" at the top of the lobby (the five heart icons not included).
    Uses the lobby font's digit shapes (templates/LOBBY_DIGIT_<n>.png). None when it is not sure."""
    global _lobby_digits
    if _lobby_digits is None:
        # LOBBY_DIGIT_<digit>_<small|big>.png: shapes from the heart counter itself where known, otherwise
        # from the (bigger) friend scores in the same font
        import glob
        _lobby_digits = []
        for path in glob.glob(os.path.join(TEMPLATE_DIR, "LOBBY_DIGIT_*.png")):
            image = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
            if image is not None and image.shape == (DIGIT_HEIGHT, DIGIT_WIDTH):
                _lobby_digits.append((os.path.basename(path)[12], image.astype(np.float32)))
    gray = _normalize_gray(screen)
    if gray is None or gray.shape != (720, 1280) or len({d for d, _ in _lobby_digits}) < 10:
        return None
    x1, y1, x2, y2 = HEARTS_REGION
    mask = (gray[y1:y2, x1:x2] < 140).astype(np.uint8)
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    blobs = [stats[i] for i in range(1, count) if 14 <= stats[i][3] <= 26 and stats[i][4] >= 20]
    if not blobs:
        return None
    full = max(b[3] for b in blobs)
    text = ""
    for x, y, w, h, _ in sorted(blobs, key=lambda b: b[0]):
        if h < full * 0.75:
            continue  # the "+" sign
        for glyph in _split_touching(mask[y:y + h, x:x + w]):
            shape = _normalize_glyph(_trim_glyph(glyph)).astype(np.float32)
            per_digit = {}
            for d, t in _lobby_digits:
                per_digit[d] = max(per_digit.get(d, 0), 1 - float(np.abs(shape - t).mean()) / 255)
            ranked = sorted(per_digit.items(), key=lambda kv: kv[1], reverse=True)
            # not sure (weak match, or two digits match almost as well): no number rather than a wrong one
            if ranked[0][1] < 0.8 or ranked[0][1] - ranked[1][1] < 0.02:
                return None
            text += ranked[0][0]
    return int(text) if text and len(text) <= 5 else None


def read_result_numbers(screen):
    """Returns (coins, xp) shown on the Result screen; either can be None when it could not be read."""
    return read_number(screen, RESULT_COINS_REGION), read_number(screen, RESULT_XP_REGION)


def detect_anti_bot_odd_cards(screen):
    """
    Return 0-based indices of the 2 cards that differ from the majority 4.

    Strategy:
      1. Crop each card region.
      2. Build a pairwise HSV-histogram similarity matrix (6x6).
      3. For each card, compute its average similarity to all others.
      4. The 2 cards with the lowest average similarity are the odd ones.
    """

    # Define card coordinates based on config constants
    card_coords = [
        ANTI_BOT_CARD_POS_1,
        ANTI_BOT_CARD_POS_2,
        ANTI_BOT_CARD_POS_3,
        ANTI_BOT_CARD_POS_4,
        ANTI_BOT_CARD_POS_5,
        ANTI_BOT_CARD_POS_6,
    ]

    # Crop card regions as grayscale for structural comparison
    screen_bgr = _normalize(screen)
    crops = []
    for cx, cy in card_coords:
        crop = screen_bgr[cy:cy + ANTI_BOT_CARD_HEIGHT, cx:cx + ANTI_BOT_CARD_WIDTH]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        crops.append(gray)

    # Pairwise structural similarity via normalized cross-correlation
    n = len(crops)
    sim = np.zeros((n, n), dtype=np.float32)
    for i in range(n):
        for j in range(n):
            if i != j:
                result = cv2.matchTemplate(crops[i], crops[j], cv2.TM_CCOEFF_NORMED)
                sim[i][j] = result[0][0]

    # Average similarity of each card against all others (excluding self)
    avg_sim = sim.sum(axis=1) / (n - 1)
    print("🔍 Analyzing card similarity...")
    for idx, s in enumerate(avg_sim):
        print(f"  Card {idx + 1}: similarity score {s:.2f}")

    # The 2 cards with the lowest average similarity are the odd ones
    odd_indices = list(np.argsort(avg_sim)[:2])
    return odd_indices
