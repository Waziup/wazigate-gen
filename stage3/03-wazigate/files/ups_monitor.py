#!/usr/bin/env python3
"""
ups_monitor.py - Waveshare UPS HAT (INA219) monitor with graceful shutdown.

Watches the battery voltage over I2C and powers the system down cleanly while
there is still enough charge left to actually do it.

All tuning is done through environment variables (see the systemd unit), so
thresholds can be changed without editing this file.

Why the defaults look the way they do
-------------------------------------
On 2026-08-19 this gateway's pack stopped delivering power at ~7.18 V, more
than a volt above the 6.1 V the previous version watched for. The threshold
was unreachable, so the graceful shutdown could never fire and every outage
ended in a hard power cut. The cause was two mismatched cells (3.2 V and
3.55 V): the weak one tripped the pack protection while the pack total still
looked healthy.

With a MATCHED pair installed, SHUTDOWN_VOLTAGE defaults to 6.50 V, i.e.
3.25 V per cell. That sits above the ~3.0 V/cell point below which a Li-ion
cell has only 2-3% of its capacity left and starts losing cycle life, with
margin for the voltage sag the Pi load causes. HARD_FLOOR_VOLTAGE is 6.20 V
(3.10 V/cell) as a genuine last resort.

Confirm against a real outage. For that one run, set TRACE_DIR and a short
WARN_LOG_INTERVAL to record a high-resolution discharge curve; leave both off
in normal operation, because writing a row every few seconds is needless SD
card wear. If the board browns out above 6.50 V, raise the threshold - do not
lower it below 6.40 V for a 2S pack.

Why STANDBY instead of poweroff (ON_TRIP)
-----------------------------------------
Calling poweroff on a Pi fed by this HAT is a trap. The SoC halts, but the HAT
keeps the 5 V rail up as long as the pack is above its own protection point,
and a halted Pi cannot notice mains returning - so the gateway stays dark
until somebody physically power-cycles it. This HAT has no software
controllable output (the INA219 only measures), and GPIO3 wake-from-halt no
longer works on the Pi 4.

So the default trip action is STANDBY: stop the containers, stop the Docker
units, move journald off the root filesystem, sync, make the root filesystem
write-safe, and keep monitoring.

Making it write-safe takes two attempts, because a plain remount read-only
fails with EBUSY on a live system - dockerd, containerd, journald and any
host-side application still hold writable handles:

  1. mount -o remount,ro /   - cleanest when it works.
  2. fsfreeze -f /           - fallback. Flushes everything and blocks further
                               writes without caring about open handles, which
                               is exactly the situation here.

  * mains returns   -> remount rw, restart the containers, carry on. No visit.
  * battery empties -> the HAT protection cuts the rail itself. That hard cut
                       is harmless because the filesystem is already read-only,
                       and it leaves the Pi genuinely off, so the next time
                       mains returns the gateway boots normally.

Either way the device recovers without a site visit. Set ON_TRIP=poweroff for
the old behaviour if the hardware can be power-cycled some other way.

Design rules for the critical path
----------------------------------
* Nothing may raise: every subprocess call is wrapped and returns a bool.
* Nothing may block forever: every subprocess call has a timeout, and the
  poweroff path escalates if systemd fails to take us down.
* A single glitched I2C read must never shut the gateway down: readings are
  median-filtered, range-checked and the trip is debounced.
* A RISING voltage must never shut the gateway down. If the battery is
  charging then mains is present, and powering off would leave the device off
  until somebody physically visits it - worse than losing the page cache.
* Log lines carry a syslog priority prefix, so journald records real levels
  instead of filing everything as info. Beyond making journalctl -p useful,
  journald flushes CRIT and above to disk immediately rather than within
  SyncIntervalSec (5 min by default), so the shutdown reason survives a power
  cut seconds later without needing a second log file.
"""

import logging
import os
import signal
import subprocess
import sys
import time
from collections import deque

import smbus2

LOG = logging.getLogger("ups")

# syslog priorities: journald flushes crit and above to disk immediately.
_PRIORITY = {logging.CRITICAL: 2, logging.ERROR: 3, logging.WARNING: 4,
             logging.INFO: 6, logging.DEBUG: 7}


class JournalFormatter(logging.Formatter):
    """Prefix each line with a <N> syslog priority.

    systemd files unprefixed stderr as plain info, which loses the level and
    means even the shutdown message waits for the next journal sync. With the
    prefix, journalctl -p works and crit lines hit the disk at once.
    """

    def format(self, record):
        return "<%d>%s" % (_PRIORITY.get(record.levelno, 6),
                           logging.Formatter.format(self, record))

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def _env_str(name, default):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def _env_int(name, default):
    raw = _env_str(name, None)
    if raw is None:
        return default
    try:
        return int(raw, 0)          # accepts decimal and 0x.. forms
    except ValueError:
        LOG.error("Invalid %s=%r, falling back to %r", name, raw, default)
        return default


