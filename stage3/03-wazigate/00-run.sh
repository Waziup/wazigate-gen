#!/bin/bash -e

WAZIGATE_DIR=$ROOTFS_DIR/var/lib/wazigate

# libseccomp2 >= 2.5 (the version matching newer kernels/userspace, previously
# pulled from buster-backports) ships with Debian 12+ base, so no backports needed.

# Overwrite mongodb service and conf file on host
# MongoDB 6.0 replaces the packaged unit, so install ours afterwards and keep it
# enabled across upgrades.
install -m 644 files/mongod.service "$ROOTFS_DIR/lib/systemd/system/"
install -m 644 files/mongod.conf "$ROOTFS_DIR/etc/"
on_chroot <<EOF
sudo apt-mark hold mongodb-org mongodb-org-server mongodb-org-shell mongodb-org-mongos mongodb-org-tools
# MongoDB 6.0 needs an empty-by-default log dir; config sends logs to /dev/null
install -d -m 755 -o mongodb -g mongodb /var/log/mongodb
install -d -m 755 -o mongodb -g mongodb /var/lib/mongodb
install -d -m 755 -o mongodb -g mongodb /var/run/mongodb
EOF

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

# Disable all journalctl logs
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

# Install shellinabox dependencies copy executable and service to host
on_chroot <<EOF
apt-get install -y -qq --no-install-recommends libssl-dev libpam0g-dev zlib1g-dev
EOF
install -m 755 files/shellinaboxd "$ROOTFS_DIR/usr/bin/"
install -m 755 files/shellinabox.service "$ROOTFS_DIR/etc/systemd/system/"

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
systemctl enable mongod
systemctl enable wazigate
systemctl enable reconnect_wifi.timer
systemctl enable shellinabox
systemctl enable ups_monitor
systemctl enable vpn-watchdog.timer
systemctl enable wazigate-watchdog.timer
EOF

# Create log file for wazigate-setup
#touch "$ROOTFS_DIR/tmp/wazigate-setup-step.txt"
