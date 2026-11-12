#!/bin/bash
IFACE="$1"
STATUS="$2"

case "$IFACE" in
    lo|docker*|br-*|tun*|veth*|vbox*)
        exit 0
        ;;
esac

if [ "$STATUS" != "up" ] && [ "$STATUS" != "down" ] && [ "$STATUS" != "connectivity-change" ]; then
    exit 0
fi

sleep 2
exec /usr/local/bin/vpn-watchdog.sh "$IFACE" "$STATUS"