def _env_float(name, default):
    raw = _env_str(name, None)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        LOG.error("Invalid %s=%r, falling back to %r", name, raw, default)
        return default


def _env_bool(name, default):
    raw = _env_str(name, None)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


I2C_BUS   = _env_int("I2C_BUS", 1)
I2C_ADDR  = _env_int("I2C_ADDR", 0x42)
REG_SHUNT = 0x01
REG_BUS   = 0x02

# Trip levels
SHUTDOWN_VOLTAGE   = _env_float("SHUTDOWN_VOLTAGE", 6.50)
HARD_FLOOR_VOLTAGE = _env_float("HARD_FLOOR_VOLTAGE", 6.20)
WARN_VOLTAGE       = _env_float("WARN_VOLTAGE", 7.00)

# Readings outside this window are treated as bus glitches, not as a flat
# battery. Without this a corrupted read of 0.00 V would trigger a shutdown.
PLAUSIBLE_MIN = _env_float("PLAUSIBLE_MIN", 4.00)
PLAUSIBLE_MAX = _env_float("PLAUSIBLE_MAX", 13.00)

# Timing
CHECK_INTERVAL = _env_float("CHECK_INTERVAL", 10.0)
LOG_INTERVAL   = _env_float("LOG_INTERVAL", 300.0)
# How often to log while below WARN_VOLTAGE. Entering or leaving the warning
# band is always logged immediately, so this only sets the routine cadence.
WARN_LOG_INTERVAL = _env_float("WARN_LOG_INTERVAL", 300.0)
TRIPS_TO_ACT   = _env_int("TRIPS_TO_ACT", 3)
TREND_WINDOW   = _env_float("TREND_WINDOW", 120.0)
TREND_MIN_SPAN = _env_float("TREND_MIN_SPAN", 60.0)
FLOOR_MIN_SPAN = _env_float("FLOOR_MIN_SPAN", 20.0)
RISE_EPSILON   = _env_float("RISE_EPSILON", 0.010)
# This board has no RTC: at boot the clock is restored stale and NTP steps it
# once the network is up. Steps larger than this get flagged in the trace.
CLOCK_STEP_TOLERANCE = _env_float("CLOCK_STEP_TOLERANCE", 60.0)

# Sensor failure handling. "exit" leaves the gateway running WITHOUT battery
# protection and lets systemd restart us (the failure stays visible in
# systemctl status). "shutdown" is the fail-safe alternative for deployments
# where filesystem integrity matters more than uptime.
MAX_READ_ERRORS    = _env_int("MAX_READ_ERRORS", 6)
SENSOR_FAIL_ACTION = _env_str("SENSOR_FAIL_ACTION", "exit").lower()

# Shutdown sequence
STOP_CONTAINERS = _env_bool("STOP_CONTAINERS", True)
COMPOSE_BIN     = _env_str("COMPOSE_BIN", "/usr/local/bin/docker-compose")
COMPOSE_DIR     = _env_str("COMPOSE_DIR", "/var/lib/wazigate/")
COMPOSE_TIMEOUT = _env_float("COMPOSE_TIMEOUT", 45.0)
ESCALATE_AFTER  = _env_float("ESCALATE_AFTER", 30.0)

# Trip action: "standby" (quiesce and wait, recoverable) or "poweroff".
ON_TRIP = _env_str("ON_TRIP", "standby").lower()
# Remount the root filesystem read-only in standby, so the eventual hard cut
# by the HAT protection cannot corrupt anything. Best effort: it fails when
# something still holds a file open for writing.
REMOUNT_RO = _env_bool("REMOUNT_RO", True)
# Leave standby above this voltage. Keep it clearly above SHUTDOWN_VOLTAGE:
# dropping the container load alone makes the pack rebound by ~0.1 V, and that
# must not be mistaken for mains coming back.
RESUME_VOLTAGE      = _env_float("RESUME_VOLTAGE", 6.90)
RESUME_TRIPS        = _env_int("RESUME_TRIPS", 3)
STANDBY_MIN_SECONDS = _env_float("STANDBY_MIN_SECONDS", 120.0)
# "containers" restarts the compose stack in place, "reboot" starts clean.
RESUME_ACTION = _env_str("RESUME_ACTION", "containers").lower()
# Units stopped before quiescing and restarted (in reverse) on resume. These
# are the processes that keep the root filesystem busy. Add any host-side
# application that writes to disk - the monitor names the culprits in the log
# when a read-only remount fails.
# Never allow this service into the list: stopping it during standby would
# leave nothing running to notice mains coming back.
# Listed in STOP order: application, then database, then container runtime.
# Resume starts them in reverse, so the runtime and the database are back
# before the application that needs them.
#
# mongod matters most here. Its WiredTiger journal and lock files are open for
# writing at all times, so a hard cut leaves the database replaying its journal
# on the next start. A clean stop writes a final checkpoint instead.
QUIESCE_UNITS = [_u for _u in _env_str(
    "QUIESCE_UNITS",
    "wazigate.service mongod.service docker.socket docker.service containerd").split()
    if "ups_monitor" not in _u and "ups-monitor" not in _u]
