# Asus Accel Tablet Mode Driver

[![License: GPLv2](https://img.shields.io/badge/License-GPL_v2-blue.svg)](https://www.gnu.org/licenses/old-licenses/gpl-2.0.en.html)
![Maintainer](https://img.shields.io/badge/maintainer-ldrahnik-blue)
[![GitHub Release](https://img.shields.io/github/release/asus-linux-drivers/asus-accel-tablet-mode-driver.svg?style=flat)](https://github.com/asus-linux-drivers/asus-accel-tablet-mode-driver/releases)
[![GitHub commits](https://img.shields.io/github/commits-since/asus-linux-drivers/asus-accel-tablet-mode-driver/v0.0.1.svg)](https://GitHub.com/asus-linux-drivers/asus-accel-tablet-mode-driver/commit/)
![Badge](https://hitscounter.dev/api/hit?url=https%3A%2F%2Fgithub.com%2Fasus-linux-drivers%2Fasus-accel-tablet-mode-driver&label=Visitors&icon=suit-heart-fill&color=%23e35d6a)
[![Hits](https://hits.seeyoufarm.com/api/count/incr/badge.svg?url=https%3A%2F%2Fgithub.com%2Fasus-linux-drivers%2Fasus-fliplock-driver&count_bg=%2379C83D&title_bg=%23555555&icon=&icon_color=%23E7E7E7&title=hits&edge_flat=false)](https://hits.seeyoufarm.com)

The driver is written in python and runs as a systemctl service. Driver allow to configure flip events using accelerometer.

If you find this project useful, please do not forget to give it a [![GitHub stars](https://img.shields.io/github/stars/asus-linux-drivers/asus-accel-tablet-mode-driver.svg?style=social&label=Star&maxAge=2592000)](https://github.com/asus-linux-drivers/asus-accel-tablet-mode-driver/stargazers) People already did!

## Changelog

[CHANGELOG.md](CHANGELOG.md)

## Features

- Is allowed to configure key press & release for each flip (default is `KEY_PROG2`)
- Is allowed to configure event with different state for each mode (default is `SWITCH_TOGGLE` : `switch tablet-mode state 0` or `switch tablet-mode state 1`)

```
$ sudo libinput debug-events
...
-event7   DEVICE_ADDED            Asus WMI accel tablet mode        seat0 default group9  cap:kS
...
 event7   KEYBOARD_KEY            +0.000s	KEY_PROG2 (149) pressed
 event7   KEYBOARD_KEY            +0.000s	KEY_PROG2 (149) released
 event7   SWITCH_TOGGLE           +0.000s	switch tablet-mode state 1
 event7   KEYBOARD_KEY            +1.004s	KEY_PROG2 (149) pressed
 event7   KEYBOARD_KEY            +1.004s	KEY_PROG2 (149) released
 event7   SWITCH_TOGGLE           +1.004s	switch tablet-mode state 0
```

```
$ sudo acpi_listen
video/tabletmode TBLT 0000008A 00000001
video/tabletmode TBLT 0000008A 00000000
```

## Limitations

- This driver identifies tablet mode by accelerometer data which means when is laptop's display almost in the horizontal position it is recognized as tablet mode and does not matter whether is laptop flipped or not

## Installation

Get latest dev version using `git`

```bash
$ git clone https://github.com/asus-linux-drivers/asus-accel-tablet-mode-driver
$ cd asus-accel-tablet-mode-driver
```

and install

```bash
$ bash install.sh
```

or run separately parts of the install script

- run notifier every time when the user log in (do NOT run as `$ sudo`, works via `systemctl --user`)

```bash
$ bash install_service.sh
```

- install the break-glass launcher (see [Stuck in tablet mode](#stuck-in-tablet-mode))

```bash
$ bash install_break_glass.sh
```

## Uninstallation

To uninstall run

```bash
$ bash uninstall.sh
```

or run separately parts of the uninstall script

```bash
$ bash uninstall_service.sh
$ bash uninstall_break_glass.sh
```

## Stuck in tablet mode

The machine can come back from suspend with tablet mode still asserted, which leaves
the built-in keyboard and touchpad disabled. Two things cause it and both are handled:

1. The firmware replays the hinge event (`KEY_PROG2`) on wake. Those queued events are
   stale, so on resume the driver discards everything the hinge device queued while
   asleep, forces laptop mode and ignores hinge events for 3 seconds.
2. The compositor can hold on to a tablet state of its own. Re-sending
   `SW_TABLET_MODE 0` cannot fix that, because the kernel drops a switch value equal to
   the current one, so no event is ever delivered. Instead the driver destroys and
   re-creates its virtual switch device once the resume grace window closes: libinput
   sees the device go away and come back reading `0`, which is the same recovery the
   manual rotate-to-portrait-and-back workaround triggers indirectly.

### Break-glass launcher

If input is ever disabled anyway, the recovery has to be reachable without a keyboard
or a touchpad. `install_break_glass.sh` installs an **Unstick Tablet Mode** launcher;
pin it to the GNOME dash (Activities, long press the icon, *Pin to Dash*) and one touch
restores laptop mode. It sends `SIGUSR1` to the driver, which replays a
tablet-to-laptop edge and re-creates the switch device, and restarts the service if the
driver is not running.

The same thing from a terminal or over SSH:

```bash
$ asus-tablet-mode-unstick
```

or, without the helper installed:

```bash
$ systemctl --user kill -s SIGUSR1 asus_accel_tablet_mode_driver@$USER.service
```

**Troubleshooting**

To activate logger, do in a console:
```
LOG=DEBUG sudo -E ./asus_accel_driver.py "default"
```

**Why was this project created?** For laptops which do not indicate tablet modes (e.g. `Zenbook UN5401QAB` or `Vivobook TM420` do not send `EV_SW.SW_TABLET_MODE` or `EV_KEY.KEY_PROG2` either)
([see the reported issue for Kernel](https://bugzilla.kernel.org/show_bug.cgi?id=214675))
