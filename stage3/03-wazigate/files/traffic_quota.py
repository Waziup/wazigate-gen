#!/usr/bin/env python3
"""
traffic_quota.py - monthly traffic budget monitor with email alerts.

Watches one interface against the volume booked with the network provider and
mails a warning at 80% and a critical warning at 98% of the monthly quota.

    traffic_quota.py --setup       interactive configuration, then start
    traffic_quota.py --status      show current usage and projection
    traffic_quota.py --test-mail   send a test mail with the stored settings
    traffic_quota.py               run the monitor (this is what systemd does)

Measurement
-----------
Bytes come from /sys/class/net/<iface>/statistics/{rx,tx}_bytes: kernel
counters, the only figure that matches what the provider bills. Anything
pcap-based (iftop, nethogs) loses traffic and must not be used for a quota.

Those counters restart from zero on reboot and whenever the interface is
recreated (a USB modem re-enumerating, for example), so the monthly total is
accumulated from deltas and kept in a state file rather than read directly.

No RTC on this board
--------------------
The clock is restored stale at boot and only steps to the real time once NTP
syncs. Rolling the billing period over on a wrong date would reset the counter
early and hide an overrun, so a rollover waits for a synchronised clock. A
stale clock always reads earlier than the truth, so the only effect of waiting
is a slightly late rollover, never an early one.
"""

import argparse
import calendar
import configparser
import getpass
import json
import logging
import os
import smtplib
import subprocess
import sys
import time
from datetime import date, timedelta
from email.message import EmailMessage

CONFIG_PATH = os.environ.get("TRAFFIC_QUOTA_CONFIG", "/etc/traffic-quota.conf")
STATE_PATH = os.environ.get("TRAFFIC_QUOTA_STATE",
                            "/var/lib/traffic-quota/state.json")
NET_DIR = os.environ.get("TRAFFIC_QUOTA_NET", "/sys/class/net")
SYNC_FLAG = "/run/systemd/timesync/synchronized"

LOG = logging.getLogger("quota")

_PRIORITY = {logging.CRITICAL: 2, logging.ERROR: 3, logging.WARNING: 4,
             logging.INFO: 6, logging.DEBUG: 7}


class JournalFormatter(logging.Formatter):
    """Prefix a <N> syslog priority so journald records real levels."""

    def format(self, record):
        return "<%d>%s" % (_PRIORITY.get(record.levelno, 6),
                           logging.Formatter.format(self, record))


DEFAULTS = {
    "quota": {"interface": "", "quota_mb": "0", "unit": "MB",
              "billing_day": "1", "count": "both", "interval": "60",
              "persist_interval": "300", "require_clock_sync": "true"},
    "alerts": {"thresholds": "80,98", "critical_at": "98",
               "retry_seconds": "900"},
    "mail": {"to": "", "from": "", "method": "smtp", "host": "", "port": "587",
             "security": "starttls", "user": "", "password": ""},
}


# --------------------------------------------------------------------------
# Configuration and state
# --------------------------------------------------------------------------

def load_config(path=None):
    parser = configparser.ConfigParser()
    for section, values in DEFAULTS.items():
        parser[section] = dict(values)
    read = parser.read(path or CONFIG_PATH)
    return parser, bool(read)


