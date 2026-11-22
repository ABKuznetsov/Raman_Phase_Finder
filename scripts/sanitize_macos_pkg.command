#!/bin/zsh
set -euo pipefail

if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
    echo "Usage: $0 COMPONENT.pkg" >&2
    exit 2
fi

PACKAGE_PATH="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
WORK_DIR="$(mktemp -d /tmp/ir-raman-pkg-sanitize.XXXXXX)"
EXPANDED_DIR="$WORK_DIR/expanded"
PAYLOAD_DIR="$WORK_DIR/payload"
CLEAN_PACKAGE="$WORK_DIR/clean.pkg"

cleanup() {
    /bin/rm -rf "$WORK_DIR"
}
trap cleanup EXIT

pkgutil --expand "$PACKAGE_PATH" "$EXPANDED_DIR"
# Script resources are outside Payload, so pkgbuild's payload filters do not
# cover AppleDouble files created next to preinstall/postinstall on macOS.
find "$EXPANDED_DIR" -name '._*' -delete
find "$EXPANDED_DIR" -name '.DS_Store' -delete
mkdir -p "$PAYLOAD_DIR"
(
    cd "$PAYLOAD_DIR"
    gzip -dc "$EXPANDED_DIR/Payload" | cpio -idm >/dev/null 2>&1
    find . -name '._*' -delete
    find . -print | cpio -o --format odc 2>/dev/null | gzip -c > "$EXPANDED_DIR/Payload"
)
mkbom "$PAYLOAD_DIR" "$EXPANDED_DIR/Bom"
pkgutil --flatten "$EXPANDED_DIR" "$CLEAN_PACKAGE"
mv "$CLEAN_PACKAGE" "$PACKAGE_PATH"
