#!/bin/zsh
set -e
export COPYFILE_DISABLE=1
export COPY_EXTENDED_ATTRIBUTES_DISABLE=1

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_PYTHON="${RAMAN_BUILD_PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$BUILD_PYTHON" ]; then BUILD_PYTHON=python3; fi
VERSION="$("$BUILD_PYTHON" -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' "$ROOT/pyproject.toml")"
APP_NAME="Raman Phase Finder"
APP_BUNDLE_NAME="Raman Phase Finder"
EXECUTABLE_NAME="ir-raman-phase-finder"
IDENTIFIER="com.irraman.phasefinder.app"
PKG_IDENTIFIER="com.ramanphasefinder.macos.pkg"
PKG_NAME="Raman_Phase_Finder_macOS_${VERSION}.pkg"
DIST_DIR="${RAMAN_BUILD_OUTPUT:-$ROOT/dist}"
STAGE_ROOT="$(mktemp -d /private/tmp/raman-pkg-build.XXXXXX)"
PAYLOAD_ROOT="$STAGE_ROOT/payload"
SCRIPTS_DIR="$STAGE_ROOT/scripts"
APP_BUNDLE="$PAYLOAD_ROOT/Applications/$APP_BUNDLE_NAME.app"
CONTENTS_DIR="$APP_BUNDLE/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
APP_PAYLOAD_DIR="$RESOURCES_DIR/app"
COMPONENT_PKG="$STAGE_ROOT/Raman_Phase_Finder.component.pkg"
PKG_PATH="$DIST_DIR/$PKG_NAME"

cd "$ROOT"

if ! command -v pkgbuild >/dev/null 2>&1; then
    echo "pkgbuild was not found. Install Xcode Command Line Tools."
    exit 1
fi

if ! command -v productbuild >/dev/null 2>&1; then
    echo "productbuild was not found. Install Xcode Command Line Tools."
    exit 1
fi

echo "Building macOS PKG: $PKG_PATH"
if [ -e "$PKG_PATH" ]; then
    echo "Refusing to overwrite $PKG_PATH. Set RAMAN_BUILD_OUTPUT to a new directory." >&2
    exit 1
fi
mkdir -p "$MACOS_DIR" "$RESOURCES_DIR" "$APP_PAYLOAD_DIR" "$SCRIPTS_DIR" "$DIST_DIR"

"$BUILD_PYTHON" "$ROOT/scripts/stage_raman_release.py" "$ROOT" "$APP_PAYLOAD_DIR"

