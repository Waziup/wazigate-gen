#!/bin/bash -e

# add for mongodb source location
# MongoDB 6.0 (jammy) links against OpenSSL 3 / libcurl4, which is what trixie
# provides (libssl3t64 / libcurl4t64 provide the older names). 4.4 could not be
# used because it requires libssl1.1, absent from trixie.
# NOTE: apt-key was removed in Debian 12+, so the key goes into a keyring and is
# referenced with signed-by instead.
# NOTE: the MongoDB key is SHA1-signed; verification depends on the SHA1 policy
# override installed by stage0/00-configure-apt.
on_chroot <<EOF 
wget -qO /usr/share/keyrings/mongodb-server-6.0.asc https://www.mongodb.org/static/pgp/server-6.0.asc

echo \
"deb [ arch=arm64 signed-by=/usr/share/keyrings/mongodb-server-6.0.asc ] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/6.0 multiverse" | tee "/etc/apt/sources.list.d/mongodb-org-6.0.list"


apt-get update

# Install pip and the build dependencies
apt-get install -y python3-pip python3-setuptools

# Install smbus2 via pip (the most reliable way to get that specific library)
pip3 install smbus2 || pip3 install --break-system-packages smbus2
EOF