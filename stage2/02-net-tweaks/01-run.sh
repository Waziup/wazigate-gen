#!/bin/bash -e

install -v -d					"${ROOTFS_DIR}/etc/wpa_supplicant"
install -v -m 600 files/wpa_supplicant.conf	"${ROOTFS_DIR}/etc/wpa_supplicant/"

# Previously: raspi-config nonint do_boot_wait 0
# trixie's raspi-config no longer ships do_boot_wait, but it only ever set
# boot_delay=1 in config.txt, so do that directly.
if ! grep -q '^boot_delay=' "${ROOTFS_DIR}/boot/firmware/config.txt"; then
	echo 'boot_delay=1' >> "${ROOTFS_DIR}/boot/firmware/config.txt"
fi

if [ -v WPA_COUNTRY ]; then
	echo "country=${WPA_COUNTRY}" >> "${ROOTFS_DIR}/etc/wpa_supplicant/wpa_supplicant.conf"
fi

if [ -v WPA_ESSID ] && [ -v WPA_PASSWORD ]; then
on_chroot <<EOF
set -o pipefail
wpa_passphrase "${WPA_ESSID}" "${WPA_PASSWORD}" | tee -a "/etc/wpa_supplicant/wpa_supplicant.conf"
EOF
elif [ -v WPA_ESSID ]; then
cat >> "${ROOTFS_DIR}/etc/wpa_supplicant/wpa_supplicant.conf" << EOL

network={
	ssid="${WPA_ESSID}"
	key_mgmt=NONE
}
EOL
fi

# Disable wifi on 5GHz models if WPA_COUNTRY is not set
mkdir -p "${ROOTFS_DIR}/var/lib/systemd/rfkill/"
if [ -n "$WPA_COUNTRY" ]; then
    echo 0 > "${ROOTFS_DIR}/var/lib/systemd/rfkill/platform-3f300000.mmcnr:wlan"
    echo 0 > "${ROOTFS_DIR}/var/lib/systemd/rfkill/platform-fe300000.mmcnr:wlan"
else
    echo 1 > "${ROOTFS_DIR}/var/lib/systemd/rfkill/platform-3f300000.mmcnr:wlan"
    echo 1 > "${ROOTFS_DIR}/var/lib/systemd/rfkill/platform-fe300000.mmcnr:wlan"
fi