# If the read-only remount fails, freeze the filesystem instead. A freeze
# ignores open handles, so it works where the remount cannot.
FREEZE_FALLBACK = _env_bool("FREEZE_FALLBACK", True)
FSFREEZE_BIN    = _env_str("FSFREEZE_BIN", "fsfreeze")
# At startup, undo a frozen or read-only root left behind by an instance that
# died mid-standby. Without this the gateway stays wedged until someone
# notices, which defeats the whole point of surviving unattended.
STARTUP_RECOVER = _env_bool("STARTUP_RECOVER", True)

# Optional CSV trace. OFF by default: the journal is the log, and writing a
# row per check wears the SD card. Switch it on for a calibration outage,
# where durable machine-readable rows are worth the writes.
TRACE_DIR      = _env_str("TRACE_DIR", "")
DRY_RUN        = _env_bool("DRY_RUN", False)
STARTUP_PROBES = _env_int("STARTUP_PROBES", 5)
# systemd sets JOURNAL_STREAM when our stderr goes to the journal; only then is
# a priority prefix useful (on a terminal it would just be noise).
JOURNAL_PRIORITY = _env_bool("JOURNAL_PRIORITY",
                             bool(os.environ.get("JOURNAL_STREAM")))


# --------------------------------------------------------------------------
# Sensor
# --------------------------------------------------------------------------

class Ina219:
    """Minimal INA219 reader.

    The bus handle is reopened after any error, so a wedged I2C bus can
    recover without restarting the service.
    """

    def __init__(self, bus_no, addr):
        self.bus_no = bus_no
        self.addr = addr
        self._bus = None

    def _open(self):
        if self._bus is None:
            self._bus = smbus2.SMBus(self.bus_no)

    def reset(self):
        if self._bus is not None:
            try:
                self._bus.close()
            except Exception:
                pass
            self._bus = None

    def _read_be(self, reg):
        self._open()
        raw = self._bus.read_word_data(self.addr, reg)
        # SMBus hands back the low byte first; the INA219 sends MSB first.
        return ((raw & 0xFF) << 8) | ((raw >> 8) & 0xFF)

    def voltage(self):
        # Bus voltage register: data in bits 15:3, 4 mV per LSB.
        return (self._read_be(REG_BUS) >> 3) * 0.004

    def shunt_mv(self):
        # Shunt voltage register: signed, 10 uV per LSB. Unlike the current
        # register this needs no calibration write, so it works on a HAT we
        # have not configured. Logged for diagnostics only - the sign depends
        # on how the HAT wires the shunt, which is not verified here.
        raw = self._read_be(REG_SHUNT)
        if raw >= 0x8000:
            raw -= 0x10000
        return raw * 0.01

    def present(self):
        try:
            self._open()
            self._bus.read_byte(self.addr)
            return True
        except Exception:
            self.reset()
            return False


def sample(sensor, reads=3):
    """Median of `reads` range-checked readings, or None if none were usable."""
    good = []
    for k in range(reads):
        try:
            volts = sensor.voltage()
        except Exception as exc:
            LOG.debug("I2C read failed: %s", exc)
            sensor.reset()
        else:
            if PLAUSIBLE_MIN <= volts <= PLAUSIBLE_MAX:
                good.append(volts)
            else:
                LOG.warning("Discarding implausible reading %.3f V "
                            "(expected %.2f-%.2f V)",
                            volts, PLAUSIBLE_MIN, PLAUSIBLE_MAX)
        if k + 1 < reads:
            time.sleep(0.05)
    if not good:
        return None
    good.sort()
    return good[len(good) // 2]


# --------------------------------------------------------------------------
# Durable trace
# --------------------------------------------------------------------------

def boot_uptime():
    """Seconds since boot. Survives the wall-clock steps this board produces,
    so a discharge curve stays reconstructible even when timestamps jump."""
    try:
        with open("/proc/uptime") as handle:
            return float(handle.read().split()[0])
    except Exception:
        return time.monotonic()


def trace(when, voltage, shunt, note=""):
    """Append one flushed and fsynced CSV row, so the last reading before a
    hard power cut survives even when journald loses its tail."""
    if not TRACE_DIR or _quiesce_state != "none":
        return
    try:
        os.makedirs(TRACE_DIR, exist_ok=True)
        stamp = time.localtime(when)
        path = os.path.join(TRACE_DIR,
                            "discharge-%s.csv" % time.strftime("%Y-%m-%d", stamp))
        write_header = not os.path.exists(path)
        with open(path, "a") as handle:
            if write_header:
                handle.write("timestamp,uptime_s,voltage,shunt_mv,note\n")
            handle.write("%s,%.1f,%.3f,%s,%s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S", stamp),
                boot_uptime(),
                voltage,
                "" if shunt is None else "%.2f" % shunt,
                note))
            handle.flush()
            os.fsync(handle.fileno())
    except Exception as exc:
        LOG.debug("Trace write failed: %s", exc)


