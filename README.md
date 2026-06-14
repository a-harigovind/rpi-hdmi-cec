# hdmiCEC

Use your TV's HDMI-CEC remote to control a Raspberry Pi (or any Linux machine with libinput). The script listens for CEC key events from your TV and translates them into keyboard, mouse, and scroll input via virtual devices.

**Main script:** [`cecRemoteWithScroll.py`](cecRemoteWithScroll.py)

## Features

- **Pointer movement** — Hold up/down/left/right to move the cursor with acceleration
- **Mouse click** — `select` sends a left click
- **Scroll** — Double-press up or down, then hold the second press to scroll continuously until release
- **Keyboard shortcuts** — Rewind, forward, pause, and exit map to keyboard keys
- **Long-hold exit** — Short press sends Esc; hold for 2.5 seconds sends F5 (refresh)
- **Compositor-agnostic** — Uses kernel `UInput` instead of Wayland-specific tools like `wlrctl`

## Requirements

- Raspberry Pi (or any Linux machine) with HDMI-CEC support
- TV or AV receiver that forwards CEC remote key presses
- Python 3
- [`cec-client`](https://github.com/Pulse-Eight/libcec) (from `libcec` / `cec-utils`)
- Python [`evdev`](https://pypi.org/project/evdev/) package
- Write access to `/dev/uinput` (typically via the `input` group)

### Install dependencies

```bash
sudo apt install cec-utils python3-evdev
```

If `python3-evdev` is not available on your distro:

```bash
pip install evdev
```

Add your user to the `input` group if needed, then log out and back in:

```bash
sudo usermod -aG input $USER
```

### HDMI-CEC setup

CEC must be enabled on both the Pi and the TV. On Raspberry Pi OS, this is usually handled automatically when the HDMI cable is connected. Verify CEC is working:

```bash
echo 'scan' | cec-client -s -d 1
```

You should see your TV listed as a CEC device. The main script uses device `0` (`cec-client -s -d 0`). If you get no key events, try changing the device index in `cecRemoteWithScroll.py`.

## Usage

Run the script from the project directory:

```bash
python3 cecRemoteWithScroll.py
```

If you get a permission error on `/dev/uinput`, either add your user to the `input` group (see above) or run with `sudo`:

```bash
sudo python3 cecRemoteWithScroll.py
```

Press **Ctrl+C** to stop. The script prints each key press and release to the terminal for debugging.

Only one process should use the CEC adapter at a time. Stop other `cec-client` instances before starting the script.

### Run at login (optional)

To start automatically, add a systemd user service or a desktop autostart entry that runs the script in the background.

## Remote key mappings

| CEC key   | Action |
|-----------|--------|
| Up        | Move cursor up (hold) |
| Down      | Move cursor down (hold) |
| Left      | Move cursor left (hold) |
| Right     | Move cursor right (hold) |
| Up (double-press + hold) | Scroll up until release |
| Down (double-press + hold) | Scroll down until release |
| Select    | Left mouse click |
| Exit      | Esc (short press) |
| Exit (hold ≥ 2.5 s) | F5 (refresh) |
| Rewind    | Left arrow key |
| Fast      | Right arrow key |
| Pause     | K key |

## How it works

1. `cec-client -s -d 0` runs in monitor mode and streams key events to stdout.
2. The script parses lines like `key pressed: up` and `key released: up`.
3. Two separate virtual input devices are created:
   - `cec-remote-kbd` — keyboard keys (Esc, arrows, F5, etc.)
   - `cec-remote-mouse` — pointer motion, mouse buttons, and scroll wheel

Keyboard and mouse capabilities are split across two devices on purpose. A single `UInput` device that reports both `EV_REL` motion and keyboard keys is classified by libinput as a pointer, which causes keyboard events to be dropped.

Mouse movement and scrolling run in a background thread until `cec-client` reports a release event. While a directional key is held for movement or scroll, other CEC keys are not handled until that action finishes.

## Tuning

Constants at the top of `cecRemoteWithScroll.py` control behavior:

| Constant | Default | Description |
|----------|---------|-------------|
| `SPEED_START` | 1.0 | Initial pointer speed multiplier |
| `SPEED_MAX` | 18.0 | Maximum pointer speed |
| `SPEED_STEP` | 0.4 | Acceleration per frame |
| `FRAME_INTERVAL` | 0.05 s | Pointer update interval (~20 Hz) |
| `SCROLL_INTERVAL` | 0.2 s | Scroll notch interval |
| `DOUBLE_PRESS_WINDOW` | 0.6 s | Max gap for double-press detection |
| `EXIT_HOLD_SECONDS` | 2.5 s | Hold duration for F5 vs Esc |
| `KEY_TAP_HOLD` | 0.02 s | Delay between key down and up |

Increase `SCROLL_INTERVAL` to slow scrolling; decrease `FRAME_INTERVAL` for smoother (but heavier) pointer movement.
