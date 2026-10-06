import json
import os
import random
import time

import cv2

import adb
from adb import device_capture_screen, device_connect, device_reset_app, device_tap
from actions import (
    exit_current_run,
    handle_connection_lost,
    jump,
    set_boost_cells,
)
from actions import (
    accept_congratulations,
    accept_daily_checkin,
    accept_daily_checkin_boost_set,
    accept_daily_new,
    accept_daily_treasure,
    accept_enter_league,
    accept_league_results,
    accept_level_up,
    accept_mystery_box,
    accept_overtake_break_score,
    accept_previous_rank_results,
    accept_relic_claim,
    accept_too_many_treasures,
    close_announcement_dialog,
    complete_finish,
    handle_anti_bot,
    handle_inactive,
    handle_quick_receive_and_send_lives,
    handle_send_friend_life,
    open_relic_complete,
    play_game,
    purchase_cookie_relay,
    purchase_desired_random_boost,
    purchase_fast_start,
    start_game,
    using_cookie_relay,
    using_fast_start,
)
from config import (
    BOOST_17P_BASE_SPEED_TEMPLATE,
    BOOST_15P_SCORE_BONUS_TEMPLATE,
    BOOST_20P_HP_FROM_POTIONS_TEMPLATE,
    BOOST_2PIT_LIFTS_TEMPLATE,
    BOOST_70P_CRUSH_CHANCE_TEMPLATE,
    BOOST_DOUBLE_COINS_TEMPLATE,
    BOOST_GOLD_COIN_MAGIC_TEMPLATE,
    BOOST_M15P_HP_DRAIN_TEMPLATE,
    BOOST_M30P_COLLISION_DAMAGE_TEMPLATE,
    BOOST_MAGNETIC_AURA_TEMPLATE,
    BOOST_REVIVE_ONCE_WITH_80HP_TEMPLATE,
    DETECTION_ALWAYS_STAGES,
    DETECTION_GROUPS,
    DETECTION_RECOVERY_SCAN_INTERVAL,
    DEVICE_IP,
    DEVICE_PORT,
    SESSION_RESET_INTERVAL,
)
from detection import (classify_mystery_boxes, detect_running, detect_stage, is_mystery_box_closed, load_templates,
                       read_hearts, read_result_numbers)

BOX_TYPE_NAMES = {1: "copper", 2: "silver", 3: "gold", 4: "rainbow"}
from debug import save_debug_screen