def sync_disks():
    if _quiesce_state == "frozen":
        return              # a frozen filesystem has nothing left to flush
    if hasattr(os, "sync"):
        try:
            os.sync()
        except Exception as exc:
            LOG.error("sync failed: %s", exc)


def persist_logs():
    """Give journald a moment to write, then flush the page cache, so the
    reason for a shutdown survives a power cut seconds later."""
    for handler in list(logging.getLogger().handlers) + list(LOG.handlers):
        try:
            handler.flush()
        except Exception:
            pass
    time.sleep(0.3)
    sync_disks()


# --------------------------------------------------------------------------
# Shutdown sequence
# --------------------------------------------------------------------------

def run(cmd, timeout, cwd=None, quiet=False):
    """Run a command. Never raises, never blocks longer than `timeout`.

    quiet=True downgrades failure logging to debug, for probes whose failure
    is a normal outcome rather than a problem.
    """
    try:
        proc = subprocess.run(cmd, cwd=cwd, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True)
    except subprocess.TimeoutExpired:
        LOG.error("%s timed out after %.0fs - continuing anyway", cmd[0], timeout)
        return False
    except FileNotFoundError:
        if quiet:
            LOG.debug("%s not found", cmd[0])
        else:
            LOG.error("%s not found - continuing anyway", cmd[0])
        return False
    except Exception as exc:
        LOG.error("%s failed: %s - continuing anyway", cmd[0], exc)
        return False
    if proc.returncode < 0:
        # During a poweroff systemd terminates our whole cgroup, so the child
        # we just asked for that poweroff gets SIGTERM. Expected, not a failure.
        LOG.info("%s terminated by signal %d (expected during poweroff)",
                 cmd[0], -proc.returncode)
        return True
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:400]
        if quiet:
            LOG.debug("%s exited %d: %s", cmd[0], proc.returncode, detail)
        else:
            LOG.error("%s exited %d: %s", cmd[0], proc.returncode, detail)
        return False
    return True


def remount(mode):
    """Best effort remount of the root filesystem (ro makes a hard cut safe)."""
    return run(["/bin/mount", "-o", "remount," + mode, "/"], timeout=20)


def stop_units(units):
    """One at a time on purpose: a unit name that does not exist on this
    system must not stop the others from being stopped."""
    ok = True
    for unit in units:
        LOG.info("Stopping %s", unit)
        if not run(["systemctl", "stop", unit], timeout=90):
            ok = False
    return ok


def start_units(units):
    ok = True
    for unit in units:
        LOG.info("Starting %s", unit)
        if not run(["systemctl", "start", unit], timeout=120):
            ok = False
    return ok


def unit_of(pid):
    """Best effort systemd unit for a pid, from its cgroup path."""
    try:
        with open("/proc/%s/cgroup" % pid) as handle:
            data = handle.read()
    except Exception:
        return ""
    for part in data.replace("/", " ").split():
        if part.endswith(".service") or part.endswith(".scope"):
            return part
    return ""


def open_writers(limit=12):
    """Processes holding a file open for writing on a disk-backed path.

    This is what makes a read-only remount fail, so naming them turns an
    opaque EBUSY into an actionable list for QUIESCE_UNITS.
    """
    skip = ("/proc", "/sys", "/dev", "/run")
    found = []
    try:
        pids = [name for name in os.listdir("/proc") if name.isdigit()]
    except Exception:
        return found
    for pid in pids:
        fddir = "/proc/%s/fd" % pid
        try:
            fds = os.listdir(fddir)
        except Exception:
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fddir, fd))
            except Exception:
                continue
            if not target.startswith("/") or target.startswith(skip):
                continue
            try:
                with open("/proc/%s/fdinfo/%s" % (pid, fd)) as handle:
                    flags = 0
                    for line in handle:
                        if line.startswith("flags:"):
                            flags = int(line.split()[1], 8)
                            break
            except Exception:
                continue
            if flags & 3 == 0:              # O_RDONLY, harmless
                continue
            try:
                with open("/proc/%s/comm" % pid) as handle:
                    comm = handle.read().strip()
            except Exception:
                comm = "?"
            found.append((comm, pid, target, unit_of(pid)))
            if len(found) >= limit:
                return found
    return found


