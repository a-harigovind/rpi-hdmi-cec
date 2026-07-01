import subprocess
import sys
import re
import time
import threading
from evdev import UInput, ecodes

# Relative movement deltas (dx, dy) per direction.
DELTAS = {
    "up": (0, -3),
    "down": (0, 3),
    "left": (-3, 0),
    "right": (3, 0),
}

# Directions that scroll (on a double-press) and their wheel sign.
SCROLL_DIRECTION = {
    "up": 1,    # scroll up
    "down": -1,  # scroll down
}

# CEC key name -> keyboard key to tap.
KEY_MAP = {
    "exit": ecodes.KEY_ESC,
    "Fast": ecodes.KEY_RIGHT,
    "rewind": ecodes.KEY_LEFT,
    "pause": ecodes.KEY_K,
}

# Some kernels/evdev builds lack the hi-res wheel codes; fall back gracefully.
REL_WHEEL_HI_RES = getattr(ecodes, "REL_WHEEL_HI_RES", None)

# Separate virtual devices: mixing EV_REL motion with keyboard keys on one
# device makes libinput treat it as a pointer and drop the keyboard events.
KEYBOARD_CAPS = {
    # KEY_F5 is emitted on a long-hold of "exit", so it must be declared even
    # though it is not in KEY_MAP.
    ecodes.EV_KEY: list(KEY_MAP.values()) + [ecodes.KEY_F5],
}
MOUSE_REL = [ecodes.REL_X, ecodes.REL_Y, ecodes.REL_WHEEL, ecodes.REL_HWHEEL]
if REL_WHEEL_HI_RES is not None:
    MOUSE_REL.append(REL_WHEEL_HI_RES)
MOUSE_CAPS = {
    ecodes.EV_KEY: [ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_MIDDLE],
    ecodes.EV_REL: MOUSE_REL,
}

# Mouse acceleration tuning.
SPEED_START = 1.0
SPEED_MAX = 18.0
SPEED_STEP = 0.4
FRAME_INTERVAL = 0.05

# Scroll tuning.
SCROLL_INTERVAL = 0.2

# Press/release must land in separate event frames (with a short hold) or
# libinput collapses them and the tap/click is ignored.
KEY_TAP_HOLD = 0.02

# Max gap between a release and the next same-direction press to count as a
# double-press.
DOUBLE_PRESS_WINDOW = 0.15

# Hold "exit" at least this long to trigger F5 (refresh) instead of Esc.
EXIT_HOLD_SECONDS = 2.5


def tap_key(ui, code):
    """Press and release a single key."""
    ui.write(ecodes.EV_KEY, code, 1)
    ui.syn()
    time.sleep(KEY_TAP_HOLD)
    ui.write(ecodes.EV_KEY, code, 0)
    ui.syn()


def click_mouse(ui, button=ecodes.BTN_LEFT):
    """Press and release a mouse button."""
    tap_key(ui, button)


def _wait_for_release(process):
    """Block until cec-client reports a key/control release."""
    print("Waiting for release...")
    while True:
        line = process.stdout.readline()
        if not line:
            break  # process died
        line = line.strip()
        if "user control release" in line or "key released" in line:
            print(line)
            break


def run_until_release(process, frame_fn, interval):
    """
    Run `frame_fn(frame_index)` every `interval` seconds in a background thread
    until cec-client reports a release, then stop and join.
    """
    stop_movement = threading.Event()

    def loop():
        i = 0
        while not stop_movement.is_set():
            frame_fn(i)
            i += 1
            time.sleep(interval)

    t = threading.Thread(target=loop)
    t.start()
    _wait_for_release(process)
    stop_movement.set()
    t.join()


def move_mouse(ui_mouse, direction, process):
    """Continuously move the pointer (with acceleration) until release."""
    dx, dy = DELTAS[direction]

    def frame(i):
        speed = min(SPEED_START + i * SPEED_STEP, SPEED_MAX)
        ui_mouse.write(ecodes.EV_REL, ecodes.REL_X, int(dx * speed))
        ui_mouse.write(ecodes.EV_REL, ecodes.REL_Y, int(dy * speed))
        ui_mouse.syn()

    run_until_release(process, frame, FRAME_INTERVAL)