chmod +x "$APP_PAYLOAD_DIR"/install_macos.command "$APP_PAYLOAD_DIR"/update_macos.command "$APP_PAYLOAD_DIR"/toolkit/*.command "$APP_PAYLOAD_DIR"/*.command "$APP_PAYLOAD_DIR"/*.sh 2>/dev/null || true

if [ -f "$APP_PAYLOAD_DIR/icon.png" ]; then
    cp "$APP_PAYLOAD_DIR/icon.png" "$RESOURCES_DIR/icon.png"
    ICON_PYTHON=""
    if "$BUILD_PYTHON" -c 'import PIL' >/dev/null 2>&1; then
        ICON_PYTHON="$BUILD_PYTHON"
    elif python3 -c 'import PIL' >/dev/null 2>&1; then
        ICON_PYTHON="python3"
    fi
    if [ -n "$ICON_PYTHON" ]; then
        "$ICON_PYTHON" - "$APP_PAYLOAD_DIR/icon.png" "$RESOURCES_DIR/icon.icns" <<'PY'
from pathlib import Path
import sys
from PIL import Image

source = Path(sys.argv[1])
target = Path(sys.argv[2])
image = Image.open(source).convert("RGBA")
image.save(target, format="ICNS", sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)])
PY
    fi
fi

ICON_PLIST=""
if [ -f "$RESOURCES_DIR/icon.icns" ]; then
    ICON_PLIST='    <key>CFBundleIconFile</key>
    <string>icon.icns</string>'
fi

cat > "$CONTENTS_DIR/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>en</string>
    <key>CFBundleDisplayName</key>
    <string>$APP_NAME</string>
    <key>CFBundleExecutable</key>
    <string>$EXECUTABLE_NAME</string>
    <key>CFBundleIdentifier</key>
    <string>$IDENTIFIER</string>
$ICON_PLIST
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>$APP_NAME</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>$VERSION</string>
    <key>CFBundleVersion</key>
    <string>$VERSION</string>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

cat > "$MACOS_DIR/$EXECUTABLE_NAME" <<'LAUNCHER'
#!/bin/zsh
set -e

APP_BUNDLE="$(cd "$(dirname "$0")/../.." && pwd)"
APP_ROOT="$APP_BUNDLE/Contents/Resources/app"
exec "$APP_ROOT/toolkit/launch_ir_raman_phase_finder_preview.command" "$@"
LAUNCHER

chmod +x "$MACOS_DIR/$EXECUTABLE_NAME"
xattr -cr "$APP_BUNDLE" >/dev/null 2>&1 || true
xattr -cr "$PAYLOAD_ROOT" >/dev/null 2>&1 || true
xattr -dr com.apple.quarantine "$APP_BUNDLE" >/dev/null 2>&1 || true

COMPONENT_PLIST="$STAGE_ROOT/bundle.plist"
cat > "$COMPONENT_PLIST" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<array><dict>
    <key>RootRelativeBundlePath</key><string>Applications/Raman Phase Finder.app</string>
    <key>BundleIsRelocatable</key><false/>
    <key>BundleIsVersionChecked</key><false/>
    <key>BundleHasStrictIdentifier</key><true/>
    <key>BundleOverwriteAction</key><string>upgrade</string>
</dict></array>
</plist>
PLIST

cat > "$SCRIPTS_DIR/postinstall" <<POSTINSTALL
#!/bin/zsh
set -e

APP_BUNDLE="/Applications/$APP_BUNDLE_NAME.app"
xattr -dr com.apple.quarantine "\$APP_BUNDLE" >/dev/null 2>&1 || true
touch "\$APP_BUNDLE" >/dev/null 2>&1 || true
touch "\$APP_BUNDLE/Contents/Info.plist" >/dev/null 2>&1 || true

LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
if [ -x "\$LSREGISTER" ]; then
    "\$LSREGISTER" -f "\$APP_BUNDLE" >/dev/null 2>&1 || true
fi

/usr/bin/qlmanage -r cache >/dev/null 2>&1 || true
/usr/bin/killall Dock >/dev/null 2>&1 || true

exit 0
POSTINSTALL
chmod +x "$SCRIPTS_DIR/postinstall"

pkgbuild \
    --root "$PAYLOAD_ROOT" \
    --component-plist "$COMPONENT_PLIST" \
    --install-location "/" \
    --identifier "$PKG_IDENTIFIER" \
    --version "$VERSION" \
    --ownership recommended \
    --scripts "$SCRIPTS_DIR" \
    --filter '(^|/)\._[^/]*$' \
    --filter '(^|/)\.DS_Store$' \
    --filter '(^|/)\.git($|/)' \
    --filter '(^|/)__pycache__( \([0-9]+\))?($|/)' \
    --filter '(^|/)\.pytest_cache($|/)' \
    --filter '(^|/)\.ruff_cache($|/)' \
    --filter '(^|/)\.mypy_cache($|/)' \
    --filter '(^|/)\.superpowers($|/)' \
    --filter '(^|/)портобл($|/)' \
    --filter '(^|/)portable($|/)' \
    --filter '\.pyc$' \
    "$COMPONENT_PKG"

"$ROOT/scripts/sanitize_macos_pkg.command" "$COMPONENT_PKG"

productbuild \
    --package "$COMPONENT_PKG" \
    "$PKG_PATH"

echo "$PKG_PATH"
