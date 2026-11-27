#!/bin/bash -e

WAZIGATE_DIR=$ROOTFS_DIR/var/lib/wazigate

# libseccomp2 >= 2.5 (the version matching newer kernels/userspace, previously
# pulled from buster-backports) ships with Debian 12+ base, so no backports needed.

# MongoDB is a container now, not a host service -- see
# stage3/01-install-packages/01-packages for the ARMv8.2-A / libssl1.1 reason.
# Only the data directory is prepared here: the compose service bind-mounts
# /var/lib/mongodb to /data/db, so a gateway that is updated rather than
# reflashed keeps its database, and the official image's entrypoint chowns it
# to its own mongodb user on first start.
install -d -m 755 "$ROOTFS_DIR/var/lib/mongodb"


# Overwrite redis.conf file on host 
install -m 644 files/redis.conf "$ROOTFS_DIR/etc/redis/"
# Make folder for socket file and working dir, change owner and group, on_chroot: because no user redis
on_chroot <<EOF
install -d -m 644 -o redis -g redis "/var/run/redis/"
install -d -m 755 -o redis -g redis "/var/lib/redis/"
chown redis:redis /var/run/redis
EOF
# Replace redis.service file
install -m 644 files/redis-server.service "$ROOTFS_DIR/lib/systemd/system/"

# Install Network Manager config
install -m 644 files/NetworkManager.conf "$ROOTFS_DIR/etc/NetworkManager/"

# Change the kernel virtual memory accounting mode to: always overcommit, never check
echo "vm.overcommit_memory = 1" >> $ROOTFS_DIR/etc/sysctl.conf

# Reduce total amounts of writes

# Disable swap file
on_chroot <<EOF
dphys-swapfile swapoff 
dphys-swapfile uninstall
update-rc.d dphys-swapfile remove
systemctl disable dphys-swapfile
EOF

# Rsyslog: drop all messages, journald keeps them (avoids duplicate writes)
install -m 644 files/rsyslog.conf "$ROOTFS_DIR/etc/"


# Install rsync, alternative to cp, recommended by log2ram
on_chroot <<EOF
apt-get install -y -qq --no-install-recommends rsync
EOF

#Install Log2RAM and copy configuration
wget https://github.com/azlux/log2ram/archive/master.tar.gz -O "$ROOTFS_DIR/home/$FIRST_USER_NAME/log2ram.tar.gz"
tar -xf "$ROOTFS_DIR/home/$FIRST_USER_NAME/log2ram.tar.gz" -C "$ROOTFS_DIR/home/$FIRST_USER_NAME/"
chmod +x "$ROOTFS_DIR/home/$FIRST_USER_NAME/log2ram-master/install.sh"
on_chroot <<EOF
cd /home/$FIRST_USER_NAME/log2ram-master/
install -m 644 log2ram.service /etc/systemd/system/log2ram.service
install -m 644 log2ram-daily.service /etc/systemd/system/log2ram-daily.service
install -m 644 log2ram-daily.timer /etc/systemd/system/log2ram-daily.timer
install -m 755 log2ram /usr/local/bin/log2ram
if [ ! -f /etc/log2ram.conf ]; then
    install -m 644 log2ram.conf /etc/log2ram.conf
fi
install -m 644 uninstall.sh /usr/local/bin/uninstall-log2ram.sh
systemctl enable log2ram.service log2ram-daily.timer

# logrotate
if [ -d /etc/logrotate.d ]; then
    install -m 644 log2ram.logrotate /etc/logrotate.d/log2ram
else
    echo "##### Directory /etc/logrotate.d does not exist. #####"
    echo "#####  Skipping log2ram.logrotate installation.  #####"
fi
EOF
rm -f "$ROOTFS_DIR/home/$FIRST_USER_NAME/log2ram.tar.gz"
rm -rf "$ROOTFS_DIR/home/$FIRST_USER_NAME/log2ram-master"

# echo "deb [signed-by=/usr/share/keyrings/azlux-archive-keyring.gpg] http://packages.azlux.fr/debian/ bullseye main" | sudo tee "$ROOTFS_DIR/etc/apt/sources.list.d/azlux.list"
# sudo wget -O "$ROOTFS_DIR/usr/share/keyrings/azlux-archive-keyring.gpg"  https://azlux.fr/repo.gpg
# on_chroot <<EOF
# apt-get update
# apt-get install -y -qq --no-install-recommends log2ram
# EOF
install -m 644 files/log2ram.conf "$ROOTFS_DIR/etc/"

# Show text-ui on login
echo -e "# Add wazi-config on startup:\nsudo wazi-config" >> "$ROOTFS_DIR/home/$FIRST_USER_NAME/.profile"