def scroll_mouse(ui_mouse, direction, process):
    """Continuously scroll the wheel until release."""
    step = SCROLL_DIRECTION[direction]

    def frame(i):
        if REL_WHEEL_HI_RES is not None:
            ui_mouse.write(ecodes.EV_REL, REL_WHEEL_HI_RES, step * 120)
        ui_mouse.write(ecodes.EV_REL, ecodes.REL_WHEEL, step)
        ui_mouse.syn()

    run_until_release(process, frame, SCROLL_INTERVAL)


def handle_exit(ui_kbd, process):
    """
    Short press of "exit" -> Esc. Holding "exit" for EXIT_HOLD_SECONDS -> F5
    (refresh). The release that follows a long hold is consumed here so it does
    not leak back into the main loop.
    """
    released = threading.Event()

    def waiter():
        _wait_for_release(process)
        released.set()

    t = threading.Thread(target=waiter)
    t.start()

    if released.wait(timeout=EXIT_HOLD_SECONDS):
        # Released before the hold threshold -> normal Esc.
        tap_key(ui_kbd, ecodes.KEY_ESC)
    else:
        # Held long enough -> refresh.
        print("Exit held -> F5 (refresh)")
        tap_key(ui_kbd, ecodes.KEY_F5)

    t.join()


def run_cec_parser():
    """
    Runs cec-client, parses the output, and emits the corresponding input events.
    """
    command = ["cec-client", "-s", "-d", "0"]

    print("Starting CEC key monitor... Press Ctrl+C to exit.")

    ui_kbd = UInput(KEYBOARD_CAPS, name="cec-remote-kbd")
    ui_mouse = UInput(MOUSE_CAPS, name="cec-remote-mouse")
    process = None

    # Matches: "key pressed: up (1) ..." -> group(1) = "up"
    pressed_re = re.compile(r"key pressed:\s+(\w+)")
    # Matches: "key released: up (1) ..." -> group(1) = "up"
    released_re = re.compile(r"key released:\s+(\w+)")

    # Double-press tracking: direction of the last directional press and the
    # time its hold ended.
    last_dir = None
    last_release_time = 0.0
    press_count = 0

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        while True:
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            if not line:
                continue

            line = line.strip()

            if "key pressed:" in line:
                # cec-client emits two lines per press; the one containing
                # 'current' is the robust one to act on (avoids double events).
                if "current" not in line:
                    continue
                match = pressed_re.search(line)
                if not match:
                    continue
                key_name = match.group(1)
                print(f"Key pressed - {key_name}")

                if key_name in DELTAS:
                    now = time.monotonic()
                    
                    # Check if this continues a sequence: same direction + within window
                    if key_name == last_dir and (now - last_release_time) < DOUBLE_PRESS_WINDOW:
                        press_count += 1
                    else:
                        # New direction or timeout → reset to 1
                        press_count = 1
                    
                    last_dir = key_name
                    last_release_time = now

                    # Triple press (3rd press) triggers scroll
                    if press_count >= 3 and key_name in SCROLL_DIRECTION:
                        scroll_mouse(ui_mouse, key_name, process)
                        # Reset so next press starts fresh
                        press_count = 0
                        last_dir = None
                        last_release_time = 0.0
                    else:
                        move_mouse(ui_mouse, key_name, process)
                        last_dir = key_name
                        last_release_time = time.monotonic()
                elif key_name == "select":
                    click_mouse(ui_mouse, ecodes.BTN_LEFT)
                elif key_name == "exit":
                    handle_exit(ui_kbd, process)
                elif key_name in KEY_MAP:
                    tap_key(ui_kbd, KEY_MAP[key_name])

            elif "key released:" in line:
                match = released_re.search(line)
                if match:
                    print(f"Key released - {match.group(1)}")

            elif "user control release" in line:
                print("Key released")

    except FileNotFoundError:
        print("Error: 'cec-client' executable not found.")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nStopping CEC monitor...")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        sys.exit(1)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
        ui_kbd.close()
        ui_mouse.close()


if __name__ == "__main__":
    run_cec_parser()
