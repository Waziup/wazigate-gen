#!/bin/bash

sleep 5
FILE="/etc/NetworkManager/dispatcher.d/vpn-helper"

if [ ! -f "$FILE" ]; then
    echo "Error: File '$FILE' does not exist."
    exit 1
fi

echo "File exists. VPN helper is installed."

"$FILE" eth0 up &
sleep 5
"$FILE" wlan0 up &

wait
