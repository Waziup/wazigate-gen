#!/bin/bash
IFACE="$1"
STATUS="$2"

LOG_FILE="/tmp/openvpn-dispatcher.log"
VPN_PREFIX="gateway-"

touch "$LOG_FILE"

log() {
	echo "$(date '+%F %T') $1" >> "$LOG_FILE"
}
if [ "$IFACE" = "lo" ]; then
	log "Interface $IFACE is loopback, exiting."
	exit 0
fi
if [ "$STATUS" != "up" ]; then
	log "Connection on $IFACE is not up exiting."
	exit 0
fi

case "$IFACE" in
	docker*|br-*|tun*|veth*|vbox*)
		log "Interface $IFACE is a virtual interface, skipping VPN connection."
		return 0 2>/dev/null || exit 0
		;;
	*)
		log "Interface $IFACE is up. Proceeding with vpn connection..."
		;;
esac

log "Interface $IFACE is up, waiting for NetworkManager..."


for i in {1..30}; do
	NM_STATE=$(nmcli general status | awk 'NR==2 {print $1}')

	if [ "$NM_STATE" = "connected" ]; then
		log "NetworkManager is connected"
		break
	fi

	sleep 1
done

if [ "$NM_STATE" != "connected" ]; then
	log "NetworkManager not ready, exiting"
	exit 0
fi

log "Verifying internet connectivity..."
INTERNET_READY=0
for i in {1..35}; do
	if ping -c 1 -W 1 8.8.8.8 >/dev/null 2>&1; then
		log "Connected to internet with interface $IFACE."
		INTERNET_READY=1
		break
	fi
	log "Not connected yet, waiting... (Attempt $i/35)"
	sleep 1
done

if [ "$INTERNET_READY" -ne 1 ]; then
	log "ERROR: NetworkManager is up but public internet is unreachable. Exiting."
	exit 0
fi

log "Fetching VPN name from API..."

while true; do
	VPN_RAW=$(curl -s --max-time 5 http://localhost/device/id | xargs)

	if [ -n "$VPN_RAW" ]; then
		log "Received Gateway ID: $VPN_RAW"
		break
	fi

	log "API unavailable or returned empty response, retrying in 1 second..."
	sleep 1
done

VPN_NAME="${VPN_PREFIX}${VPN_RAW}"

log "Resolved VPN name: $VPN_NAME"


if ! nmcli connection show | awk '{print $1}' | grep -qx "$VPN_NAME"; then
	log "VPN connection '$VPN_NAME' does not exist"
	exit 0
fi


if nmcli connection show --active | awk '{print $1}' | grep -qx "$VPN_NAME"; then
	log "VPN already active, skipping"
	exit 0
fi


log "Bringing up VPN: $VPN_NAME"

success=0

for i in {1..5}; do
	if nmcli connection up "$VPN_NAME" >> "$LOG_FILE" 2>&1; then
		log "VPN connection succeeded on attempt $i"
		success=1
		break
	fi
	sleep 3
done

if [ "$success" -ne 1 ]; then
	log "VPN connection failed after 5 attempts"
fi
log "VPN start command executed"