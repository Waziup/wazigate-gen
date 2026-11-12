#!/bin/bash
TRIGGER="${1:-manual}"
STATUS="${2:-check}"

LOCK_FILE="/run/lock/openvpn-dispatcher.lock"
VPN_PREFIX="gateway-"
LOG_TAG="vpn-watchdog"

log() {
    logger -t "$LOG_TAG" -p daemon.info "[$TRIGGER:$STATUS] $1"
}

# Queue incoming events up to 60s so simultaneous triggers never collide
exec 200>"$LOCK_FILE"
if ! flock -w 60 200; then
    log "Timed out waiting for lock, exiting."
    exit 0
fi

get_active_vpn() {
    nmcli -t -f NAME,TYPE connection show --active | awk -F: '$2=="vpn" || $2=="wireguard" {print $1}' | grep "^${VPN_PREFIX}" | head -n 1
}

vpn_tunnel_healthy() {
    ip link show tun0 >/dev/null 2>&1 && ping -I tun0 -c 1 -W 3 8.8.8.8 >/dev/null 2>&1
}

get_online_interface() {
    local default_dev
    default_dev=$(ip route get 8.8.8.8 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}')
    if [ -n "$default_dev" ] && [ "$default_dev" != "tun0" ]; then
        if ping -I "$default_dev" -c 1 -W 2 8.8.8.8 >/dev/null 2>&1; then
            echo "$default_dev"
            return 0
        fi
    fi

    for dev in $(nmcli -t -f DEVICE,TYPE,STATE device | awk -F: '$3=="connected" && ($2=="ethernet" || $2=="wifi" || $2=="gsm") {print $1}'); do
        if ping -I "$dev" -c 1 -W 2 8.8.8.8 >/dev/null 2>&1; then
            echo "$dev"
            return 0
        fi
    done

    return 1
}

resolve_vpn_name() {
    local raw=""
    local attempt=0
    while true; do
        raw=$(curl -s --max-time 5 http://localhost/device/id | xargs)
        if [ -n "$raw" ]; then
            log "Received Gateway ID: $raw"
            echo "${VPN_PREFIX}${raw}"
            return 0
        fi
        attempt=$((attempt + 1))
        if [ "$attempt" -eq 1 ] || [ $((attempt % 15)) -eq 0 ]; then
            log "Local API not ready yet, retrying every 1s (attempt $attempt)..."
        fi
        sleep 1
    done
}

connect_or_restart_vpn() {
    local vpn_name="$1"
    local force_restart="$2"

    if ! nmcli -t -f NAME connection show | grep -qx "$vpn_name"; then
        log "ERROR: VPN profile '$vpn_name' does not exist."
        return 1
    fi

    if [ "$force_restart" = "true" ]; then
        log "Restarting VPN '$vpn_name' to bind to active internet interface..."
        nmcli connection down "$vpn_name" >/dev/null 2>&1
        sleep 2
    fi

    log "Bringing up VPN: $vpn_name"
    for i in {1..5}; do
        local current_uplink
        current_uplink=$(get_online_interface)
        if [ -z "$current_uplink" ]; then
            log "Attempt $i: No online interface available right now, waiting 3s..."
            sleep 3
            continue
        fi

        log "Attempt $i: Connecting VPN via available uplink '$current_uplink'..."
        local nm_out
        if nm_out=$(nmcli connection up "$vpn_name" 2>&1); then
            log "VPN connection succeeded on attempt $i via '$current_uplink' ($nm_out)."
            return 0
        else
            log "Attempt $i failed: $nm_out"
        fi
        sleep 3
    done

    log "ERROR: VPN connection failed after 5 attempts."
    return 1
}

ONLINE_IFACE=$(get_online_interface)
if [ -z "$ONLINE_IFACE" ]; then
    [ "$TRIGGER" != "systemd-timer" ] && log "No interface currently has internet access."
    exit 0
fi

ACTIVE_VPN=$(get_active_vpn)

if [ -n "$ACTIVE_VPN" ]; then
    if vpn_tunnel_healthy; then
        [ "$TRIGGER" != "systemd-timer" ] && log "VPN '$ACTIVE_VPN' is healthy (uplink: $ONLINE_IFACE)."
        exit 0
    else
        log "Internet is available on '$ONLINE_IFACE', but VPN '$ACTIVE_VPN' is dead. Reconnecting..."
        connect_or_restart_vpn "$ACTIVE_VPN" "true"
        exit 0
    fi
fi

log "Internet confirmed on '$ONLINE_IFACE' and no VPN is active. Fetching VPN name..."
VPN_NAME=$(resolve_vpn_name)
connect_or_restart_vpn "$VPN_NAME" "false"