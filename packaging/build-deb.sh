#!/bin/sh
# Build linux-airdrop_<version>_amd64.deb from this source tree.
# Usage: packaging/build-deb.sh [version] [output-dir]      (needs dpkg-deb and python3)
set -eu
here=$(cd "$(dirname "$0")" && pwd)
root=$(dirname "$here")
version=${1:-1.1.0}
out=${2:-"$root/dist"}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
pkg="$work/pkg"
lib="$pkg/usr/lib/linux-airdrop"
doc="$pkg/usr/share/doc/linux-airdrop"
mkdir -p "$lib" "$doc" "$pkg/DEBIAN" "$pkg/usr/bin" "$pkg/usr/share/applications" \
         "$pkg/usr/share/icons/hicolor/scalable/apps" "$pkg/usr/share/icons/hicolor/symbolic/apps"

# The program: everything needed at run time, nothing else (no tests, no packaging, no VCS data).
for item in app.py confirm.py tray.py autostart.py system_helper.py root_bridge.py p2p_anchor.py \
            awdl_diag.py secure_receiver.py fileinfo.py progress.py transfer_events.py store.py preview.py \
            identity.py outgoing.py diagnostics.py repair.py ui_transfers.py ui_history.py ui_devices.py \
            ui_diagnostics.py nautilus start.sh LICENSE verification.json icons backend; do
    cp -a "$root/$item" "$lib/"
done
find "$lib" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$lib" -name '*.pyc' -delete
find "$pkg" -type d -exec chmod 755 {} +
find "$lib" -type f -exec chmod 644 {} +
chmod 755 "$lib/start.sh" "$lib/confirm.py" "$lib/backend/opendrop" "$lib/backend/owl" \
          "$lib/backend/owl-modern" "$lib/backend/owl-original" "$lib/nautilus/scripts/Invia con Linux AirDrop"

# GNOME Files context-menu entry (loaded by python3-nautilus, which the package only recommends).
mkdir -p "$pkg/usr/share/nautilus-python/extensions"
install -m 644 "$root/nautilus/linux_airdrop_nautilus.py" "$pkg/usr/share/nautilus-python/extensions/"

# Launcher, desktop entry, icons.
install -m 755 "$here/debian/linux-airdrop" "$pkg/usr/bin/linux-airdrop"
install -m 644 "$here/debian/it.foglio.LinuxAirDrop.desktop" "$pkg/usr/share/applications/"
install -m 644 "$root/icons/it.foglio.LinuxAirDrop.svg" "$pkg/usr/share/icons/hicolor/scalable/apps/"
install -m 644 "$root/icons/linux-airdrop-symbolic.svg" "$root/icons/linux-airdrop-active-symbolic.svg" \
        "$pkg/usr/share/icons/hicolor/symbolic/apps/"

# Documentation, copyright, changelog and the licences of the bundled Python libraries.
install -m 644 "$root/README.md" "$doc/README.md"
install -m 644 "$root"/docs/*.md "$doc/"
python3 - "$root" "$doc" "$version" <<'PY'
import email.parser, glob, os, sys, time, gzip
root, doc, version = sys.argv[1:4]
rows = []
for meta in sorted(glob.glob(os.path.join(root, "backend/python/*.dist-info/METADATA"))):
    head = email.parser.HeaderParser().parsestr(open(meta, encoding="utf-8", errors="replace").read(), headersonly=True)
    name, ver = head.get("Name", "?"), head.get("Version", "?")
    lic = head.get("License-Expression") or head.get("License") or ""
    if not lic or len(lic) > 60:
        classifiers = [c.split("::")[-1].strip() for c in head.get_all("Classifier", []) if c.startswith("License ::")]
        lic = ", ".join(classifiers) or "see the package's dist-info"
    rows.append(f"{name} {ver}: {lic}")
open(os.path.join(doc, "third-party-licenses.txt"), "w").write(
    "Python libraries bundled in /usr/lib/linux-airdrop/backend/python (licence as declared by each package;\n"
    "full texts are in the matching *.dist-info directories and in backend/licenses):\n\n" + "\n".join(rows) + "\n")
stamp = time.strftime("%a, %d %b %Y %H:%M:%S %z")
with gzip.open(os.path.join(doc, "changelog.Debian.gz"), "wt") as f:
    f.write(f"linux-airdrop ({version}) unstable; urgency=low\n\n  * Transfer history, remembered devices, drag and drop, live progress,\n    pre-acceptance preview, diagnostics with guided repairs, GNOME Files entry.\n  * The radio code, the driver modules and the privileged helper are unchanged.\n\n"
            f" -- Foglio24 <82967977+Foglio24@users.noreply.github.com>  {stamp}\n")
PY
cat > "$doc/copyright" <<'CR'
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: linux-airdrop
Source: https://github.com/Foglio24/linux-airdrop

Files: *
Copyright: 2026 Foglio24 and contributors
License: GPL-3+

Files: usr/lib/linux-airdrop/backend/owl* usr/lib/linux-airdrop/backend/python/opendrop/*
Copyright: the OWL and OpenDrop authors (see usr/lib/linux-airdrop/backend/licenses)
License: GPL-3+

Files: usr/lib/linux-airdrop/backend/driver-compat/*.ko
Copyright: the Linux kernel authors
License: GPL-2
Comment: Built from the Ubuntu 7.3.0-9.9 kernel sources with the patches shipped next to them.
 The complete corresponding sources are in the same directory (driver-sources.tar.gz,
 patched-sources.tar.gz, *.patch).

Files: usr/lib/linux-airdrop/backend/python/*
Copyright: their respective authors
License: various
Comment: Third-party Python libraries, each under its own licence. See third-party-licenses.txt
 and the *.dist-info directories.

License: GPL-3+
 On Debian systems the full text of the GNU General Public License version 3 is in
 /usr/share/common-licenses/GPL-3.

License: GPL-2
 On Debian systems the full text of the GNU General Public License version 2 is in
 /usr/share/common-licenses/GPL-2.
CR
chmod 644 "$doc"/*

# Control data.
size=$(du -sk --exclude=DEBIAN "$pkg" | cut -f1)
sed -e "s/@VERSION@/$version/" -e "s/@SIZE@/$size/" "$here/debian/control.in" > "$pkg/DEBIAN/control"
install -m 755 "$here/debian/postinst" "$here/debian/postrm" "$pkg/DEBIAN/"

mkdir -p "$out"
deb="$out/linux-airdrop_${version}_amd64.deb"
dpkg-deb --root-owner-group -Zxz --build "$pkg" "$deb" >/dev/null
(cd "$out" && sha256sum "$(basename "$deb")" > "$(basename "$deb").sha256")
echo "$deb"
