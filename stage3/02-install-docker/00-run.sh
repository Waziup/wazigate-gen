#!/bin/bash -e

# curl -fsSL https://get.docker.com -o $ROOTFS_DIR/tmp/get-docker.sh


# on_chroot <<EOF
# sh /tmp/get-docker.sh
# adduser "$FIRST_USER_NAME" docker
# EOF

on_chroot <<EOF
apt-get update -qq
apt-get install -y -qq --no-install-recommends apt-transport-https ca-certificates curl gnupg 
EOF

lsb_dist="debian"
dist_version="$(sed 's/\/.*//' $ROOTFS_DIR/etc/debian_version | sed 's/\..*//')"
case "$dist_version" in
	11)
		dist_version="bullseye"
	;;
	10)
		dist_version="buster"
	;;
	9)
		dist_version="stretch"
	;;
	8)
		dist_version="jessie"
	;;
esac

CHANNEL="stable"
DOWNLOAD_URL="https://download.docker.com"

echo "Docker: Channel $CHANNEL, Dist $lsb_dist / $dist_version"

curl -fsSL "$DOWNLOAD_URL/linux/$lsb_dist/gpg" | gpg --dearmor --yes -o "$ROOTFS_DIR/usr/share/keyrings/docker-archive-keyring.gpg"

echo \
  "deb [arch=arm64 signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/$lsb_dist \
  $dist_version stable" | sudo tee "$ROOTFS_DIR/etc/apt/sources.list.d/docker.list" > /dev/null


on_chroot <<EOF
apt-get update -qq
apt-get install -y -qq --no-install-recommends docker-ce
adduser "$FIRST_USER_NAME" docker
EOF

mkdir -p "$ROOTFS_DIR/etc/docker"
install -m 644 files/daemon.json "$ROOTFS_DIR/etc/docker/daemon.json"


# Install docker-compose

curl -L "https://github.com/docker/compose/releases/download/v5.0.2/docker-compose-linux-aarch64" -o $ROOTFS_DIR/usr/local/bin/docker-compose

chmod +x "$ROOTFS_DIR/usr/local/bin/docker-compose"
