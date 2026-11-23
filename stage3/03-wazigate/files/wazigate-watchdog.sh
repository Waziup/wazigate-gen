#!/bin/bash
# WaziGate watchdog, run periodically by wazigate-watchdog.timer.
#
# Detects states the gateway does not recover from by itself and restarts
# wazigate.service, whose start.sh recreates broken containers (and restores
# corrupted images from the image backup). Escalates to a reboot if that does
# not help or the root filesystem has become read-only.
#
# Detected problems:
#   - wazigate.service failed or not running
#   - docker daemon not running
#   - a container of docker-compose.yml is missing
#   - docker could not start a container (.State.Error set, e.g. corrupted
#     overlay after an SD card write error: "lstat .../etc: not a directory")
#   - root filesystem mounted read-only
#
# Containers whose process exits on its own (e.g. the LoRa forwarders without
# a concentrator, exit 2) are NOT treated as a problem: docker restarts them
# via their restart policy, and a gateway restart would not change anything.
#
# Installation (e.g. in the ISO build):
#   install -m 755 wazigate-watchdog.sh      /usr/local/bin/
#   install -m 644 wazigate-watchdog.service /etc/systemd/system/
#   install -m 644 wazigate-watchdog.timer   /etc/systemd/system/
#   systemctl enable wazigate-watchdog.timer
#
# Settings can be overridden in /etc/default/wazigate-watchdog.
# Pause the watchdog for maintenance: systemctl stop wazigate-watchdog.timer
# Logs: journalctl -u wazigate-watchdog

set -u

WAZIGATE_DIR=${WAZIGATE_DIR:-/var/lib/wazigate}
FAIL_THRESHOLD=${FAIL_THRESHOLD:-2}            # consecutive failed checks before acting
MAX_RESTARTS=${MAX_RESTARTS:-3}                # restarts within RESTART_WINDOW before reboot
RESTART_WINDOW=${RESTART_WINDOW:-3600}         # seconds
REBOOT_ENABLED=${REBOOT_ENABLED:-1}
REBOOT_MIN_INTERVAL=${REBOOT_MIN_INTERVAL:-21600}  # min seconds between watchdog reboots
REBOOT_MIN_UPTIME=${REBOOT_MIN_UPTIME:-3600}   # never reboot earlier than this after boot
# Created by ups_monitor.py while it keeps the gateway in standby on battery
UPS_STANDBY_MARKER=${UPS_STANDBY_MARKER:-/run/wazigate-ups-standby}

# Runtime state lives in /run (tmpfs) to avoid SD card writes on every check.
RUN_DIR=${RUN_DIR:-/run/wazigate-watchdog}
FAIL_FILE=$RUN_DIR/failcount
RESTART_FILE=$RUN_DIR/restarts
# Only written right before a watchdog reboot, must survive the reboot.
LAST_REBOOT_FILE=$WAZIGATE_DIR/watchdog-last-reboot

mkdir -p "$RUN_DIR"
now=$(date +%s)

log () {
  echo "$*"
}

################################################################################
# Skip while the gateway is intentionally disabled, starting or being updated

if [ "$(systemctl is-enabled wazigate 2>/dev/null)" != "enabled" ]; then
  exit 0
fi

# The UPS monitor stopped the stack on purpose (power outage: services stopped,
# root read-only or frozen). Restarting or rebooting now would defeat that.
if [ -e "$UPS_STANDBY_MARKER" ]; then
  exit 0
fi

if pgrep -f 'update_containers\.sh|/update\.sh|docker (load|save|pull)|docker-compose (pull|down)' >/dev/null \
   || pgrep -x 'dpkg|apt|apt-get' >/dev/null; then
  exit 0
fi

wazigate_state=$(systemctl is-active wazigate 2>/dev/null)
case "$wazigate_state" in
  activating|deactivating|reloading)
    # start.sh is running (incl. its own container recovery), check again later
    exit 0
    ;;
esac

################################################################################
# Checks

problems=()

