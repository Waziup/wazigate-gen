#!/bin/bash -e

# add for mongodb source location
on_chroot <<EOF 
wget -qO - https://www.mongodb.org/static/pgp/server-4.4.asc | apt-key add -


echo \
"deb [ arch=arm64 ] https://repo.mongodb.org/apt/ubuntu focal/mongodb-org/4.4 multiverse" | tee "/etc/apt/sources.list.d/mongodb-org-4.4.list"


apt-get update

pip3 install smbus2
EOF

