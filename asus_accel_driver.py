#!/usr/bin/env python3

import sys
import importlib
import logging
import os
import glob
import time
import threading
from typing import Optional
from libevdev import Device, EV_SW, EV_KEY, EV_SYN, InputEvent
from time import sleep
import subprocess

logging.basicConfig(
    format='%(asctime)s %(levelname)-8s %(message)s',
    level=os.environ.get('LOG', 'INFO')
)
log = logging.getLogger('Asus Accel Tablet Mode Driver')

DEBOUNCE_S = 0.3
# After suspend/resume: force laptop mode and ignore hinge events for this long.
# Firmware can replay a spurious KEY_PROG2 on wake, and the accelerometer is not
# a reliable orientation source (raw values are unscaled and only one flat pose
# is detectable), so laptop mode is always assumed on resume.
RESUME_GRACE_S = 3.0
RESUME_POLL_S = 2.0
RESUME_DRIFT_THRESHOLD_S = 3.0


# Layout
layout_name = 'default'
if len(sys.argv) > 1 and not sys.argv[1].startswith('--'):
    layout_name = sys.argv[1]
try:
    layout = importlib.import_module('conf.' + layout_name)
except Exception:
    log.error("Layout *.py from dir conf is required as first argument. Re-run install script or add missing first argument (valid value is default, ..).")
    sys.exit(1)

no_grab = '--no-grab' in sys.argv


def isEventKey(key):
    if hasattr(key, "name") and hasattr(EV_KEY, key.name):
        return True
    elif hasattr(key, "name") and hasattr(EV_SW, key.name):
        return True
    return False


def isEventInput(event):
    if hasattr(event, "code") and isEventKey(event.code):
        return True
    return False


dev = Device()
dev.name = "Asus WMI accel tablet mode"

for key_to_enable in layout.flip_keys:
    if isEventKey(key_to_enable):
        dev.enable(key_to_enable)
for event_to_enable in layout.laptop_mode_events:
    if isEventInput(event_to_enable):
        dev.enable(event_to_enable.code)
for event_to_enable in layout.tablet_mode_events:
    if isEventInput(event_to_enable):
        dev.enable(event_to_enable.code)

# Sleep so udev, libinput, Xorg, Wayland have had a chance to see the device
udev = dev.create_uinput_device()
sleep(1)


def flip(tablet_mode):
    keys_to_send_press_events = []
    keys_to_send_release_events = []
    events_to_send = []

    for keys_to_send_press in layout.flip_keys:
        if isEventKey(keys_to_send_press):
            keys_to_send_press_events.append(InputEvent(keys_to_send_press, 1))
    for keys_to_send_release in layout.flip_keys:
        if isEventKey(keys_to_send_release):
            keys_to_send_release_events.append(InputEvent(keys_to_send_release, 0))

    if tablet_mode:
        for event_to_send in layout.tablet_mode_events:
            if isEventInput(event_to_send):
                events_to_send.append(event_to_send)
    else:
        for event_to_send in layout.laptop_mode_events:
            if isEventInput(event_to_send):
                events_to_send.append(event_to_send)

    sync_event = [InputEvent(EV_SYN.SYN_REPORT, 0)]

    try:
        udev.send_events(keys_to_send_press_events)
        udev.send_events(sync_event)
        udev.send_events(keys_to_send_release_events)
        udev.send_events(sync_event)
        udev.send_events(events_to_send)
        udev.send_events(sync_event)
    except OSError as e:
        log.error("Cannot send event, %s", e)


def find_accel_device() -> Optional[str]:
    """Return the sysfs path of the IIO accelerometer, or None if not found."""
    cmd = ["udevadm", "info", "--export-db"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    accel_detected = 0
    for bytes_line in proc.stdout.readlines():
        try:
            line = bytes_line.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if accel_detected == 0 and "accel" in line:
            accel_detected = 1
        elif accel_detected == 1 and "P: " in line:
            return "/sys" + line.split(" ")[1].rstrip()
    return None


def infer_initial_tablet_mode() -> bool:
    """Read accelerometer once at startup to guess current physical orientation."""
    accel_path = find_accel_device()
    if not accel_path:
        log.warning("Accelerometer not found; assuming laptop mode at startup")
        return False
    try:
        def _read(name):
            with open(os.path.join(accel_path, name)) as f:
                return float(f.read())
        x = _read("in_accel_x_raw")
        y = _read("in_accel_y_raw")
        z = _read("in_accel_z_raw")
        is_tablet = (abs(x) <= 5 and abs(y) <= 5 and z <= -9)
        log.info("Initial state from accelerometer: %s (x=%.1f y=%.1f z=%.1f)",
                 "tablet" if is_tablet else "laptop", x, y, z)
        return is_tablet
    except OSError as e:
        log.warning("Could not read accelerometer: %s; assuming laptop mode", e)
        return False


def find_wmi_hotkey_device() -> str:
    """Return the /dev/input/eventN path for the Asus WMI hotkeys device."""
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            with open(path, 'rb') as f:
                d = Device(f)
                if d.name == "Asus WMI hotkeys" or (d.phys and "asus-nb-wmi" in d.phys):
                    log.info("Found WMI hotkey device: %s (%s)", path, d.name)
                    return path
        except (OSError, PermissionError):
            continue
    raise RuntimeError("Asus WMI hotkeys device not found in /dev/input/ — is asus-nb-wmi loaded?")


tablet_mode = infer_initial_tablet_mode()

state_lock = threading.Lock()
last_resume_time = 0.0


def _suspend_drift():
    """BOOTTIME advances during suspend while MONOTONIC pauses; the gap grows on resume."""
    return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()


def resume_watcher():
    global tablet_mode, last_resume_time
    drift = _suspend_drift()
    while True:
        sleep(RESUME_POLL_S)
        new_drift = _suspend_drift()
        if new_drift - drift >= RESUME_DRIFT_THRESHOLD_S:
            log.info("Resume from suspend detected (slept ~%.0fs); forcing laptop mode",
                     new_drift - drift)
            with state_lock:
                tablet_mode = False
                last_resume_time = time.monotonic()
            flip(False)
        drift = new_drift


threading.Thread(target=resume_watcher, daemon=True).start()

wmi_path = find_wmi_hotkey_device()
wmi_fd = open(wmi_path, 'rb')
wmi_dev = Device(wmi_fd)

if not no_grab:
    wmi_dev.grab()
    log.debug("Grabbed %s exclusively", wmi_path)

log.info("Listening for hinge events on %s (grab=%s)", wmi_path, not no_grab)

# Broadcast initial state so SW_TABLET_MODE is not stale after a driver restart
flip(tablet_mode)

last_toggle_time = 0.0

for event in wmi_dev.events():
    if event.matches(EV_KEY.KEY_PROG2) and event.value == 1:
        now = time.monotonic()
        with state_lock:
            if now - last_resume_time < RESUME_GRACE_S:
                log.info("Ignoring hinge event within %.0fs of resume", RESUME_GRACE_S)
                continue
            if now - last_toggle_time < DEBOUNCE_S:
                continue
            last_toggle_time = now
            tablet_mode = not tablet_mode
            new_mode = tablet_mode
        flip(new_mode)
        log.info("Hinge event → %s", "tablet" if new_mode else "laptop")
