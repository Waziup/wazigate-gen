#!/bin/bash -e
#
# Rebuild stage3/03-wazigate/files/shellinaboxd for arm64/trixie.
#
# WHY THIS EXISTS
# The binary committed to this repo in February 2023 was built on a
# bullseye-era system and links libssl.so.1.1. Trixie ships OpenSSL 3, so on
# the first image that booted far enough to run it, shellinaboxd died with
#   /usr/bin/shellinaboxd: error while loading shared libraries: libssl.so.1.1
# Upstream shellinabox has been dead since 2.20 (2016) and Debian dropped the
# package after bullseye, so there is nothing to upgrade to -- it has to be
# rebuilt.
#
# The unit in files/shellinabox.service already passes -t, which disables SSL
# at runtime, so the OpenSSL dependency was never used. Building with
# --disable-ssl removes it outright rather than swapping libssl.so.1.1 for
# libssl.so.3.
#
# HOW
# The build runs inside a throwaway debian:trixie-slim container under QEMU
# emulation (--platform=linux/arm64), matching the gateway's distribution and
# architecture. This is emulation, not cross-compilation: no GOOS/GOARCH, no
# BUILDPLATFORM. The downloaded source is only ever unpacked inside that
# container, never on the host.
#
# USAGE
#   tools/build-shellinaboxd.sh            # build and replace the committed binary
#   KEEP=1 tools/build-shellinaboxd.sh     # also leave the build tree for inspection
#
# Needs docker with binfmt/QEMU registered for arm64, same as the ISO build.

SHELLINABOX_VERSION="${SHELLINABOX_VERSION:-2.20}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$REPO_ROOT/stage3/03-wazigate/files/shellinaboxd"
OUT_DIR="$(mktemp -d)"
trap '[ -n "${KEEP:-}" ] || rm -rf "$OUT_DIR"' EXIT

echo "Building shellinabox $SHELLINABOX_VERSION for linux/arm64 on debian:trixie-slim"
echo "Output directory: $OUT_DIR"

docker run --rm -i --platform=linux/arm64 \
	-v "$OUT_DIR:/out" \
	-e "SHELLINABOX_VERSION=$SHELLINABOX_VERSION" \
	debian:trixie-slim bash -s <<'BUILD'
set -eux

export DEBIAN_FRONTEND=noninteractive
apt-get update
# No libssl-dev: the point of this build is to not link OpenSSL at all.
apt-get install -y --no-install-recommends \
	build-essential autoconf automake libtool pkg-config \
	zlib1g-dev libpam0g-dev \
	ca-certificates curl

mkdir -p /build/src
cd /build/src
curl -fsSL -o shellinabox.tar.gz \
	"https://github.com/shellinabox/shellinabox/archive/refs/tags/v${SHELLINABOX_VERSION}.tar.gz"
tar -xzf shellinabox.tar.gz --strip-components=1

# shellinabox 2.20 (2016) predates OpenSSL 1.1, which made BIO an opaque type.
# libhttp/ssl.c:1018 sslGetNextBIO is the one function in that file whose body
# is NOT wrapped in "#if defined(HAVE_OPENSSL)", and its version test reads
#     #if OPENSSL_VERSION_NUMBER <= 0x10100000L
# With --disable-ssl that macro is never defined. The C preprocessor evaluates
# an undefined identifier in #if as 0, and 0 <= 0x10100000L is true, so the
# pre-1.1 branch is taken and the code dereferences b->next_bio on a type that
# libhttp/ssl.h:62 only forward declares:
#     libhttp/ssl.c:1020: error: invalid use of incomplete typedef 'BIO'
# Every caller sits inside sslFreeHndl's own HAVE_OPENSSL block, so with SSL
# disabled this function is dead code that only has to compile; NULL is safe.
# The "<=" is also off by one -- 0x10100000L IS OpenSSL 1.1.0, the first
# version with an opaque BIO -- so it becomes "<" at the same time.
if ! grep -q '^#if OPENSSL_VERSION_NUMBER <= 0x10100000L$' libhttp/ssl.c; then
	echo "ERROR: the sslGetNextBIO guard is not where this patch expects it." >&2
	echo "shellinabox $SHELLINABOX_VERSION differs from 2.20; re-check libhttp/ssl.c." >&2
	exit 1
fi
sed -i 's|^#if OPENSSL_VERSION_NUMBER <= 0x10100000L$|#if !defined(HAVE_OPENSSL)\n  return NULL;\n#elif OPENSSL_VERSION_NUMBER < 0x10100000L|' libhttp/ssl.c

# shellinabox 2.20 is 2016-era C. GCC 14 promoted implicit function
# declarations, int/pointer conversions and incompatible pointer types from
# warnings to hard errors, so the upstream tree does not build unmodified.
# These flags restore the pre-GCC-14 behaviour for exactly those diagnostics;
# they do not silence anything else.
export CFLAGS="-O2 -g0 \
	-Wno-implicit-function-declaration \
	-Wno-int-conversion \
	-Wno-incompatible-pointer-types \
	-Wno-discarded-qualifiers"

autoreconf -fi
./configure --disable-ssl --disable-debug
make -j"$(nproc)"

strip shellinaboxd
cp shellinaboxd /out/shellinaboxd

echo "=== result ==="
file /out/shellinaboxd
echo "=== shared library dependencies ==="
ldd /out/shellinaboxd
# Fail loudly rather than shipping another binary that cannot start.
if ldd /out/shellinaboxd | grep -qi 'libssl\|libcrypto'; then
	echo "ERROR: the binary still links OpenSSL; --disable-ssl did not take effect." >&2
	exit 1
fi
if ldd /out/shellinaboxd | grep -qi 'not found'; then
	echo "ERROR: the binary has unresolved libraries on trixie itself." >&2
	exit 1
fi
BUILD

if [ ! -s "$OUT_DIR/shellinaboxd" ]; then
	echo "ERROR: the build produced no binary." >&2
	exit 1
fi

install -m 755 "$OUT_DIR/shellinaboxd" "$DEST"
echo
echo "Installed $(stat -c%s "$DEST") bytes to:"
echo "  $DEST"
echo "Commit it, then rebuild the ISO."