# shellinaboxd is built from source with --disable-ssl (tools/build-shellinaboxd.sh),
# so it links no OpenSSL at all. The 2023 binary linked libssl1.1 and could not
# start on trixie. These are the runtime libraries it does link -- the -dev
# packages that used to be installed here were never needed on a gateway.
on_chroot <<EOF
apt-get install -y -qq --no-install-recommends libpam0g zlib1g
EOF
install -m 755 files/shellinaboxd "$ROOTFS_DIR/usr/bin/"
install -m 755 files/shellinabox.service "$ROOTFS_DIR/etc/systemd/system/"

# The binary is committed to this repo, so a stale one would sail through the
# build and only fail on the gateway, as the 2023 libssl1.1 build did. It is
# built with --disable-ssl, so any reference to libssl means the committed
# binary is not the one tools/build-shellinaboxd.sh produces.
if grep -qa 'libssl\.so' "$ROOTFS_DIR/usr/bin/shellinaboxd"; then
	echo "ERROR: files/shellinaboxd still references libssl. The committed binary" >&2
	echo "is the old OpenSSL-linked build and shellinaboxd will not start on" >&2
	echo "trixie. Rebuild it with tools/build-shellinaboxd.sh and commit it." >&2
	exit 1
fi

# Install Network Time Protocol (NTP) to sync time during runtime
# Classic 'ntp' was removed from Debian; 'ntpsec' is its maintained successor
# and also ships ntp.service / ntp.conf, so downstream config stays unchanged.
on_chroot <<EOF
apt-get install -y -qq --no-install-recommends ntpsec
systemctl enable ntpsec
EOF

# Install builded qjs, because apt and deb where not possible because of conflicts with libc6 and locales(needed)
install -m 755 files/qjs "$ROOTFS_DIR/usr/bin/"

# Copy reconnect_wifi shell script to host 
install -m 755 files/reconnect_wifi.sh "$ROOTFS_DIR/usr/bin/reconnect_wifi"
install -m 755 files/reconnect_wifi.service "$ROOTFS_DIR/etc/systemd/system/reconnect_wifi.service"
install -m 755 files/reconnect_wifi.timer "$ROOTFS_DIR/etc/systemd/system/reconnect_wifi.timer"
touch "$ROOTFS_DIR/etc/do_not_reconnect_wifi"

# Copy openvpn boot service script to host 
install -m 755 files/vpn-helper.sh "$ROOTFS_DIR/etc/NetworkManager/dispatcher.d/vpn-helper"
install -m 755 files/openvpn-fallback-boot.sh "$ROOTFS_DIR/usr/bin/openvpn-fallback-boot"
install -m 755 files/vpn-watchdog.sh "$ROOTFS_DIR/usr/local/bin/vpn-watchdog.sh"
install -m 755 files/vpn-watchdog.service "$ROOTFS_DIR/etc/systemd/system/vpn-watchdog.service"
install -m 755 files/vpn-watchdog.timer "$ROOTFS_DIR/etc/systemd/system/vpn-watchdog.timer"

# Copy script for Waveshare UPS HAT
install -m 755 files/ups_monitor.py "$ROOTFS_DIR/usr/local/bin/ups_monitor.py"
install -m 755 files/ups_monitor.service "$ROOTFS_DIR/etc/systemd/system/ups_monitor.service"

# WaziGate watchdog: restarts wazigate.service (start.sh repairs broken
# containers) and reboots as last resort. Pauses while ups_monitor is in
# standby (/run/wazigate-ups-standby).
install -m 755 files/wazigate-watchdog.sh "$ROOTFS_DIR/usr/local/bin/wazigate-watchdog.sh"
install -m 644 files/wazigate-watchdog.service "$ROOTFS_DIR/etc/systemd/system/wazigate-watchdog.service"
install -m 644 files/wazigate-watchdog.timer "$ROOTFS_DIR/etc/systemd/system/wazigate-watchdog.timer"

# Global network prioritization (WLAN over GSM) & IPv4 preference
echo "=== Configuring global network prioritization ==="

# Ensure NetworkManager drop-in configuration directory exists in the rootfs
mkdir -p "$ROOTFS_DIR/etc/NetworkManager/conf.d"

# Copy pre-configured metric configuration files from the files/ directory
install -m 644 files/90-wlan-metric.conf "$ROOTFS_DIR/etc/NetworkManager/conf.d/"
install -m 644 files/91-gsm-metric.conf  "$ROOTFS_DIR/etc/NetworkManager/conf.d/"
install -m 755 files/20-connectivity.conf  "$ROOTFS_DIR/etc/NetworkManager/conf.d/"