def report_writers():
    writers = open_writers()
    if not writers:
        LOG.warning("No writable handles found, yet the remount still failed.")
        return
    for comm, pid, path, unit in writers:
        LOG.warning("  %s[%s] %s %s", comm, pid, path,
                    "(" + unit + ")" if unit else "")
    units = sorted(set(unit for (_, _, _, unit) in writers if unit))
    if units:
        LOG.warning("Add to QUIESCE_UNITS for a read-only root: %s",
                    " ".join(units))


_shutdown_started = False
_stop_requested = False
_in_standby = False
_standby_since = None
_quiesce_state = "none"      # "none" | "ro" | "frozen"


def interruptible_sleep(deadline):
    """Sleep in short slices so a SIGTERM is acted on within a second, instead
    of holding up a shutdown transaction for a whole check interval."""
    while not _stop_requested:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def shutdown(reason, voltage):
    """Stop the containers, sync, power off. Returns True if a real poweroff
    was initiated (False in DRY_RUN, or if already running)."""
    global _shutdown_started
    if _shutdown_started:
        return False
    _shutdown_started = True

    shown = "unknown" if voltage is None else "%.2f V" % voltage
    LOG.critical("SHUTDOWN TRIGGERED: %s (battery %s)", reason, shown)
    trace(time.time(), voltage if voltage is not None else 0.0, None,
          "shutdown: " + reason)
    persist_logs()

    if DRY_RUN:
        LOG.warning("DRY_RUN is set - would run '%s down' in %s, sync, then "
                    "power off. Continuing to monitor.", COMPOSE_BIN, COMPOSE_DIR)
        return False

    if STOP_CONTAINERS:
        LOG.info("Stopping containers (timeout %.0fs)...", COMPOSE_TIMEOUT)
        if run([COMPOSE_BIN, "down"], timeout=COMPOSE_TIMEOUT, cwd=COMPOSE_DIR):
            LOG.info("Containers stopped.")
        else:
            LOG.warning("Container shutdown did not complete - powering off anyway.")

    LOG.info("Syncing filesystems...")
    sync_disks()
    persist_logs()

    LOG.info("Powering off...")
    for cmd in (["/usr/sbin/shutdown", "-h", "now"],
                ["systemctl", "--no-block", "poweroff"]):
        run(cmd, timeout=20)
        # systemd now stops every unit, this one included. Its SIGTERM is the
        # confirmation that the poweroff is under way, so stop waiting and let
        # the process exit - otherwise we hold up the very shutdown we asked
        # for until TimeoutStopSec expires, burning battery for nothing.
        deadline = time.monotonic() + ESCALATE_AFTER
        while time.monotonic() < deadline:
            if _stop_requested:
                LOG.info("systemd is taking the system down - exiting.")
                return True
            time.sleep(0.5)
        LOG.error("Still running %.0fs after %s - escalating.",
                  ESCALATE_AFTER, " ".join(cmd))

    # Last resort: ask the kernel directly. Filesystems are already synced.
    LOG.critical("Falling back to sysrq poweroff.")
    for path, value in (("/proc/sys/kernel/sysrq", "1"),
                        ("/proc/sysrq-trigger", "o")):
        try:
            with open(path, "w") as handle:
                handle.write(value)
        except Exception as exc:
            LOG.error("Writing %r to %s failed: %s", value, path, exc)
    return True


def root_is_ro():
    try:
        with open("/proc/mounts") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 4 and parts[1] == "/":
                    return "ro" in parts[3].split(",")
    except Exception:
        pass
    return False


def startup_recover():
    """Undo a quiesce that never got undone.

    A crash, OOM kill or SIGKILL during standby leaves the root filesystem
    frozen or read-only. Both wedge the gateway for good, and a restarted
    monitor would have no idea, so clear both unconditionally at startup.
    """
    if FREEZE_FALLBACK:
        # Fails harmlessly with EINVAL when the filesystem is not frozen.
        if run([FSFREEZE_BIN, "-u", "/"], timeout=30, quiet=True):
            LOG.warning("Root filesystem was still frozen from an earlier "
                        "standby - thawed it.")
    if root_is_ro():
        LOG.warning("Root filesystem is mounted read-only at startup, most "
                    "likely a standby that never finished. Remounting rw.")
        if remount("rw"):
            LOG.info("Root filesystem is writable again.")
        else:
            LOG.error("Could not remount rw - this needs manual attention.")


def quiesce():
    """Put the root filesystem where a hard power cut cannot corrupt it.
    Returns "ro", "frozen" or "none"."""
    if REMOUNT_RO:
        if remount("ro"):
            LOG.info("Root filesystem is read-only: the hard cut when the HAT "
                     "protection trips can no longer corrupt it.")
            return "ro"
        LOG.warning("Remount read-only failed - these still hold writable "
                    "handles on the root filesystem:")
        report_writers()

    if FREEZE_FALLBACK:
        LOG.info("Freezing the root filesystem instead (open handles do not "
                 "block a freeze).")
        if run([FSFREEZE_BIN, "-f", "/"], timeout=30):
            LOG.info("Root filesystem frozen: everything pending is flushed "
                     "and further writes block until thaw, so a hard cut is "
                     "safe.")
            return "frozen"
        LOG.error("fsfreeze failed as well.")

    LOG.warning("Root filesystem stays writable - containers are stopped and "
                "synced, but a hard cut could still lose recent writes.")
    return "none"