rootfs_ro=0
if findmnt -n -o OPTIONS / | tr ',' '\n' | grep -qx ro; then
  rootfs_ro=1
  problems+=("root filesystem is mounted read-only")
fi

docker_ok=1
if ! systemctl is-active --quiet docker; then
  docker_ok=0
  problems+=("docker daemon is not running")
fi

if [ "$wazigate_state" != "active" ]; then
  problems+=("wazigate.service is $wazigate_state")
fi

if [ "$docker_ok" == "1" ] && cd "$WAZIGATE_DIR"; then
  services=$(timeout 60 docker-compose config --services 2>/dev/null)
  ids=$(timeout 60 docker-compose ps -a -q 2>/dev/null)
  existing=""
  if [ -n "$ids" ]; then
    while IFS='|' read -r service name status err; do
      existing+="$service"$'\n'
      if [ "$status" != "running" ] && [ -n "$err" ]; then
        problems+=("container ${name#/} could not be started: $err")
      fi
    done < <(timeout 60 docker inspect -f \
      '{{index .Config.Labels "com.docker.compose.service"}}|{{.Name}}|{{.State.Status}}|{{.State.Error}}' $ids)
  fi
  for service in $services; do
    if ! grep -qx "$service" <<<"$existing"; then
      problems+=("container of service $service is missing")
    fi
  done
fi

################################################################################
# Evaluate

if [ ${#problems[@]} -eq 0 ]; then
  rm -f "$FAIL_FILE"
  exit 0
fi

fails=$(( $(cat "$FAIL_FILE" 2>/dev/null || echo 0) + 1 ))
echo "$fails" > "$FAIL_FILE"

for p in "${problems[@]}"; do
  log "Problem: $p"
done

if [ "$fails" -lt "$FAIL_THRESHOLD" ]; then
  log "Check failed ($fails/$FAIL_THRESHOLD), waiting for the next check before acting."
  exit 0
fi

################################################################################
# Actions

reboot_gateway () {
  if [ "$REBOOT_ENABLED" != "1" ]; then
    log "Reboot is disabled (REBOOT_ENABLED=0), manual intervention needed."
    return
  fi
  local uptime last
  uptime=$(cut -d. -f1 /proc/uptime)
  if [ "$uptime" -lt "$REBOOT_MIN_UPTIME" ]; then
    log "Uptime ${uptime}s is below ${REBOOT_MIN_UPTIME}s, not rebooting yet."
    return
  fi
  last=$(cat "$LAST_REBOOT_FILE" 2>/dev/null || echo 0)
  if [ $((now - last)) -lt "$REBOOT_MIN_INTERVAL" ]; then
    log "Last watchdog reboot was $(( (now - last) / 60 )) min ago, not rebooting again. Manual intervention needed."
    return
  fi
  # May fail on a read-only filesystem, REBOOT_MIN_UPTIME still limits reboot loops then
  echo "$now" > "$LAST_REBOOT_FILE" || log "Could not record reboot time."
  log "Rebooting the gateway."
  sync
  systemctl reboot
}

if [ "$rootfs_ro" == "1" ]; then
  log "Restarting services cannot fix a read-only root filesystem, escalating to reboot (fsck runs on boot)."
  reboot_gateway
  exit 0
fi

recent=$(awk -v t=$((now - RESTART_WINDOW)) '$1 > t' "$RESTART_FILE" 2>/dev/null)
count=$(grep -c . <<<"$recent")
if [ "$count" -ge "$MAX_RESTARTS" ]; then
  log "Already $count restarts within the last $((RESTART_WINDOW / 60)) min without success, escalating to reboot."
  reboot_gateway
  exit 0
fi

{ [ -n "$recent" ] && echo "$recent"; echo "$now"; } > "$RESTART_FILE"
rm -f "$FAIL_FILE"

if [ "$docker_ok" == "0" ]; then
  log "Restarting docker ..."
  systemctl restart docker || log "Restarting docker failed."
fi

log "Restarting wazigate.service (restart $((count + 1))/$MAX_RESTARTS), start.sh recreates broken containers ..."
systemctl restart --no-block wazigate
