#!/bin/bash -e

# The MongoDB apt repository used to be added here. It is gone: mongod no
# longer runs on the host at all (see 01-packages for why), so there is no
# third-party repo, no SHA1-signed key, and one less archive to reach over the
# gateway's metered link at build time.
on_chroot <<CHROOT
apt-get update

# Install pip and the build dependencies
apt-get install -y python3-pip python3-setuptools

# Install smbus2 via pip (the most reliable way to get that specific library)
pip3 install smbus2 || pip3 install --break-system-packages smbus2
CHROOT