def unquiesce(state):
    """Undo quiesce(). False means we gave up and asked for a reboot."""
    if state == "frozen":
        if not run([FSFREEZE_BIN, "-u", "/"], timeout=30):
            LOG.error("Thaw failed - rebooting to recover a writable root.")
            run(["/usr/sbin/reboot"], timeout=20)
            return False
        LOG.info("Root filesystem thawed.")
    elif state == "ro":
        if not remount("rw"):
            LOG.error("Remount rw failed - rebooting to recover a writable root.")
            run(["/usr/sbin/reboot"], timeout=20)
            return False
        LOG.info("Root filesystem is writable again.")
    return True


def enter_standby(reason, voltage):
    """Quiesce and keep watching. Never returns True: the process stays alive
    precisely so that it can notice mains coming back."""
    global _in_standby, _standby_since, _quiesce_state
    if _in_standby:
        return False

    shown = "unknown" if voltage is None else "%.2f V" % voltage
    LOG.critical("ENTERING STANDBY: %s (battery %s)", reason, shown)
    LOG.critical("Not powering off on purpose: a halted Pi cannot see mains "
                 "return while this HAT still feeds it, and the gateway would "
                 "stay dark until someone visits it.")
    trace(time.time(), voltage if voltage is not None else 0.0, None,
          "standby: " + reason)
    persist_logs()

    if DRY_RUN:
        LOG.warning("DRY_RUN is set - would stop containers and quiesce now.")
        return False

    if STOP_CONTAINERS:
        LOG.info("Stopping containers (timeout %.0fs)...", COMPOSE_TIMEOUT)
        if run([COMPOSE_BIN, "down"], timeout=COMPOSE_TIMEOUT, cwd=COMPOSE_DIR):
            LOG.info("Containers stopped.")
        else:
            LOG.warning("Container shutdown did not complete - quiescing anyway.")

    # The daemons themselves keep the root filesystem busy, so they have to go
    # too - containers alone are not enough.
    stop_units(QUIESCE_UNITS)

    # Move journald off the root filesystem, otherwise its own open journal
    # blocks the remount and it would keep writing while we are quiesced.
    run(["journalctl", "--relinquish-var"], timeout=30)

    LOG.info("Syncing filesystems...")
    sync_disks()

    _quiesce_state = quiesce()

    _in_standby = True
    _standby_since = time.monotonic()
    LOG.critical("STANDBY active. Resuming above %.2f V; otherwise the HAT cuts "
                 "the rail and the gateway boots when mains returns.",
                 RESUME_VOLTAGE)
    persist_logs()
    return False


def leave_standby(voltage, shunt):
    """Mains is back. Returns True if the process should exit (reboot case)."""
    global _in_standby, _standby_since, _quiesce_state
    LOG.critical("Mains is back (%.2f V, shunt %s mV) - leaving standby.",
                 voltage, "n/a" if shunt is None else "%.2f" % shunt)

    # Only undo what we actually did, and undo it before anything needs to
    # write: rebooting or starting units on a frozen root would just hang.
    if not unquiesce(_quiesce_state):
        return True
    _quiesce_state = "none"

    # journald can have /var back, and the daemons can come up again.
    run(["journalctl", "--flush"], timeout=30)
    start_units(list(reversed(QUIESCE_UNITS)))

    if RESUME_ACTION == "reboot":
        LOG.info("Rebooting for a clean start.")
        run(["/usr/sbin/reboot"], timeout=20)
        return True

    if STOP_CONTAINERS:
        LOG.info("Restarting containers...")
        if run([COMPOSE_BIN, "up", "-d"], timeout=COMPOSE_TIMEOUT,
               cwd=COMPOSE_DIR):
            LOG.info("Containers restarted.")
        else:
            LOG.error("Container restart failed - the stack needs attention.")

    _in_standby = False
    _standby_since = None
    LOG.critical("Normal operation resumed.")
    return False


