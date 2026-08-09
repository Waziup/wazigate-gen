#!/bin/bash
LOG_FILE="/tmp/openvpn-dispatcher-starter.log"
touch "$LOG_FILE"

log() {
    echo "$(date '+%F %T') $*" >> "$LOG_FILE"
}

sleep 5
FILE="/etc/NetworkManager/dispatcher.d/vpn-helper"

if [ ! -f "$FILE" ]; then
    log "Error: File '$FILE' does not exist."
    exit 1
fi

log "File exists. VPN helper is installed."

"$FILE" eth0 up &
log "Starting with eth0 interface."
sleep 5
"$FILE" wlan0 up &
log "Starting with wlan0 interface."

wait