def save_config(parser, path=None):
    target = path or CONFIG_PATH
    directory = os.path.dirname(target)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    tmp = target + ".tmp"
    with open(tmp, "w") as handle:
        parser.write(handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    try:
        os.chmod(target, 0o600)      # it can hold SMTP credentials
    except OSError as exc:
        LOG.warning("Could not restrict permissions on %s: %s", target, exc)


def empty_state():
    return {"period_start": "", "period_end": "", "used_bytes": 0,
            "last_rx": None, "last_tx": None, "alerted": []}


def load_state(path=None):
    try:
        with open(path or STATE_PATH) as handle:
            state = json.load(handle)
    except Exception:
        return empty_state()
    merged = empty_state()
    merged.update({k: v for k, v in state.items() if k in merged})
    return merged


def save_state(state, path=None):
    target = path or STATE_PATH
    try:
        directory = os.path.dirname(target)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        tmp = target + ".tmp"
        with open(tmp, "w") as handle:
            json.dump(state, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        return True
    except Exception as exc:
        LOG.error("Could not write state to %s: %s", target, exc)
        return False


# --------------------------------------------------------------------------
# Counters
# --------------------------------------------------------------------------

def list_interfaces():
    try:
        names = sorted(n for n in os.listdir(NET_DIR) if n != "lo")
    except Exception:
        return []
    return names


def read_counters(iface):
    """(rx_bytes, tx_bytes) from the kernel, or None if the interface is gone."""
    values = []
    for name in ("rx_bytes", "tx_bytes"):
        try:
            with open(os.path.join(NET_DIR, iface, "statistics", name)) as fh:
                values.append(int(fh.read().strip()))
        except Exception:
            return None
    return tuple(values)


def accumulate(state, rx, tx, count="both"):
    """Add the delta since the last sample to the period total.

    A counter that went backwards means the interface was recreated or the
    machine rebooted, so the current value is the delta.
    """
    added = 0
    for key, value in (("last_rx", rx), ("last_tx", tx)):
        if count == "rx" and key == "last_tx":
            state[key] = value
            continue
        if count == "tx" and key == "last_rx":
            state[key] = value
            continue
        previous = state.get(key)
        if previous is None:
            delta = 0                       # first sample, nothing to add yet
        elif value < previous:
            delta = value                   # counter reset
            LOG.info("Counter %s reset (%d -> %d), continuing from zero.",
                     key, previous, value)
        else:
            delta = value - previous
        state[key] = value
        added += delta
    state["used_bytes"] = state.get("used_bytes", 0) + added
    return added


# --------------------------------------------------------------------------
# Billing period
# --------------------------------------------------------------------------

def clamp_day(year, month, day):
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last))


def next_month(year, month):
    return (year + 1, 1) if month == 12 else (year, month + 1)


