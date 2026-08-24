#!/usr/bin/env python3

import sys
import errno
import importlib
import logging
import os
import glob
import select
import signal
import time
from typing import Optional
from libevdev import Device, EV_SW, EV_KEY, EV_SYN, InputEvent, EventsDroppedException
from time import sleep
import subprocess

logging.basicConfig(
    format='%(asctime)s %(levelname)-8s %(message)s',
    level=os.environ.get('LOG', 'INFO')
)
log = logging.getLogger('Asus Accel Tablet Mode Driver')

DEVICE_NAME = "Asus WMI accel tablet mode"

DEBOUNCE_S = 0.3
# After suspend/resume: force laptop mode and ignore hinge events for this long.
# Firmware replays spurious KEY_PROG2 events on wake, and the accelerometer is not
# a reliable orientation source (raw values are unscaled and only one flat pose
# is detectable), so laptop mode is always assumed on resume.
RESUME_GRACE_S = 3.0
RESUME_DRIFT_THRESHOLD_S = 3.0
# Main loop wake-up interval; also how fast a resume is noticed when the
# firmware queues no events at all.
POLL_S = 0.5
# Let udev, libinput, Xorg, Wayland see a freshly created uinput device.
UINPUT_SETTLE_S = 1.0
# Width of the synthetic tablet->laptop edge used to unstick a compositor that
# cached a stale tablet state. Long enough for libinput to process, short enough
# not to be noticeable.
RESYNC_PULSE_S = 0.15
# Gap between destroying and re-creating the switch device.
RECREATE_GAP_S = 0.4


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


class ModeEmitter:
    """Owns the virtual device that reports the mode (SW_TABLET_MODE) to libinput."""

    def __init__(self):
        self.udev = None
        self._create()

    def _template(self):
        dev = Device()
        dev.name = DEVICE_NAME

        for key_to_enable in layout.flip_keys:
            if isEventKey(key_to_enable):
                dev.enable(key_to_enable)
        for event_to_enable in layout.laptop_mode_events:
            if isEventInput(event_to_enable):
                dev.enable(event_to_enable.code)
        for event_to_enable in layout.tablet_mode_events:
            if isEventInput(event_to_enable):
                dev.enable(event_to_enable.code)
        return dev

    def _create(self):
        self.udev = self._template().create_uinput_device()
        sleep(UINPUT_SETTLE_S)

    def _destroy(self):
        # The device node has to be gone before the new one appears, so the
        # destruction is explicit: waiting for refcounting to call __del__ left
        # the old node alive next to the new one. Removal is what makes libinput
        # forget everything it cached about this switch, including a tablet state
        # it refused to let go of.
        udev, self.udev = self.udev, None
        if udev is None:
            return
        uinput = getattr(udev, '_uinput', None)
        if uinput is not None:
            uinput.__exit__()
            udev._uinput = None
        del udev

    def recreate(self):
        log.info("Re-creating %s so libinput drops its cached switch state", DEVICE_NAME)
        self._destroy()
        sleep(RECREATE_GAP_S)
        self._create()

    def emit(self, tablet_mode):
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
            self.udev.send_events(keys_to_send_press_events)
            self.udev.send_events(sync_event)
            self.udev.send_events(keys_to_send_release_events)
            self.udev.send_events(sync_event)
            self.udev.send_events(events_to_send)
            self.udev.send_events(sync_event)
        except OSError as e:
            log.error("Cannot send event, %s", e)

    def force_laptop_mode(self, pulse=False, recreate=False):
        """Assert laptop mode, optionally with the heavier unstick steps.

        The kernel drops an EV_SW value identical to the current one, so plain
        re-assertion is a no-op whenever the switch already reads 0 while the
        compositor still behaves as if it were 1. `pulse` replays the physical
        tablet->laptop transition to guarantee an edge; `recreate` additionally
        makes the device disappear and come back, which is what the manual
        rotate-and-rotate-back workaround achieves indirectly.
        """
        if pulse:
            self.emit(True)
            sleep(RESYNC_PULSE_S)
        self.emit(False)
        if recreate:
            self.recreate()
            self.emit(False)


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


def suspend_drift():
    """BOOTTIME advances during suspend while MONOTONIC pauses; the gap grows on resume."""
    return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()


# Signals are the break-glass interface: they work over `systemctl --user kill`,
# which needs neither a keyboard nor a privileged helper.
unstick_requested = False
terminate_requested = False


def on_unstick(signum, frame):
    global unstick_requested
    unstick_requested = True


def on_terminate(signum, frame):
    global terminate_requested
    terminate_requested = True


