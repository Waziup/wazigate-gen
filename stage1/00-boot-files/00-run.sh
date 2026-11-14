#!/bin/bash -e

# bookworm+ layout: boot config lives on the boot partition at /boot/firmware
install -d "${ROOTFS_DIR}/boot/firmware"
install -m 644 files/cmdline.txt "${ROOTFS_DIR}/boot/firmware/"
install -m 644 files/config.txt "${ROOTFS_DIR}/boot/firmware/"
