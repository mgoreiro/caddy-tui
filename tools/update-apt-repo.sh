#!/usr/bin/env bash
# Add .deb packages to caddy-tui's signed apt repository (a static tree served by GitHub Pages).
#
#   APT_GPG_PRIVATE_KEY="$(cat key.asc)" tools/update-apt-repo.sh APT_DIR package.deb [...]
#
# APT_DIR is the root of the repository tree (e.g. the `apt/` folder of the gh-pages branch). It is
# updated in place: pool/ gets the packages (old versions are kept), dists/stable/ is regenerated and
# signed, and caddy-tui.gpg (the public key users install) is refreshed. Needs apt-utils and gnupg.
set -euo pipefail

[ $# -ge 2 ] || { echo "usage: $0 APT_DIR package.deb [...]" >&2; exit 2; }
apt_dir=$1; shift
[ -n "${APT_GPG_PRIVATE_KEY:-}" ] || { echo "APT_GPG_PRIVATE_KEY is empty" >&2; exit 1; }
command -v apt-ftparchive >/dev/null || { echo "apt-ftparchive not found (apt-get install apt-utils)" >&2; exit 1; }

export GNUPGHOME
GNUPGHOME=$(mktemp -d /tmp/aptgpg.XXXXXX)          # short path: gpg-agent sockets live in here
trap 'gpgconf --kill gpg-agent >/dev/null 2>&1 || true; rm -rf "$GNUPGHOME"' EXIT
printf '%s\n' "$APT_GPG_PRIVATE_KEY" | gpg --batch --quiet --import
keyid=$(gpg --batch --list-secret-keys --with-colons | awk -F: '/^fpr/{print $10; exit}')
[ -n "$keyid" ] || { echo "no secret key imported" >&2; exit 1; }

suite=stable component=main
# Packages are Architecture: all (one binary-all index). Listing the common architectures too makes apt treat them
# as supported (and silently ignore their missing indexes) instead of printing "doesn't support architecture" notices
# on multiarch hosts. "all" must stay in the list: it is what makes apt fetch binary-all.
archs="all amd64 arm64 armhf armel i386 riscv64 ppc64el s390x"
pool="pool/$component/c/caddy-tui"
mkdir -p "$apt_dir/$pool"
for deb in "$@"; do
  [ -f "$deb" ] || { echo "not a file: $deb" >&2; exit 1; }
  dpkg-deb --field "$deb" Package >/dev/null          # refuse anything that is not a .deb
  cp -f "$deb" "$apt_dir/$pool/"
done

cd "$apt_dir"
dist="dists/$suite"
rm -rf "$dist"
mkdir -p "$dist/$component/binary-all"
apt-ftparchive --arch all packages pool > "$dist/$component/binary-all/Packages"
gzip -9nk "$dist/$component/binary-all/Packages"
apt-ftparchive \
  -o "APT::FTPArchive::Release::Origin=caddy-tui" \
  -o "APT::FTPArchive::Release::Label=caddy-tui" \
  -o "APT::FTPArchive::Release::Suite=$suite" \
  -o "APT::FTPArchive::Release::Codename=$suite" \
  -o "APT::FTPArchive::Release::Architectures=$archs" \
  -o "APT::FTPArchive::Release::Components=$component" \
  -o "APT::FTPArchive::Release::Description=caddy-tui: terminal UI for Caddy in Docker" \
  release "$dist" > "$dist/Release"
gpg --batch --yes --pinentry-mode loopback --default-key "$keyid" --armor --detach-sign -o "$dist/Release.gpg" "$dist/Release"
gpg --batch --yes --pinentry-mode loopback --default-key "$keyid" --clearsign -o "$dist/InRelease" "$dist/Release"
gpg --batch --export "$keyid" > caddy-tui.gpg
echo "apt repository updated: $(ls "$pool" | wc -l) package file(s), signed with ${keyid: -16}"