def act_on_trip(reason, voltage):
    """Dispatch the configured trip action. True means the process should exit."""
    if ON_TRIP == "poweroff":
        return shutdown(reason, voltage)
    return enter_standby(reason, voltage)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def main():
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        line = "%(levelname)s %(message)s"
        handler.setFormatter(JournalFormatter(line) if JOURNAL_PRIORITY
                             else logging.Formatter(line))
        root.addHandler(handler)
    root.setLevel(logging.INFO)

    LOG.info("UPS monitor starting: shutdown<%.2fV floor<%.2fV warn<%.2fV "
             "check=%.0fs trips=%d dry_run=%s",
             SHUTDOWN_VOLTAGE, HARD_FLOOR_VOLTAGE, WARN_VOLTAGE,
             CHECK_INTERVAL, TRIPS_TO_ACT, DRY_RUN)

    # Refuse to run with a threshold that could never fire - that is exactly
    # the bug that made the previous version useless.
    if not PLAUSIBLE_MIN < SHUTDOWN_VOLTAGE < PLAUSIBLE_MAX:
        LOG.critical("SHUTDOWN_VOLTAGE %.2f V lies outside the plausible "
                     "window %.2f-%.2f V and could never trigger. Refusing "
                     "to start.", SHUTDOWN_VOLTAGE, PLAUSIBLE_MIN, PLAUSIBLE_MAX)
        return 1
    if HARD_FLOOR_VOLTAGE >= SHUTDOWN_VOLTAGE:
        LOG.warning("HARD_FLOOR_VOLTAGE (%.2f) is not below SHUTDOWN_VOLTAGE "
                    "(%.2f) - the trend guard will never apply.",
                    HARD_FLOOR_VOLTAGE, SHUTDOWN_VOLTAGE)
    if ON_TRIP not in ("standby", "poweroff"):
        LOG.error("ON_TRIP=%r is not standby or poweroff, using standby.", ON_TRIP)
    if ON_TRIP != "poweroff" and RESUME_VOLTAGE <= SHUTDOWN_VOLTAGE + 0.15:
        LOG.warning("RESUME_VOLTAGE (%.2f) is close to SHUTDOWN_VOLTAGE (%.2f); "
                    "the rebound from stopping the containers may look like "
                    "mains returning and make standby flap.",
                    RESUME_VOLTAGE, SHUTDOWN_VOLTAGE)
    for _unit in QUIESCE_UNITS:
        if "journald" in _unit:
            LOG.warning("QUIESCE_UNITS contains %s, but journald is handled "
                        "with journalctl --relinquish-var; systemd would just "
                        "restart it anyway.", _unit)
    if SENSOR_FAIL_ACTION not in ("exit", "shutdown"):
        LOG.error("SENSOR_FAIL_ACTION=%r is not exit or shutdown, using exit.",
                  SENSOR_FAIL_ACTION)

    if STARTUP_RECOVER:
        startup_recover()

    def on_signal(signum, _frame):
        global _stop_requested
        LOG.info("Signal %d received, stopping.", signum)
        _stop_requested = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, on_signal)
        except Exception:
            pass

    sensor = Ina219(I2C_BUS, I2C_ADDR)
    for attempt in range(1, STARTUP_PROBES + 1):
        if sensor.present():
            break
        LOG.warning("UPS HAT not answering at %s on i2c-%d (%d/%d)",
                    hex(I2C_ADDR), I2C_BUS, attempt, STARTUP_PROBES)
        time.sleep(2.0)
    else:
        LOG.critical("UPS HAT not found at %s - no battery protection. "
                     "Exiting so systemd retries.", hex(I2C_ADDR))
        return 1

    history = deque()           # (monotonic, voltage) inside TREND_WINDOW
    trips = 0
    read_errors = 0
    last_status = -LOG_INTERVAL
    last_wait_msg = -LOG_INTERVAL
    last_charge_msg = -LOG_INTERVAL
    last_floor_msg = -LOG_INTERVAL
    last_wall = None
    last_mono = None
    pending_note = ""
    was_in_warn = False
    resume_trips = 0
    next_tick = time.monotonic()

    while not _stop_requested:
        try:
            now = time.monotonic()

            # Every decision below uses time.monotonic(), so a clock step
            # cannot disturb the trip logic - but it does make the trace
            # timestamps jump, which is worth recording.
            wall = time.time()
            if last_wall is not None:
                drift = (wall - last_wall) - (now - last_mono)
                if abs(drift) > CLOCK_STEP_TOLERANCE:
                    LOG.warning("System clock stepped %+.0fs (NTP sync?); "
                                "trace timestamps before this point are "
                                "unreliable, use uptime_s instead.", drift)
                    pending_note = "clock stepped %+.0fs" % drift
            last_wall, last_mono = wall, now

            voltage = sample(sensor)
            shunt = None
            try:
                shunt = sensor.shunt_mv()
            except Exception:
                pass

            if voltage is None:
                read_errors += 1
                LOG.error("No usable voltage reading (%d/%d)",
                          read_errors, MAX_READ_ERRORS)
                if read_errors >= MAX_READ_ERRORS:
                    if SENSOR_FAIL_ACTION == "shutdown":
                        LOG.critical("Sensor unreadable %d times - shutting "
                                     "down as configured.", read_errors)
                        if shutdown("sensor unreadable %d times" % read_errors,
                                    None):
                            return 0
                    else:
                        LOG.critical("Sensor unreadable %d times - exiting so "
                                     "systemd restarts us. THE GATEWAY IS "
                                     "RUNNING WITHOUT BATTERY PROTECTION.",
                                     read_errors)
                        return 1
            else:
                read_errors = 0
                history.append((now, voltage))
                while history and now - history[0][0] > TREND_WINDOW:
                    history.popleft()

                # Checking every CHECK_INTERVAL is cheap; logging every check
                # is not. Log on state changes and on a slow cadence only.
                shunt_txt = "n/a" if shunt is None else "%.2f" % shunt
                in_warn = voltage <= WARN_VOLTAGE
                due = WARN_LOG_INTERVAL if in_warn else LOG_INTERVAL
                if (in_warn != was_in_warn or pending_note
                        or now - last_status >= due):
                    if in_warn:
                        LOG.warning("Battery %.2f V (shunt %s mV) - below warn "
                                    "level %.2f V", voltage, shunt_txt,
                                    WARN_VOLTAGE)
                    else:
                        LOG.info("Battery %.2f V (shunt %s mV)",
                                 voltage, shunt_txt)
                    trace(time.time(), voltage, shunt,
                          (("warn " if in_warn else "") + pending_note).strip())
                    pending_note = ""
                    last_status = now
                was_in_warn = in_warn

                if _in_standby:
                    # Quiesced. The only question left is whether mains is
                    # back, and that needs a sustained rise well above the
                    # trip point - the rebound from dropping the container
                    # load must not be mistaken for it.
                    dwell = now - (_standby_since or now)
                    span = now - history[0][0]
                    rise = voltage - history[0][1]
                    if (dwell >= STANDBY_MIN_SECONDS
                            and voltage >= RESUME_VOLTAGE
                            and span >= TREND_MIN_SPAN
                            and rise > RISE_EPSILON):
                        resume_trips += 1
                    else:
                        resume_trips = 0
                    if resume_trips >= RESUME_TRIPS:
                        if leave_standby(voltage, shunt):
                            return 0
                        trips = 0
                        resume_trips = 0
                elif voltage < SHUTDOWN_VOLTAGE:
                    trips += 1
                else:
                    trips = 0

                if trips >= TRIPS_TO_ACT and not _in_standby:
                    span = now - history[0][0]
                    rise = voltage - history[0][1]

                    if voltage <= HARD_FLOOR_VOLTAGE and not (
                            span >= FLOOR_MIN_SPAN and rise > 0.0):
                        # At or below the floor and not climbing: act at once,
                        # skipping the full trend confirmation.
                        if act_on_trip("hard floor %.2f V reached (%+.3f V over "
                                    "%.0fs)" % (HARD_FLOOR_VOLTAGE, rise, span),
                                    voltage):
                            return 0
                    elif voltage <= HARD_FLOOR_VOLTAGE:
                        # Below the floor but climbing: a deeply discharged pack
                        # being charged again. Powering off here would strand
                        # the gateway at the very moment mains came back.
                        if now - last_floor_msg >= LOG_INTERVAL:
                            LOG.warning("%.2f V is below the hard floor %.2f V "
                                        "but rising (+%.3f V over %.0fs) - "
                                        "charging, not shutting down.",
                                        voltage, HARD_FLOOR_VOLTAGE, rise, span)
                            last_floor_msg = now
                    elif span < TREND_MIN_SPAN:
                        if now - last_wait_msg >= LOG_INTERVAL:
                            LOG.warning("%.2f V is below %.2f V; need %.0fs "
                                        "more history to tell discharge from "
                                        "charging.", voltage, SHUTDOWN_VOLTAGE,
                                        TREND_MIN_SPAN - span)
                            last_wait_msg = now
                    elif rise > RISE_EPSILON:
                        # Mains is back and the pack is charging. Powering off
                        # here would leave the gateway off until a site visit.
                        if now - last_charge_msg >= LOG_INTERVAL:
                            LOG.warning("%.2f V is below %.2f V but rising "
                                        "(+%.3f V over %.0fs) - mains looks "
                                        "present, not shutting down.",
                                        voltage, SHUTDOWN_VOLTAGE, rise, span)
                            last_charge_msg = now
                    else:
                        if act_on_trip("battery below %.2f V on %d consecutive "
                                    "checks (%+.3f V over %.0fs)"
                                    % (SHUTDOWN_VOLTAGE, trips, rise, span),
                                    voltage):
                            return 0

        except KeyboardInterrupt:
            raise
        except Exception:
            # A bug in the monitor must not silently end battery protection.
            LOG.exception("Unexpected error in monitor loop - continuing")

        next_tick += CHECK_INTERVAL
        if next_tick <= time.monotonic():
            next_tick = time.monotonic()        # fell behind, resync
        interruptible_sleep(next_tick)

    LOG.info("UPS monitor stopped.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