emitter = ModeEmitter()
tablet_mode = infer_initial_tablet_mode()

wmi_path = find_wmi_hotkey_device()
wmi_fd = open(wmi_path, 'rb')
wmi_dev = Device(wmi_fd)
os.set_blocking(wmi_fd.fileno(), False)

if not no_grab:
    wmi_dev.grab()
    log.debug("Grabbed %s exclusively", wmi_path)

# A pipe woken by signal delivery, so select() returns immediately on SIGUSR1.
sig_r, sig_w = os.pipe()
os.set_blocking(sig_r, False)
os.set_blocking(sig_w, False)
signal.set_wakeup_fd(sig_w)
signal.signal(signal.SIGUSR1, on_unstick)
signal.signal(signal.SIGTERM, on_terminate)
signal.signal(signal.SIGINT, on_terminate)

log.info("Listening for hinge events on %s (grab=%s)", wmi_path, not no_grab)

# Broadcast initial state so SW_TABLET_MODE is not stale after a driver restart
emitter.emit(tablet_mode)


def drain_hinge_events():
    """Read and discard everything queued on the hinge device."""
    dropped = 0
    try:
        for _ in wmi_dev.events():
            dropped += 1
    except EventsDroppedException:
        pass
    except OSError as e:
        if e.errno != errno.EAGAIN:
            raise
    return dropped


drift = suspend_drift()
last_toggle_time = 0.0
grace_until = 0.0
reassert_at = None

while not terminate_requested:
    timeout = POLL_S
    if reassert_at is not None:
        timeout = max(0.05, min(timeout, reassert_at - time.monotonic()))

    try:
        readable, _, _ = select.select([wmi_fd, sig_r], [], [], timeout)
    except (OSError, InterruptedError):
        continue

    now = time.monotonic()

    if sig_r in readable:
        try:
            os.read(sig_r, 4096)
        except BlockingIOError:
            pass

    if terminate_requested:
        break

    # Resume is checked before any event is looked at: everything the firmware
    # queued while asleep is stale and must not reach the toggle logic.
    new_drift = suspend_drift()
    resumed = new_drift - drift >= RESUME_DRIFT_THRESHOLD_S
    slept_for = new_drift - drift
    drift = new_drift

    if resumed or unstick_requested:
        by_hand = unstick_requested
        unstick_requested = False
        dropped = drain_hinge_events()
        if resumed:
            log.info("Resume from suspend detected (slept ~%.0fs); forcing laptop mode "
                     "(%d queued hinge events discarded)", slept_for, dropped)
        if by_hand:
            log.info("Unstick requested (SIGUSR1); forcing laptop mode the hard way")
        tablet_mode = False
        emitter.force_laptop_mode(pulse=by_hand, recreate=by_hand)
        now = time.monotonic()
        grace_until = now + RESUME_GRACE_S
        # Firmware sometimes replays the hinge event a second or two late, and a
        # compositor that resumed holding a stale tablet state ignores a switch
        # value it thinks it already has. Rebuilding the device once the grace
        # window closes covers both.
        reassert_at = grace_until if resumed else None
        last_toggle_time = now
        continue

    if reassert_at is not None and now >= reassert_at:
        reassert_at = None
        if not tablet_mode:
            # Plain re-assertion cannot help here (the switch already reads 0),
            # so the device is rebuilt instead: libinput re-reads the state of a
            # brand new switch and cannot keep a tablet state of its own. No
            # synthetic tablet edge is used, which would briefly claim a posture
            # the machine is not in.
            log.info("Re-asserting laptop mode after resume grace window")
            emitter.force_laptop_mode(recreate=True)

    if wmi_fd not in readable:
        continue

    try:
        events = list(wmi_dev.events())
    except EventsDroppedException:
        continue
    except OSError as e:
        if e.errno == errno.EAGAIN:
            continue
        raise

    for event in events:
        if not (event.matches(EV_KEY.KEY_PROG2) and event.value == 1):
            continue

        now = time.monotonic()
        if now < grace_until:
            log.info("Ignoring hinge event within %.0fs of resume", RESUME_GRACE_S)
            continue
        if now - last_toggle_time < DEBOUNCE_S:
            continue

        last_toggle_time = now
        tablet_mode = not tablet_mode
        emitter.emit(tablet_mode)
        log.info("Hinge event → %s", "tablet" if tablet_mode else "laptop")

log.info("Terminating; releasing %s and leaving laptop mode asserted", wmi_path)
try:
    if not no_grab:
        wmi_dev.ungrab()
except OSError:
    pass
emitter.emit(False)