def previous_month(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


def period_bounds(billing_day, today):
    """First and last day of the billing period containing `today`."""
    start_this = clamp_day(today.year, today.month, billing_day)
    if today >= start_this:
        start = start_this
        year, month = next_month(start.year, start.month)
        end = clamp_day(year, month, billing_day) - timedelta(days=1)
    else:
        year, month = previous_month(today.year, today.month)
        start = clamp_day(year, month, billing_day)
        end = start_this - timedelta(days=1)
    return start, end


def clock_synced():
    return os.path.exists(SYNC_FLAG)


def maybe_rollover(state, billing_day, today, require_sync=True):
    """Start a new period when `today` has left the stored one.

    Returns "rolled", "deferred" or "" (nothing to do).
    """
    start, end = period_bounds(billing_day, today)
    stored = state.get("period_start") or ""
    if stored == start.isoformat():
        return ""
    if stored and require_sync and not clock_synced():
        return "deferred"
    state["period_start"] = start.isoformat()
    state["period_end"] = end.isoformat()
    state["used_bytes"] = 0
    state["alerted"] = []
    return "rolled" if stored else "started"


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------

def quota_bytes(quota_mb, unit):
    factor = 1024 * 1024 if unit.lower() in ("mib", "mebibyte") else 1000 * 1000
    return int(float(quota_mb) * factor)


def human(num_bytes):
    step = 1000.0
    units = ["B", "kB", "MB", "GB", "TB"]
    value = float(num_bytes)
    index = 0
    while value >= step and index < len(units) - 1:
        value /= step
        index += 1
    return "%.2f %s" % (value, units[index])


def due_alerts(state, percent, thresholds):
    already = set(int(t) for t in state.get("alerted", []))
    return [t for t in sorted(thresholds) if percent >= t and t not in already]


def report(state, cfg):
    """Numbers shared by the status output and the alert mails."""
    quota = quota_bytes(cfg.get("quota", "quota_mb"), cfg.get("quota", "unit"))
    used = int(state.get("used_bytes", 0))
    percent = (100.0 * used / quota) if quota > 0 else 0.0
    try:
        start = date.fromisoformat(state["period_start"])
        end = date.fromisoformat(state["period_end"])
    except Exception:
        return {"used": used, "quota": quota, "percent": percent,
                "start": None, "end": None, "elapsed": 0, "remaining": 0,
                "per_day": 0, "projected": used}
    today = date.today()
    total_days = (end - start).days + 1
    elapsed = max(1, min(total_days, (today - start).days + 1))
    remaining = max(0, (end - today).days)
    per_day = used / float(elapsed)
    return {"used": used, "quota": quota, "percent": percent,
            "start": start, "end": end, "elapsed": elapsed,
            "remaining": remaining, "per_day": per_day,
            "projected": int(per_day * total_days)}


def alert_text(iface, info, threshold, critical):
    kind = "KRITISCH" if critical else "HINWEIS"
    lines = [
        "%s: %d%% des gebuchten Datenvolumens sind verbraucht." % (kind, threshold),
        "",
        "Interface:        %s" % iface,
        "Verbraucht:       %s (%.1f%%)" % (human(info["used"]), info["percent"]),
        "Gebucht:          %s" % human(info["quota"]),
        "Abrechnungszeitraum: %s bis %s" % (info["start"], info["end"]),
        "Tage vergangen:   %d" % info["elapsed"],
        "Tage restlich:    %d" % info["remaining"],
        "Schnitt pro Tag:  %s" % human(info["per_day"]),
        "Prognose Periodenende: %s" % human(info["projected"]),
    ]
    if critical:
        lines += ["", "Das Volumen ist fast aufgebraucht. Ohne Eingriff wird "
                      "das Limit vor Ende des Abrechnungszeitraums erreicht."]
    return "\n".join(lines) + "\n"


def send_mail(cfg, subject, body):
    """True if the mail was handed over. Never raises."""
    to = cfg.get("mail", "to")
    if not to:
        LOG.error("No recipient configured - cannot send mail.")
        return False
    sender = cfg.get("mail", "from") or ("traffic-quota@" + os.uname()[1])
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = to
    message.set_content(body)

    method = cfg.get("mail", "method").lower()
    try:
        if method == "sendmail":
            proc = subprocess.run(["/usr/sbin/sendmail", "-t", "-oi"],
                                  input=message.as_bytes(), timeout=60,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
            if proc.returncode != 0:
                LOG.error("sendmail exited %d: %s", proc.returncode,
                          (proc.stderr or b"").decode("utf-8", "replace")[:300])
                return False
        else:
            host = cfg.get("mail", "host")
            port = int(cfg.get("mail", "port"))
            security = cfg.get("mail", "security").lower()
            user = cfg.get("mail", "user")
            password = cfg.get("mail", "password")
            if not host:
                LOG.error("No SMTP host configured - cannot send mail.")
                return False
            if security == "ssl":
                server = smtplib.SMTP_SSL(host, port, timeout=30)
            else:
                server = smtplib.SMTP(host, port, timeout=30)
            try:
                if security == "starttls":
                    server.starttls()
                if user:
                    server.login(user, password)
                server.send_message(message)
            finally:
                try:
                    server.quit()
                except Exception:
                    pass
        LOG.info("Mail sent to %s: %s", to, subject)
        return True
    except Exception as exc:
        # Never log the password; host/user is enough to debug.
        LOG.error("Sending mail failed (%s): %s", method, exc)
        return False


# --------------------------------------------------------------------------
# Monitor
# --------------------------------------------------------------------------

def monitor(cfg, once=False):
    iface = cfg.get("quota", "interface")
    if not iface:
        LOG.critical("No interface configured. Run with --setup first.")
        return 1
    quota = quota_bytes(cfg.get("quota", "quota_mb"), cfg.get("quota", "unit"))
    if quota <= 0:
        LOG.critical("Quota is zero. Run with --setup first.")
        return 1

    billing_day = int(cfg.get("quota", "billing_day"))
    count = cfg.get("quota", "count").lower()
    interval = float(cfg.get("quota", "interval"))
    persist_every = float(cfg.get("quota", "persist_interval"))
    require_sync = cfg.getboolean("quota", "require_clock_sync")
    thresholds = [int(t) for t in cfg.get("alerts", "thresholds").split(",")
                  if t.strip()]
    critical_at = int(cfg.get("alerts", "critical_at"))
    retry_seconds = float(cfg.get("alerts", "retry_seconds"))

    state = load_state()
    LOG.info("Watching %s: quota %s, billing day %d, alerts at %s%%",
             iface, human(quota), billing_day,
             ",".join(str(t) for t in thresholds))

    last_persist = 0.0
    mail_blocked_until = 0.0
    deferred_logged = False
    dirty = False

    while True:
        now = time.monotonic()
        try:
            change = maybe_rollover(state, billing_day, date.today(),
                                    require_sync)
            if change == "deferred":
                if not deferred_logged:
                    LOG.warning("New billing period is due but the clock is "
                                "not NTP-synced yet - waiting, so a stale "
                                "clock cannot reset the counter early.")
                    deferred_logged = True
            elif change:
                deferred_logged = False
                LOG.info("Billing period %s: %s to %s",
                         change, state["period_start"], state["period_end"])
                dirty = True

            counters = read_counters(iface)
            if counters is None:
                LOG.warning("Interface %s is not available right now.", iface)
            else:
                added = accumulate(state, counters[0], counters[1], count)
                if added:
                    dirty = True
                info = report(state, cfg)
                for threshold in due_alerts(state, info["percent"], thresholds):
                    if now < mail_blocked_until:
                        break
                    critical = threshold >= critical_at
                    subject = "%s: %d%% Datenvolumen verbraucht (%s)" % (
                        "KRITISCH" if critical else "HINWEIS", threshold, iface)
                    log = LOG.critical if critical else LOG.warning
                    log("%d%% of the quota used (%s of %s) - alerting %s",
                        threshold, human(info["used"]), human(info["quota"]),
                        cfg.get("mail", "to"))
                    if send_mail(cfg, subject,
                                 alert_text(iface, info, threshold, critical)):
                        state.setdefault("alerted", []).append(threshold)
                        dirty = True
                    else:
                        mail_blocked_until = now + retry_seconds
                        LOG.warning("Will retry the alert in %.0fs.",
                                    retry_seconds)
                        break

            if dirty and now - last_persist >= persist_every:
                if save_state(state):
                    last_persist = now
                    dirty = False
        except Exception:
            LOG.exception("Unexpected error - continuing")

        if once:
            save_state(state)
            return 0
        time.sleep(interval)


# --------------------------------------------------------------------------
# Setup, status
# --------------------------------------------------------------------------

def ask(prompt, default="", validate=None):
    while True:
        shown = " [%s]" % default if default else ""
        answer = input("%s%s: " % (prompt, shown)).strip() or default
        if validate is None:
            return answer
        problem = validate(answer)
        if not problem:
            return answer
        print("  -> %s" % problem)


def setup():
    if os.geteuid() != 0:
        print("Please run the setup as root: sudo traffic_quota.py --setup")
        return 1

    cfg, existed = load_config()
    print("=== traffic-quota setup ===\n")

    interfaces = list_interfaces()
    if not interfaces:
        print("No interfaces found under %s." % NET_DIR)
        return 1
    print("Available interfaces:")
    for index, name in enumerate(interfaces, 1):
        counters = read_counters(name)
        seen = ("rx %s, tx %s" % (human(counters[0]), human(counters[1]))
                if counters else "no counters")
        print("  %d) %-12s %s" % (index, name, seen))
    current = cfg.get("quota", "interface")
    default_choice = (str(interfaces.index(current) + 1)
                      if current in interfaces else "1")

    def valid_choice(value):
        if not value.isdigit() or not 1 <= int(value) <= len(interfaces):
            return "Enter a number between 1 and %d." % len(interfaces)
        return ""

    iface = interfaces[int(ask("\nInterface", default_choice, valid_choice)) - 1]

    def valid_quota(value):
        try:
            return "" if float(value) > 0 else "Must be greater than zero."
        except ValueError:
            return "Enter a number."

    quota_mb = ask("Booked volume in MB", cfg.get("quota", "quota_mb")
                   if cfg.get("quota", "quota_mb") != "0" else "", valid_quota)

    unit = ask("Unit: MB = 1,000,000 bytes (how providers bill), "
               "MiB = 1,048,576", cfg.get("quota", "unit"),
               lambda v: "" if v.lower() in ("mb", "mib") else "MB or MiB.")

    def valid_day(value):
        if not value.isdigit() or not 1 <= int(value) <= 31:
            return "Enter a day between 1 and 31."
        return ""

    billing_day = ask("First day of the billing period (day of month)",
                      cfg.get("quota", "billing_day"), valid_day)
    if int(billing_day) > 28:
        print("  note: in shorter months this clamps to the last day.")

    count = ask("Count which direction: both / rx / tx",
                cfg.get("quota", "count"),
                lambda v: "" if v.lower() in ("both", "rx", "tx")
                else "both, rx or tx.")

    mail_to = ask("Email address for alerts", cfg.get("mail", "to"),
                  lambda v: "" if "@" in v else "Enter an email address.")
    mail_from = ask("Sender address", cfg.get("mail", "from")
                    or "traffic-quota@" + os.uname()[1])

    have_sendmail = os.path.exists("/usr/sbin/sendmail")
    default_method = "sendmail" if have_sendmail else "smtp"
    print("\nMail delivery: 'sendmail' uses the local MTA%s, 'smtp' talks to a "
          "server directly." % ("" if have_sendmail else
                                " (not installed here)"))
    method = ask("Method: smtp / sendmail", default_method,
                 lambda v: "" if v.lower() in ("smtp", "sendmail")
                 else "smtp or sendmail.")

    cfg.set("mail", "method", method.lower())
    if method.lower() == "smtp":
        cfg.set("mail", "host", ask("SMTP host", cfg.get("mail", "host")))
        cfg.set("mail", "security",
                ask("Security: starttls / ssl / none",
                    cfg.get("mail", "security"),
                    lambda v: "" if v.lower() in ("starttls", "ssl", "none")
                    else "starttls, ssl or none."))
        default_port = {"starttls": "587", "ssl": "465", "none": "25"}[
            cfg.get("mail", "security").lower()]
        cfg.set("mail", "port", ask("SMTP port", default_port))
        user = ask("SMTP username (empty for no authentication)",
                   cfg.get("mail", "user"))
        cfg.set("mail", "user", user)
        if user:
            print("  The password is stored in %s, readable by root only.\n"
                  "  It is never logged." % CONFIG_PATH)
            password = getpass.getpass("SMTP password (not echoed): ")
            if password:
                cfg.set("mail", "password", password)

    cfg.set("quota", "interface", iface)
    cfg.set("quota", "quota_mb", quota_mb)
    cfg.set("quota", "unit", unit.lower())
    cfg.set("quota", "billing_day", billing_day)
    cfg.set("quota", "count", count.lower())
    cfg.set("mail", "to", mail_to)
    cfg.set("mail", "from", mail_from)
    save_config(cfg)
    print("\nSaved %s (permissions 0600)." % CONFIG_PATH)

    state = load_state()
    if state.get("period_start") and state.get("used_bytes"):
        keep = ask("Keep the %s already counted in this period? yes/no"
                   % human(state["used_bytes"]), "yes")
        if keep.lower().startswith("n"):
            state = empty_state()
    else:
        state = empty_state()
    # Take a fresh baseline so the first delta is not the whole uptime.
    counters = read_counters(iface)
    if counters:
        state["last_rx"], state["last_tx"] = counters
    maybe_rollover(state, int(billing_day), date.today(), require_sync=False)
    save_state(state)
    print("Billing period: %s to %s"
          % (state["period_start"], state["period_end"]))

    if ask("\nSend a test mail now? yes/no", "yes").lower().startswith("y"):
        info = report(state, cfg)
        if send_mail(cfg, "Test: traffic-quota auf %s" % os.uname()[1],
                     alert_text(iface, info, 0, False)):
            print("Test mail sent to %s." % mail_to)
        else:
            print("Test mail FAILED - see the error above. Fix it before "
                  "relying on the alerts.")

    if ask("Enable and start the service now? yes/no", "yes"
           ).lower().startswith("y"):
        for args in (["systemctl", "daemon-reload"],
                     ["systemctl", "enable", "--now", "traffic-quota"]):
            proc = subprocess.run(args, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
            if proc.returncode != 0:
                print("  %s failed: %s" % (" ".join(args),
                                           proc.stderr.decode("utf-8",
                                                              "replace")))
        print("Monitoring started. Check it with: "
              "journalctl -u traffic-quota -f")
    return 0


def status(cfg):
    state = load_state()
    iface = cfg.get("quota", "interface")
    if not iface:
        print("Not configured yet - run --setup.")
        return 1
    info = report(state, cfg)
    counters = read_counters(iface)
    print("Interface:   %s%s" % (iface, "" if counters else "  (unavailable)"))
    print("Period:      %s to %s" % (info["start"], info["end"]))
    print("Used:        %s of %s  (%.1f%%)"
          % (human(info["used"]), human(info["quota"]), info["percent"]))
    bar = int(round(info["percent"] / 2.5))
    print("             [%s%s]" % ("#" * min(bar, 40),
                                   "." * max(0, 40 - bar)))
    print("Days:        %d elapsed, %d remaining"
          % (info["elapsed"], info["remaining"]))
    print("Average:     %s per day" % human(info["per_day"]))
    print("Projection:  %s by period end (%.0f%% of quota)"
          % (human(info["projected"]),
             100.0 * info["projected"] / info["quota"]
             if info["quota"] else 0))
    already = state.get("alerted", [])
    print("Alerts sent: %s" % (", ".join("%d%%" % a for a in already)
                              if already else "none"))
    if not clock_synced():
        print("Note:        clock is not NTP-synced; a period rollover will "
              "wait for it.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Monthly traffic quota monitor")
    parser.add_argument("--setup", action="store_true",
                        help="interactive configuration, then start the service")
    parser.add_argument("--status", action="store_true",
                        help="show current usage and projection")
    parser.add_argument("--test-mail", action="store_true",
                        help="send a test mail using the stored settings")
    parser.add_argument("--once", action="store_true",
                        help="run a single measurement cycle and exit")
    args = parser.parse_args(argv)

    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        line = "%(levelname)s %(message)s"
        handler.setFormatter(JournalFormatter(line)
                             if os.environ.get("JOURNAL_STREAM")
                             else logging.Formatter(line))
        root.addHandler(handler)
    root.setLevel(logging.INFO)

    if args.setup:
        return setup()

    cfg, existed = load_config()
    if not existed:
        LOG.critical("No configuration at %s - run with --setup first.",
                     CONFIG_PATH)
        return 1

    if args.status:
        return status(cfg)
    if args.test_mail:
        info = report(load_state(), cfg)
        ok = send_mail(cfg, "Test: traffic-quota auf %s" % os.uname()[1],
                       alert_text(cfg.get("quota", "interface"), info, 0, False))
        return 0 if ok else 1
    return monitor(cfg, once=args.once)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