# Created by the web UI to ask a running bot to send friend lives at the next main menu
HEARTS_NOW_FILE = os.environ.get("BOT_HEARTS_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".send_hearts_now")

# Option key -> boost cell name on the prep screen (see BOOST_CELLS in config.py)
BOOST_CELL_OPTIONS = {
    "buy_hp_extension": "HP Extension",
    "buy_power_jelly": "Power Jelly Boost",
    "buy_double_xp": "Double XP",
}

# Dialogs that are skipped when the "close popups" option is turned off
POPUP_STAGES = {
    "MYSTERY_BOX",
    "CONGRATULATIONS",
    "LEVEL_UP",
    "DAILY_CHECKIN",
    "DAILY_CHECKIN_BOOST_SET",
    "DAILY_TREASURE",
    "DAILY_NEW",
    "ENTER_LEAGUE",
    "LEAGUE_RESULTS",
    "PREVIOUS_RANK_RESULTS",
    "TOO_MANY_TREASURES",
    "OVERTAKE_BREAK_SCORE",
}


# Lines starting with this marker carry statistics for the web UI instead of text for the log
STAT_MARKER = "@@STAT "
# Result screen of each finished round (relative to the working directory).
# With several emulators the web UI gives every bot process its own sub-folder.
RESULTS_DIR = os.environ.get("BOT_RESULTS_DIR") or "results"
MAX_RESULT_SHOTS = 50
RESULT_SETTLE_SECONDS = 2.5      # the Result numbers count as final once unchanged this long
RESULT_SETTLE_MAX_SECONDS = 25   # never wait longer than this on the Result screen


def emit_stat(event, **data):
    print(STAT_MARKER + json.dumps({"event": event, **data}))


def save_shot(prefix, screen):
    """Saves a screen as results/<prefix>-<time>.jpg, keeping the newest MAX_RESULT_SHOTS. Returns the file name."""
    try:
        if screen is None:
            return None
        os.makedirs(RESULTS_DIR, exist_ok=True)
        name = time.strftime(f"{prefix}-%Y%m%d-%H%M%S.jpg")
        cv2.imwrite(os.path.join(RESULTS_DIR, name), screen, [cv2.IMWRITE_JPEG_QUALITY, 80])
        shots = sorted(f for f in os.listdir(RESULTS_DIR) if f.startswith(prefix + "-") and f.endswith(".jpg"))
        for old in shots[:-MAX_RESULT_SHOTS]:
            os.remove(os.path.join(RESULTS_DIR, old))
        return name
    except Exception as e:
        print(f"⚠️ Could not save the {prefix} screen: {e}")
        return None


def save_result_screen():
    """Saves the result screen of the round that just ended and reads its numbers.
    Returns (file name, coins, xp); each one is None when it could not be obtained."""
    # The game first shows the raw numbers, then adds each treasure/boost bonus one after another.
    # Read until both numbers have stopped changing, so the totals include every bonus.
    try:
        time.sleep(1.5)
        screen = device_capture_screen(DEVICE_IP, DEVICE_PORT)
        coins, xp = read_result_numbers(screen)
        stable_since = time.time()
        deadline = time.time() + RESULT_SETTLE_MAX_SECONDS
        while time.time() < deadline and time.time() - stable_since < RESULT_SETTLE_SECONDS:
            time.sleep(0.4)
            new_screen = device_capture_screen(DEVICE_IP, DEVICE_PORT)
            if detect_stage(new_screen, ["GAME_COMPLETE"]) != "GAME_COMPLETE":
                break  # the Result screen is gone
            numbers = read_result_numbers(new_screen)
            if numbers != (coins, xp) or None in numbers:
                stable_since = time.time()
            screen, (coins, xp) = new_screen, numbers
        if coins is not None or xp is not None:
            print(f"💰 Round result: {coins if coins is not None else '?'} coins, {xp if xp is not None else '?'} XP")
        return save_shot("round", screen), coins, xp
    except Exception as e:
        print(f"⚠️ Could not save the result screen: {e}")
        return None, None, None


def next_lives_interval(options):
    minutes = options.get("mail_minutes")
    if minutes is not None:
        return float(minutes) * 60  # 0: open the mailbox at every main menu
    return random.uniform(25 * 60, 35 * 60)


APP_CHECK_SECONDS = 20       # how often the bot checks that the game is still running
STUCK_SECONDS = 8 * 60       # no known screen and no run in progress this long: the game is reopened


def reopen_game():
    """Reconnects to the emulator and (re)starts the game, then clears the start-up announcements.
    Returns False when it could not, so the caller tries again later instead of stopping the bot."""
    try:
        device_connect(DEVICE_IP, DEVICE_PORT)
        device_reset_app(DEVICE_IP, DEVICE_PORT)  # force-stop + launch, waits until the game is stable
        time.sleep(5)
        close_announcement_dialog()
        print("✅ เปิดเกมใหม่แล้ว ทำงานต่อ")
        return True
    except Exception as e:
        print(f"⚠️ เปิดเกมใหม่ไม่สำเร็จ: {e} (จะลองใหม่)")
        return False


def stop_reason(stop_rounds, rounds, stop_minutes, run_start_time, stop_coins, coins_earned, stop_hearts, screen):
    """Why the bot should stop now at the lobby (a message), or None to go on playing."""
    if stop_rounds is not None and rounds >= stop_rounds:
        return f"ครบ {rounds} รอบแล้ว"
    if stop_minutes is not None and time.time() - run_start_time >= stop_minutes * 60:
        return f"ทำงานครบ {stop_minutes} นาทีแล้ว"
    if stop_coins is not None and coins_earned >= stop_coins:
        return f"ได้เงิน {coins_earned:,} แล้ว (เป้า {stop_coins:,})"
    if stop_hearts is not None:
        # read twice from fresh screens: a misread must not stop the bot
        readings = [read_hearts(screen)]
        time.sleep(1)
        readings.append(read_hearts(device_capture_screen(DEVICE_IP, DEVICE_PORT)))
        if readings[0] is not None and readings[0] == readings[1]:
            print(f"❤️ หัวใจสำรองเหลือ +{readings[0]}")
            if readings[0] < stop_hearts:
                return f"หัวใจสำรองเหลือ +{readings[0]} (ต่ำกว่า {stop_hearts})"
    return None


def consume_hearts_now_request():
    if not os.path.exists(HEARTS_NOW_FILE):
        return False
    try:
        os.remove(HEARTS_NOW_FILE)
    except OSError:
        pass
    return True

# -------------------
# BOT OPTIONS
# -------------------
BOOST_CHOICES = [
    ("Double Coins",            BOOST_DOUBLE_COINS_TEMPLATE),
    ("+15% Score Bonus",        BOOST_15P_SCORE_BONUS_TEMPLATE),
    ("-15% HP Drain",           BOOST_M15P_HP_DRAIN_TEMPLATE),
    ("Revive Once with 80 HP",  BOOST_REVIVE_ONCE_WITH_80HP_TEMPLATE),
    ("70% Crush Chance",        BOOST_70P_CRUSH_CHANCE_TEMPLATE),
    ("+17% Base Speed",         BOOST_17P_BASE_SPEED_TEMPLATE),
    ("Gold Coin Magic",         BOOST_GOLD_COIN_MAGIC_TEMPLATE),
    ("-30% Collision Damage",   BOOST_M30P_COLLISION_DAMAGE_TEMPLATE),
    ("+20% HP from Potions",    BOOST_20P_HP_FROM_POTIONS_TEMPLATE),
    ("Magnetic Aura",           BOOST_MAGNETIC_AURA_TEMPLATE),
    ("2 Pit Lifts",             BOOST_2PIT_LIFTS_TEMPLATE),
]


def get_detection_stage_names(group_name, exclude=None):
    stage_names = []
    # For non-in-game groups, always stages have higher priority
    if group_name != "IN_GAME":
        for stage_name in DETECTION_ALWAYS_STAGES:
            if stage_name not in stage_names:
                stage_names.append(stage_name)
    # Add stages from the specified detection group
    for stage_name in DETECTION_GROUPS[group_name]:
        if stage_name not in stage_names:
            stage_names.append(stage_name)
    # For in-game, always stages are appended last (original behavior)
    if group_name == "IN_GAME":
        for stage_name in DETECTION_ALWAYS_STAGES:
            if stage_name not in stage_names:
                stage_names.append(stage_name)
    if exclude:
        stage_names = [s for s in stage_names if s not in exclude]
    return stage_names


def prompt_user_options():
    desired_boost_template = None

    print("⚙️ --- Bot Options ---")
    use_fast_start = input("⚡ Use Fast Start (buy + use)? [y/n]: ").strip().lower() == "y"
    use_cookie_relay = input("🍪 Use Cookie Relay (buy + use)? [y/n]: ").strip().lower() == "y"
    use_desired_random_boost = input("🎲 Use Desired Random Boost (buy + use)? [y/n]: ").strip().lower() == "y"
    if use_desired_random_boost:
        print("  Select desired boost (must match the boost option configured in-game):")
        for i, (name, _) in enumerate(BOOST_CHOICES, 1):
            print(f"  {i:2}. {name}")
        while True:
            choice = input("  Enter number: ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(BOOST_CHOICES):
                desired_boost_template = BOOST_CHOICES[int(choice) - 1][1]
                desired_boost_name = BOOST_CHOICES[int(choice) - 1][0]
                print(f"  ✅ Selected: {desired_boost_name}")
                break
            print(f"  ⚠️ Please enter a number between 1 and {len(BOOST_CHOICES)}.")
    detect_relic = input("🏺 Detect Relic (open + claim)? [y/n]: ").strip().lower() == "y"
    print("---------------------")

    return {
        "use_fast_start": use_fast_start,
        "use_cookie_relay": use_cookie_relay,
        "use_desired_random_boost": use_desired_random_boost,
        "desired_boost_template": desired_boost_template,
        "desired_boost_name": desired_boost_name if use_desired_random_boost else None,
        "detect_relic": detect_relic,
    }


# -------------------
# MAIN LOOP
# -------------------
def main():
    try:
        print("🚀 CookieRun Classic Bot Started")
        print("⚠️ Screen must be 1280x720 resolution for the bot to work properly.")
        print(f"📱 Connecting to device at {adb.DEVICE_SERIAL or f'{DEVICE_IP}:{DEVICE_PORT}'}...")

        device_connect(DEVICE_IP, DEVICE_PORT)
        load_templates()

        # * for debugging *
        # device_screen = device_capture_screen(DEVICE_IP, DEVICE_PORT)
        # save_debug_screen(device_screen)

        options = prompt_user_options()
        relic_exclude = set() if options["detect_relic"] else {"RELIC_COMPLETE", "RELIC_CLAIM"}

        # Extra options (set by the web UI). Missing keys keep the original behaviour.
        dry_run = bool(options.get("dry_run", False))
        buy_fast_start = options.get("buy_fast_start", options["use_fast_start"])
        buy_cookie_relay = options.get("buy_cookie_relay", options["use_cookie_relay"])
        boost_cells = {name: bool(options[key]) for key, name in BOOST_CELL_OPTIONS.items() if key in options}
        auto_jump = bool(options.get("auto_jump", False))
        stop_jump_when_done = bool(options.get("stop_jump_when_done", False))
        jump_limit = int(options.get("jump_limit", 30))
        exit_after_relay = bool(options.get("exit_after_relay", False))
        send_hearts = options.get("send_hearts")  # None: only after an app reset (original behaviour)
        heart_hours = float(options.get("heart_hours", 1))
        mail_hearts = bool(options.get("mail_hearts", True))
        close_popups = bool(options.get("close_popups", True))
        restart_on_lost = bool(options.get("restart_on_lost", True))
        # Seconds to wait at the main menu before starting the next game
        next_game_delay_min = float(options.get("next_game_delay_min", 30))
        next_game_delay_max = max(next_game_delay_min, float(options.get("next_game_delay_max", 60)))
        report_stats = bool(options.get("report_stats", False))  # the web UI collects round statistics
        result_delay = float(options.get("result_delay", 0))  # seconds on the Result screen before pressing OK
        round_start_time = None
        # Stop by itself at the lobby once one of these is reached (web UI options; all off by default)
        stop_rounds = int(options["stop_rounds_n"]) if options.get("stop_rounds") else None
        stop_minutes = int(options["stop_minutes_n"]) if options.get("stop_minutes") else None
        stop_hearts = int(options["stop_hearts_n"]) if options.get("stop_hearts") else None
        stop_coins = int(options["stop_coins_n"]) if options.get("stop_coins") else None
        run_start_time = time.time()
        coins_earned = 0

        if not close_popups:
            relic_exclude |= POPUP_STAGES
        relic_exclude = relic_exclude or None
        adb.DRY_RUN = dry_run
        if dry_run:
            print("🧪 Dry run: the bot only logs what it would do, no taps are sent.")
        consume_hearts_now_request()  # drop a stale request from a previous run

        last_stage = None
        is_first_game = True
        detection_group = "PRE_GAME"
        last_detected_time = time.time()
        session_start_time = time.time()
        session_reset_interval = random.uniform(*SESSION_RESET_INTERVAL)
        lives_interval = next_lives_interval(options)
        last_lives_time = time.time() - lives_interval  # first mailbox run happens at the first main menu
        pending_send_friend_life = False
        # first friend lives run happens at the first main menu, unless the hearts tool has just sent them
        next_hearts_time = time.time() + heart_hours * 3600 if options.get("hearts_just_sent") else 0
        rounds = 0
        jumps = 0
        jump_limit_logged = False
        dry_seen_stage = None

        last_app_check = time.time()
        last_progress = time.time()  # last time a known screen or a run in progress was seen
        while True:
            # Game crashed / closed, emulator connection dropped, or stuck on an unknown screen: open the game again
            reason = None
            try:
                device_screen = device_capture_screen(DEVICE_IP, DEVICE_PORT)
            except Exception as e:
                device_screen = None
                reason = f"จับภาพหน้าจอไม่ได้ ({e})"
            if reason is None and time.time() - last_app_check >= APP_CHECK_SECONDS:
                last_app_check = time.time()
                try:
                    if not adb.device_is_app_running(DEVICE_IP, DEVICE_PORT, adb.GAME_PACKAGE):
                        reason = "เกมปิดไป/เด้งออก"
                except Exception as e:
                    reason = f"ติดต่ออีมูเลเตอร์ไม่ได้ ({e})"
            if reason is None and time.time() - last_progress >= STUCK_SECONDS:
                reason = f"ค้างที่หน้าจอที่ไม่รู้จักนานเกิน {STUCK_SECONDS // 60} นาที"
            if reason and not dry_run:
                print(f"🚑 {reason} — กำลังเปิดเกมใหม่...")
                if not reopen_game():
                    time.sleep(30)  # try again on the next pass
                    last_progress = time.time()
                    continue
                pending_send_friend_life = send_hearts is not False
                session_start_time = time.time()
                session_reset_interval = random.uniform(*SESSION_RESET_INTERVAL)
                last_lives_time = time.time()
                lives_interval = next_lives_interval(options)
                detection_group = "PRE_GAME"
                last_stage = None
                is_first_game = True
                round_start_time = None
                last_progress = last_app_check = time.time()
                continue
            if device_screen is None:
                time.sleep(2)
                continue
            stage = detect_stage(device_screen, get_detection_stage_names(detection_group, exclude=relic_exclude))
            if stage is not None or detect_running(device_screen):
                last_progress = time.time()
            if stage is None:
                if time.time() - last_detected_time >= DETECTION_RECOVERY_SCAN_INTERVAL[detection_group]:
                    stage = detect_stage(device_screen, exclude=relic_exclude)
                    last_detected_time = time.time()
            else:
                last_detected_time = time.time()

            # Auto jump while a run is in progress and no prompt/dialog is on screen
            if stage is None and auto_jump and detection_group == "IN_GAME" and detect_running(device_screen):
                if stop_jump_when_done and jumps >= jump_limit:
                    if not jump_limit_logged:
                        print(f"🦘 Jumped {jumps} times — stopped jumping for this run.")
                        jump_limit_logged = True
                    time.sleep(0.1)
                else:
                    if jumps == 0:
                        print("🦘 Run in progress: Jump + Double Jump...")
                    jump()
                    jumps += 2
                last_stage = None
                continue

            # In a dry run nothing is tapped, so the same screen stays up: handle each screen only once
            if dry_run:
                if stage == dry_seen_stage:
                    time.sleep(0.5)
                    continue
                dry_seen_stage = stage

            if stage == last_stage:
                time.sleep(0.1)
                continue

            last_stage = stage

            if stage == "MAINMENU":
                print("🎮 Detected Stage: MAINMENU")
                # Wait screen refresh
                print("⏳ Waiting 5 seconds for screen refresh...")
                time.sleep(5)
                if pending_send_friend_life:
                    print("💌 Sending friend lives after app reset...")
                    handle_send_friend_life()
                    pending_send_friend_life = False
                    next_hearts_time = time.time() + heart_hours * 3600
                    last_lives_time = time.time()
                    last_stage = None
                    continue
                elapsed = time.time() - session_start_time
                if elapsed >= session_reset_interval and not dry_run:
                    print(f"🔄 Session reset triggered after {elapsed / 3600:.2f}h — restarting app...")
                    device_reset_app(DEVICE_IP, DEVICE_PORT)
                    time.sleep(5)
                    close_announcement_dialog()
                    pending_send_friend_life = send_hearts is not False
                    session_start_time = time.time()
                    session_reset_interval = random.uniform(*SESSION_RESET_INTERVAL)
                    last_lives_time = time.time()
                    lives_interval = next_lives_interval(options)
                    detection_group = "PRE_GAME"
                    last_stage = None
                    is_first_game = True
                    continue
                lives_elapsed = time.time() - last_lives_time
                if mail_hearts and lives_elapsed >= lives_interval:
                    last_lives_time = time.time()
                    lives_interval = next_lives_interval(options)
                    if dry_run:
                        print("🧪 [Dry run] Would open the mailbox to receive and send lives.")
                    else:
                        print(f"💌 {lives_elapsed / 60:.1f} min passed — receiving and sending lives...")
                        handle_quick_receive_and_send_lives()
                        last_stage = None
                        continue
                hearts_requested = consume_hearts_now_request()
                if hearts_requested or (send_hearts is True and time.time() >= next_hearts_time):
                    next_hearts_time = time.time() + heart_hours * 3600
                    if dry_run:
                        print("🧪 [Dry run] Would scroll the friend list and send lives.")
                    else:
                        print("💌 Sending friend lives...")
                        handle_send_friend_life()
                        last_stage = None
                        continue
                if detection_group == "POST_GAME":
                    detection_group = "PRE_GAME"
                    last_stage = None
                    continue
                reason = stop_reason(stop_rounds, rounds, stop_minutes, run_start_time, stop_coins, coins_earned,
                                     stop_hearts, device_screen)
                if reason:
                    print(f"⏹️ หยุดอัตโนมัติ: {reason}")
                    return
                if not is_first_game:
                    delay = random.uniform(next_game_delay_min, next_game_delay_max)
                    print(f"⏳ Waiting for {delay:.2f} seconds before starting the next game...")
                    time.sleep(delay)
                is_first_game = False
                start_game()
                detection_group = "PRE_GAME"
            elif stage == "PURCHASE_ITEM":
                print("🛒 Detected Stage: PURCHASE_ITEM")
                if boost_cells:
                    set_boost_cells(boost_cells, dry_run)
                if buy_fast_start:
                    purchase_fast_start()
                if buy_cookie_relay:
                    purchase_cookie_relay()
                if options["use_desired_random_boost"]:
                    if dry_run:
                        print(f"🧪 [Dry run] Would roll Random Boost until: {options['desired_boost_name']}")
                    else:
                        purchase_desired_random_boost(options["desired_boost_template"], options["desired_boost_name"])
                play_game()
                round_start_time = time.time()
                jumps = 0
                jump_limit_logged = False
                detection_group = "IN_GAME"
                time.sleep(0.2)
                last_stage = None
            elif stage == "GAME_START":
                print("🏁 Detected Stage: GAME_START")
                if options["use_fast_start"]:
                    using_fast_start()
                detection_group = "IN_GAME"
            elif stage == "GAME_RELAY":
                print("🔄 Detected Stage: GAME_RELAY")
                if options["use_cookie_relay"]:
                    using_cookie_relay()
                    if exit_after_relay and not dry_run:
                        exit_current_run()
                detection_group = "IN_GAME"
            elif stage == "GAME_COMPLETE":
                print("✅ Detected Stage: GAME_COMPLETE")
                rounds += 1
                print(f"🏁 Round #{rounds} finished.")
                if report_stats:
                    seconds = round(time.time() - round_start_time) if round_start_time else None
                    round_start_time = None
                    shot, coins, xp = save_result_screen()
                    emit_stat("round", round=rounds, seconds=seconds, shot=shot, coins=coins, xp=xp)
                    coins_earned += coins or 0
                # Stay on the Result screen for result_delay seconds (counted from when it appeared) before OK
                remaining = result_delay - (time.time() - last_detected_time)
                if remaining > 0:
                    print(f"⏳ Waiting {remaining:.1f} seconds before pressing OK...")
                    time.sleep(remaining)
                complete_finish()
                detection_group = "POST_GAME"
            elif stage == "MYSTERY_BOX":
                print("🎁 Detected Stage: MYSTERY_BOX")
                # the reward shown after "Open all" has the same banner: count the closed box only
                if report_stats and is_mystery_box_closed(device_screen):
                    # one or several boxes (a row of 3, two rows...): count each one
                    box_types = classify_mystery_boxes(device_screen) or [None]
                    print(f"🎁 Boxes: {', '.join(BOX_TYPE_NAMES.get(t, 'unknown') for t in box_types)}")
                    shot = save_shot("box", device_screen)
                    for box_type in box_types:
                        emit_stat("box", shot=shot, kind=box_type)
                accept_mystery_box()
                time.sleep(3)
                detection_group = "POST_GAME"
                last_stage = None
            elif stage == "CONGRATULATIONS":
                print("🎉 Detected Stage: CONGRATULATIONS")
                accept_congratulations()
                detection_group = "POST_GAME"
                last_stage = None
            elif stage == "LEVEL_UP":
                print("⬆️ Detected Stage: LEVEL_UP")
                accept_level_up()
                detection_group = "PRE_GAME"
            elif stage == "DAILY_CHECKIN":
                print("📅 Detected Stage: DAILY_CHECKIN")
                accept_daily_checkin()
                detection_group = "PRE_GAME"
            elif stage == "DAILY_CHECKIN_BOOST_SET":
                print("📅 Detected Stage: DAILY_CHECKIN_BOOST_SET")
                accept_daily_checkin_boost_set()
                detection_group = "PRE_GAME"
            elif stage == "DAILY_TREASURE":
                print("💎 Detected Stage: DAILY_TREASURE")
                accept_daily_treasure()
                detection_group = "PRE_GAME"
            elif stage == "DAILY_NEW":
                print("📰 Detected Stage: DAILY_NEW")
                accept_daily_new()
                detection_group = "PRE_GAME"
            elif stage == "ENTER_LEAGUE":
                print("🏆 Detected Stage: ENTER_LEAGUE")
                accept_enter_league()
                detection_group = "PRE_GAME"
            elif stage == "LEAGUE_RESULTS":
                print("🏆 Detected Stage: LEAGUE_RESULTS")
                accept_league_results()
                detection_group = "PRE_GAME"
            elif stage == "PREVIOUS_RANK_RESULTS":
                print("🏆 Detected Stage: PREVIOUS_RANK_RESULTS")
                accept_previous_rank_results()
                detection_group = "PRE_GAME"
            elif stage == "OVERTAKE_BREAK_SCORE":
                print("🏆 Detected Stage: OVERTAKE_BREAK_SCORE")
                accept_overtake_break_score()
                detection_group = "POST_GAME"
                last_stage = None
            elif stage == "TOO_MANY_TREASURES":
                print("💎 Detected Stage: TOO_MANY_TREASURES")
                accept_too_many_treasures()
                detection_group = "PRE_GAME"
            elif stage == "RELIC_COMPLETE":
                print("🏺 Detected Stage: RELIC_COMPLETE")
                open_relic_complete()
                detection_group = "PRE_GAME"
            elif stage == "RELIC_CLAIM":
                print("🏺 Detected Stage: RELIC_CLAIM")
                accept_relic_claim()
                detection_group = "PRE_GAME"
            elif stage == "ANTI_BOT":
                print("⚠️ Detected Stage: ANTI_BOT")
                if report_stats:
                    emit_stat("card_seen")
                if dry_run:
                    print("🧪 [Dry run] Would solve the Anti-Bot card check.")
                else:
                    handle_anti_bot(device_screen)
                    if report_stats and detect_stage(device_capture_screen(DEVICE_IP, DEVICE_PORT), ["ANTI_BOT"]) is None:
                        emit_stat("card_solved")
                last_stage = None
            elif stage == "CONNECTION_LOST":
                print("🔌 Detected Stage: CONNECTION_LOST")
                if dry_run:
                    print(f"🧪 [Dry run] Would {'restart the app' if restart_on_lost else 'tap Confirm'}.")
                elif not restart_on_lost:
                    handle_connection_lost()
                    last_stage = None
                else:
                    reopen_game()
                    session_start_time = time.time()
                    session_reset_interval = random.uniform(*SESSION_RESET_INTERVAL)
                    last_lives_time = time.time()
                    lives_interval = next_lives_interval(options)
                    detection_group = "PRE_GAME"
                    last_stage = None
                    is_first_game = True
            elif stage == "INACTIVE":
                print("💤 Detected Stage: INACTIVE")
                handle_inactive()
                last_stage = None
            time.sleep(0.25)
    except KeyboardInterrupt:
        print("🛑 Bot stopped by user.")
    except Exception as e:
        print(f"❌ An error occurred: {e}")