# Copy system-wide address sorting configuration to prioritize IPv4 over IPv6
install -m 644 files/gai.conf "$ROOTFS_DIR/etc/"

# Copy script for traffic surveillance service (deactivated by default)
install -m 755 files/traffic_quota.py "$ROOTFS_DIR/usr/local/bin/"
install -m 644 files/traffic-quota.service "$ROOTFS_DIR/etc/systemd/system/"

# Enable Wazigate services
on_chroot <<EOF
systemctl enable NetworkManager-dispatcher.service
systemctl enable wazigate
systemctl enable reconnect_wifi.timer
systemctl enable shellinabox
systemctl enable ups_monitor
systemctl enable vpn-watchdog.timer
systemctl enable wazigate-watchdog.timer
EOF

# Create log file for wazigate-setup
#touch "$ROOTFS_DIR/tmp/wazigate-setup-step.txt"

# Undo the update-initramfs diversion from stage0/00-configure-apt and build
# the initramfs exactly once, now that every kernel and module package is in
# place. This is the last stage before export-image.
on_chroot << EOF
rm -f /usr/sbin/update-initramfs
dpkg-divert --local --rename --remove /usr/sbin/update-initramfs
update-initramfs -c -k all 2>/dev/null || update-initramfs -u -k all
EOF

# An image without an initramfs does not boot, and the suppression above would
# hide that until someone flashed a card. Fail the build here instead.
if [ ! -e "$ROOTFS_DIR/boot/firmware/initramfs8" ]; then
	echo "ERROR: /boot/firmware/initramfs8 was not generated - the image would not boot." >&2
	echo "The update-initramfs diversion in stage0/00-configure-apt was not undone correctly." >&2
	exit 1
fi

# Boot sanity. The first-boot resize no longer hangs off cmdline's init=:
# raspberrypi-sys-mods dropped /usr/lib/raspberrypi-sys-mods/firstboot and moved
# the work into the initramfs, as
#   /usr/share/initramfs-tools/hooks/firstboot
#   /usr/share/initramfs-tools/scripts/local-premount/resize_early
# resize_early grows partition 2 before root is mounted; resize2fs_once (enabled
# in stage2/01-sys-tweaks/01-run.sh) then grows the filesystem into it. None of
# that runs unless the firmware loads the initramfs, and the firmware only does
# that when config.txt says so -- stage1/00-boot-files overwrites whatever
# raspi-firmware wrote, so the directive must be in our own config.txt.
CMDLINE="$ROOTFS_DIR/boot/firmware/cmdline.txt"
CONFIG="$ROOTFS_DIR/boot/firmware/config.txt"

if grep -q 'init=' "$CMDLINE"; then
	echo "ERROR: cmdline.txt still sets init=, but raspberrypi-sys-mods no longer" >&2
	echo "ships a firstboot binary -- the Pi would panic. Remove init= from" >&2
	echo "stage2/01-sys-tweaks/00-patches/07-resize-init.diff." >&2
	exit 1
fi

# resize_early bails out unless the cmdline carries a " resize" token; without
# it the card is never expanded. resize2fs_once strips the token on first boot.
if ! grep -qE '(^| )resize( |$)' "$CMDLINE"; then
	echo "ERROR: cmdline.txt has no 'resize' token, so the initramfs premount" >&2
	echo "script resize_early exits immediately and the card is never expanded." >&2
	echo "Add it in stage2/01-sys-tweaks/00-patches/07-resize-init.diff." >&2
	exit 1
fi

if ! grep -qE '^[[:space:]]*(auto_initramfs=1|initramfs[[:space:]]+)' "$CONFIG"; then
	echo "ERROR: config.txt does not tell the firmware to load an initramfs." >&2
	echo "Without it resize_early never runs and root stays at the image size on" >&2
	echo "every card. Add auto_initramfs=1 to stage1/00-boot-files/files/config.txt." >&2
	exit 1
fi

RESIZE_EARLY="/usr/share/initramfs-tools/scripts/local-premount/resize_early"
if [ ! -e "$ROOTFS_DIR$RESIZE_EARLY" ]; then
	echo "ERROR: $RESIZE_EARLY is missing from the image." >&2
	echo "The card would never be expanded. raspberrypi-sys-mods has moved it" >&2
	echo "again; these are the resize-related files it and raspi-config ship:" >&2
	grep -hiE 'firstboot|resize|expand' \
		"$ROOTFS_DIR/var/lib/dpkg/info/raspberrypi-sys-mods.list" \
		"$ROOTFS_DIR"/var/lib/dpkg/info/raspi-config*.list 2>/dev/null \
		|| echo "(no matching files in either package)" >&2
	exit 1
fi
