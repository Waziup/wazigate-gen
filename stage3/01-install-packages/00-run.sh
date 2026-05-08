#!/bin/bash -e

# add for mongodb source location
on_chroot <<EOF 
wget -qO - https://www.mongodb.org/static/pgp/server-4.4.asc | apt-key add -

echo \
"deb [ arch=arm64 ] https://repo.mongodb.org/apt/ubuntu focal/mongodb-org/4.4 multiverse" | tee "/etc/apt/sources.list.d/mongodb-org-4.4.list"


apt-get update

# Install pip and the build dependencies
apt-get install -y python3-pip python3-setuptools

# Install smbus2 via pip (the most reliable way to get that specific library)
pip3 install smbus2 || pip3 install --break-system-packages smbus2
EOF