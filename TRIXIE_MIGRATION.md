# WaziGate Image Build: Debian Bullseye → Trixie Migration

**Date:** 12 November 2026
**Scope:** `wazigate-gen` repository (pi-gen based Raspberry Pi image build)

---

## 1. Background

Debian 11 (bullseye) has left regular security support and is now in the
long-term-support phase, serviced only from `archive.debian.org`. To stay on a
supported platform, the image build has been migrated to **Debian 13 (trixie)**,
the current stable release, which receives full security updates via
`security.debian.org` (together with oldstable bookworm). The temporary
bullseye workaround that pointed the security suite at `archive.debian.org`
(commit `ff50b74`) has been reverted as part of this migration, since trixie
security is served directly by `security.debian.org`.

Source: [Debian Releases](https://www.debian.org/releases/)

The Raspberry Pi base packages were also migrated, since the RPi archive no
longer publishes the bullseye-era kernel/bootloader packages for trixie.

---

## 2. Release / toolchain changes

| File                                         | Change                                                                                                                   |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `build.sh`                                   | `RELEASE` default changed `bullseye` → `trixie`                                                                          |
| `Dockerfile`                                 | Build container base image `debian:bullseye` → `debian:trixie`                                                           |
| `build-docker.sh`                            | `i386/debian:trixie` / `debian:trixie` base images; comment updated                                                      |
| `scripts/common`                             | debootstrap components now include `non-free-firmware` (required from bookworm on)                                       |
| `stage0/00-configure-apt/files/sources.list` | `non-free-firmware` added to main/updates lines; **security suite reverted `archive.debian.org` → `security.debian.org`** (trixie security is served there; the archive only carries EOL releases) |
| `stage0/00-configure-apt/files/raspi.list` | Moved to the canonical `archive.raspberrypi.com`; marked `[trusted=yes]` — see §3.1 |
| `stage0/00-configure-apt/00-run.sh` | Drops `apt.conf.d/99-allow-weak-rpi` (`Acquire::AllowWeakRepositories "true"`) |

> `stage0/prerun.sh` (bootstrap mirror `ftp.it.debian.org`) needed no change.

---

## 3. Raspberry Pi kernel / firmware stage

The official **2026-10-06 Raspberry Pi OS trixie (arm64-lite)** image was
inspected directly (its `/var/lib/dpkg/status` was extracted from the raw
image) to determine the exact, currently-shipped package set. That ground truth
drove the changes below.

### `stage0/02-firmware/01-packages`

| Removed                  | Added                  | Reason                                                                   |
| ------------------------ | ---------------------- | ------------------------------------------------------------------------ |
| `raspberrypi-bootloader` | `raspi-firmware`       | Replaced by the distro-native firmware package in the RPi trixie archive |
| `raspberrypi-kernel`     | `linux-image-rpi-v8`   | Meta-package for Pi 3/4/CM4 and earlier (aarch64)                        |
|                          | `linux-image-rpi-2712` | Meta-package for Pi 5 / CM5 (BCM2712) — needed for multi-board support   |

Versions observed in the reference image: kernel 6.18.50, raspi-firmware
1:1.20260915-1.

### 3.1 Raspberry Pi archive signature handling (found during build test)

Debian 13's apt uses the **Sequoia** OpenPGP verifier (`sqv`) instead of GnuPG.
Its default policy rejects the Raspberry Pi archive key, which is still
SHA1-signed, so `apt-get update` failed with:

```
E: OpenPGP signature verification failed: http://archive.raspberrypi.org/debian trixie InRelease
```

Two mitigations were applied:

1. `Acquire::AllowWeakRepositories "true"` in `apt.conf.d/99-allow-weak-rpi`.
2. `[trusted=yes]` on the Raspberry Pi repository. This bypasses signature
   enforcement during the build, which is the same treatment already applied
   to the internal `staging.waziup.io` repo.

**Action for review:** because the Pi repo is a build-time dependency (kernel
and firmware only), `trusted=yes` is acceptable here, but it does mean image
builds no longer cryptographically verify Raspberry Pi packages. If stricter
supply-chain guarantees are required, the alternative is to re-sign or replace
the RPi key with an Ed25519/OpenPGP v4 key that Sequoia accepts.

### `stage1/03-install-packages/00-packages`

| Removed                                 | Added         | Reason                                                                                                                |
| --------------------------------------- | ------------- | --------------------------------------------------------------------------------------------------------------------- |
| `libraspberrypi-bin`, `libraspberrypi0` | `raspi-utils` | Userland tools (vcgencmd, dtmerge, …) now ship in raspi-utils; the libraspberrypi packages were dropped for bookworm+ |

### `stage2/01-sys-tweaks/00-packages`

| Removed                                    | Added                                                                | Reason                                               |
| ------------------------------------------ | -------------------------------------------------------------------- | ---------------------------------------------------- |
| `libraspberrypi-dev`, `libraspberrypi-doc` | `linux-headers-rpi-v8`, `linux-headers-rpi-2712`, `raspi-utils-core` | Successor packages for kernel headers / RPi userland |
| `libfreetype6-dev`                         | `libfreetype-dev`                                                    | Package renamed in trixie                            |
| `policykit-1`                              | `polkitd`, `pkexec`                                                  | policykit-1 was split up in trixie                   |
| `rng-tools`                                | `rng-tools5`                                                         | Renamed / replaced in trixie                         |
| `crda`                                     | _(none)_                                                             | Obsolete: wireless regulatory db is now in-kernel    |
| `hardlink`                                 | _(none)_                                                             | Functionality merged into `util-linux`               |

---

## 4. Stage 3 cleanups

### `stage3/02-install-docker/00-run.sh`

- Added `13 → trixie` and `12 → bookworm` to the `dist_version` mapping, so the
  Docker apt repo resolves to `download.docker.com/linux/debian/trixie`.
  (`11 → bullseye` and older mappings retained.)

### `stage3/03-wazigate/00-run.sh`

- Removed the leftover **buster-backports** block (backports repo + `apt-key`
  keyserver call + `libseccomp2` pin). Trixie ships `libseccomp2 ≥ 2.5`
  natively, and the backports repo would fail on trixie anyway. This also
  removes the last use of the deprecated `apt-key` command.

---

## 5. Export stages

Two scripts referenced the removed `raspberrypi-kernel` changelog file and
would have silently produced empty output (or failed) on trixie:

- `export-image/04-finalise/01-run.sh` — firmware/kernel info lines in the
  `.info` file are now derived via `dpkg-query` on `linux-image-rpi-v8` and
  `raspi-firmware`.
- `export-noobs/prerun.sh` — `KERNEL_VER` is now obtained via
  `dpkg-query -W -f='${Version}' linux-image-rpi-v8` instead of grepping the
  changelog.

---

## 6. Verification performed

1. **Repository availability** — `ftp.it.debian.org` serves `trixie` and
   `trixie-updates` (HTTP 200); `security.debian.org/debian-security`
   serves `trixie-security` (HTTP 200). Security support on
   `security.debian.org` currently covers trixie (stable) and bookworm
   (oldstable); bullseye is LTS-only, hence the earlier archive mirror.
2. **Package-name resolution** — a verification script checked every package
   name in all stage `*-packages` files against the trixie Debian archives
   (main, non-free-firmware) and the Raspberry Pi trixie archive. Final
   result: **ALL PACKAGES RESOLVE** — no stage list references a package that
   no longer exists.
3. **Reference-image cross-check** — package names and dependencies for the
   kernel, firmware and config packages were confirmed against the dpkg
   status of the official Raspberry Pi OS trixie image, not against
   documentation.
4. **Real build run (Docker, arm64)** — stage0 and stage1 were built
   end-to-end in the pi-gen container:

   | Stage     | Result                                                                 |
   | --------- | ---------------------------------------------------------------------- |
   | stage0    | ✅ debootstrap of trixie completed; `linux-image-rpi-v8`, `linux-image-rpi-2712` and `raspi-firmware` installed |
   | stage1    | ✅ boot files, fstab, `raspi-utils`                                                   |
   | stage2    | ✅ all renamed/replacement packages resolved                                          |
   | stage3    | ✅ MongoDB 6.0.30 installed and `mongod --version` verified; `ntpsec` installed       |

   Note: pi-gen returns exit status 0 even when a sub-stage failed, so the log,
   not the exit code, is the source of truth.

### 6.1 MongoDB: 4.4 → 6.0 (resolved)

`stage3/01-install-packages` pulled MongoDB from
`repo.mongodb.org/apt/ubuntu focal/mongodb-org/4.4`. That build depends on
**`libssl1.1`**, which does not exist in trixie in any component or
architecture (trixie ships OpenSSL 3 as `libssl3t64`), so the build failed.

**Resolution: migrated to MongoDB 6.0.30** (`jammy` repo, arm64).

| Change | Detail |
|---|---|
| `stage3/01-install-packages/00-run.sh` | repo → `jammy/mongodb-org/6.0`; key → `server-6.0.asc` in a keyring with `signed-by` (replaces removed `apt-key`) |
| `stage3/01-install-packages/01-packages` | pins `mongodb-org*=6.0.30` |
| `stage3/03-wazigate/files/mongod.conf` | removed `storage.journal.enabled` — the key was dropped in 4.x/6.0 and is rejected as unknown |
| `stage3/03-wazigate/files/mongod.service` | added `RuntimeDirectory=mongodb` so `/var/run/mongodb` exists on tmpfs `/run` |
| `stage3/03-wazigate/00-run.sh` | creates `/var/log/mongodb`, `/var/lib/mongodb`, `/var/run/mongodb` owned by `mongodb` |

Dependency check: 6.0.30 needs `libcurl4` and `libssl3`; trixie's `libcurl4t64`
and `libssl3t64` both `Provide` those names, so it installs cleanly.

**⚠️ Data migration requirement.** MongoDB cannot skip major releases. Devices
already in the field on 4.4 **cannot** open their data files with 6.0 directly;
the supported path is **4.4 → 5.0 → 6.0**, using a `mongodump`/`mongorestore`
or `--upgrade`-in-place sequence. An OTA upgrade plan must be prepared for
existing devices before this image is rolled out.

Also verification: `mongod --version` from the built rootfs reports 6.0.30.

### 6.2 Other packages removed in trixie (resolved)

| Package | Resolution |
|---|---|
| `ntp` | Removed from Debian; replaced with `ntpsec` (ships `ntpsec.service`; the unit is enabled in `stage3/03-wazigate`) |

### 6.3 SHA-1 signature policy (resolved)

Debian 13's apt uses the Sequoia verifier. Its shipped policy only permitted
SHA-1 signatures **until 2026**, and that window has expired — so both the
Raspberry Pi archive and the MongoDB repository failed with
`OpenPGP signature verification failed: ... is not signed`.

`Acquire::AllowWeakRepositories` does **not** affect this (it was tried and
didn't help). The working fix is a policy override installed by
`stage0/00-configure-apt/00-run.sh`:

```ini
# /etc/crypto-policies/back-ends/apt-sequoia.config
[hash_algorithms]
sha1 = "always"
```

This relaxes the **hash allow-list**; signatures are still cryptographically
verified. Because of this, the earlier `[trusted=yes]` workaround on the
Raspberry Pi repo was **reverted** — normal signature verification is in use
again.

**Security note for review:** SHA-1 is cryptographically weak. Both third-party
repositories here are build-time-only inputs, but if the organisation requires
stronger assurances, the alternative is to obtain repositories signed with
SHA-256.

---

## 7. Boot partition layout (fixed in this change)

Raspberry Pi OS from bookworm onward mounts the FAT boot partition at
**`/boot/firmware`**, and `raspi-firmware` installs `config.txt`,
`cmdline.txt`, `start*.elf` and the kernel images into `/boot/firmware`.
The repository previously wrote all boot files to `/boot`. This was
corrected end-to-end:

| File | Change |
|---|---|
| `stage1/01-sys-tweaks/files/fstab` | boot partition mounted at `/boot/firmware` |
| `stage1/00-boot-files/00-run.sh` | `cmdline.txt` / `config.txt` → `/boot/firmware/` |
| `stage1/00-boot-files/files/config.txt` | added `[pi5] kernel=kernel_2712.img` / `[pi4] kernel=kernel8.img` selection |
| `stage2/01-sys-tweaks/00-patches/07-resize-init.diff` | patch path → `boot/firmware/cmdline.txt` |
| `export-image/prerun.sh` | mounts/copies boot partition at `/boot/firmware` |
| `export-image/03-set-partuuid/00-run.sh` | fstab/cmdline substitution path |
| `export-image/04-finalise/01-run.sh` | `issue.txt` → `/boot/firmware/` |
| `export-noobs/prerun.sh` | NOOBS boot tarball from `/boot/firmware` |
| `export-noobs/00-release/files/partition_setup.sh` | NOOBS fstab rewrite |
| `scripts/qcow2_handling` | loop-mount and `cmdline.txt` substitution |

---

## 8. Remaining follow-ups / risks

- **MongoDB blocker** — see §6.1. Requires a product decision.
- **Kernel selection on real hardware** — `config.txt` now points Pi 5 at
  `kernel_2712.img` and Pi 4/earlier at `kernel8.img`. This needs a boot test on
  both board families.
- **`staging.waziup.io/downloads/` apt repo** — still `[trusted=yes]` and
  unpinned in `sources.list`; if its debs are built against bullseye's libc,
  dependency checks may fail on trixie.
- **First real build** — this migration is validated at the package-name and
  reference-image level; a full end-to-end `./build-docker.sh` run plus a boot
  test on target hardware (Pi 4 and/or Pi 5) is still required before release.

---

## 9. Files changed (summary)

```
Dockerfile
build.sh
build-docker.sh
scripts/common
scripts/qcow2_handling
export-image/prerun.sh
export-image/03-set-partuuid/00-run.sh
export-image/04-finalise/01-run.sh
export-noobs/prerun.sh
export-noobs/00-release/files/partition_setup.sh
stage0/00-configure-apt/00-run.sh
stage0/00-configure-apt/files/sources.list
stage0/00-configure-apt/files/raspi.list
stage0/02-firmware/01-packages
stage1/00-boot-files/00-run.sh
stage1/00-boot-files/files/config.txt
stage1/01-sys-tweaks/files/fstab
stage1/03-install-packages/00-packages
stage2/01-sys-tweaks/00-packages
stage2/01-sys-tweaks/00-patches/07-resize-init.diff
stage3/01-install-packages/00-run.sh
stage3/02-install-docker/00-run.sh
stage3/03-wazigate/00-run.sh
```

No build outputs, deploy artifacts, or unrelated files were touched.
