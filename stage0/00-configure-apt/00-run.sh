#!/bin/bash -e

install -m 644 files/sources.list "${ROOTFS_DIR}/etc/apt/"
install -m 644 files/raspi.list "${ROOTFS_DIR}/etc/apt/sources.list.d/"

# Debian 13 ships apt with the Sequoia OpenPGP verifier.
#
# Its shipped policy only permitted SHA1 signatures "until 2026", and that
# window has now expired. The Raspberry Pi archive and the MongoDB repository
# are both signed with SHA1, so `apt-get update` fails on them with
# "OpenPGP signature verification failed / is not signed".
#
# This build-time policy override re-enables SHA1 hashing for apt's Sequoia
# verifier so those two third-party repositories can still be used. Signatures
# are still verified - this relaxes the hash algorithm allow-list, it does not
# disable verification.
install -d -m 755 "${ROOTFS_DIR}/etc/crypto-policies/back-ends"
cat > "${ROOTFS_DIR}/etc/crypto-policies/back-ends/apt-sequoia.config" <<'POLICY'
[hash_algorithms]
sha1 = "always"
POLICY
sed -i "s/RELEASE/${RELEASE}/g" "${ROOTFS_DIR}/etc/apt/sources.list"
sed -i "s/RELEASE/${RELEASE}/g" "${ROOTFS_DIR}/etc/apt/sources.list.d/raspi.list"

if [ -n "$APT_PROXY" ]; then
	install -m 644 files/51cache "${ROOTFS_DIR}/etc/apt/apt.conf.d/51cache"
	sed "${ROOTFS_DIR}/etc/apt/apt.conf.d/51cache" -i -e "s|APT_PROXY|${APT_PROXY}|"
else
	rm -f "${ROOTFS_DIR}/etc/apt/apt.conf.d/51cache"
fi

cat files/raspberrypi.gpg.key | gpg --dearmor > "${ROOTFS_DIR}/etc/apt/trusted.gpg.d/raspberrypi-archive-stable.gpg"
on_chroot << EOF
## was armhf, changed to arm64
dpkg --add-architecture arm64
apt-get update
apt-get dist-upgrade -y
EOF
