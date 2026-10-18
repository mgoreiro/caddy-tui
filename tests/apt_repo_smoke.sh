#!/usr/bin/env bash
# Smoke test of tools/update-apt-repo.sh with a throw-away key and a dummy package.
# Needs: apt-utils, gnupg, dpkg-dev. Used by CI (job apt-repo).
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d); trap 'rm -rf "$work"; gpgconf --kill gpg-agent >/dev/null 2>&1 || true' EXIT

export GNUPGHOME=$(mktemp -d /tmp/smokegpg.XXXXXX)
printf '%%no-protection\nKey-Type: RSA\nKey-Length: 2048\nKey-Usage: sign\nName-Real: smoke\nName-Email: smoke@example.invalid\nExpire-Date: 1d\n%%commit\n' > "$work/params"
gpg --batch --quiet --gen-key "$work/params"
export APT_GPG_PRIVATE_KEY; APT_GPG_PRIVATE_KEY=$(gpg --armor --export-secret-keys smoke)
fpr=$(gpg --list-keys --with-colons smoke | awk -F: '/^fpr/{print $10; exit}')
unset GNUPGHOME                       # the script must use its own keyring

for v in 1.0.0 1.0.1; do
  d="$work/pkg-$v"; mkdir -p "$d/DEBIAN"
  printf 'Package: caddy-tui\nVersion: %s\nArchitecture: all\nMaintainer: t <t@example.invalid>\nDescription: dummy\n' "$v" > "$d/DEBIAN/control"
  dpkg-deb --build "$d" "$work/caddy-tui_${v}_all.deb" >/dev/null
done

repo="$work/site/apt"; mkdir -p "$repo"
"$here/tools/update-apt-repo.sh" "$repo" "$work/caddy-tui_1.0.0_all.deb" >/dev/null 2>&1
"$here/tools/update-apt-repo.sh" "$repo" "$work/caddy-tui_1.0.1_all.deb" >/dev/null 2>&1   # second run keeps the first

ls "$repo/pool/main/c/caddy-tui" | grep -q 1.0.0
ls "$repo/pool/main/c/caddy-tui" | grep -q 1.0.1
grep -q '^Version: 1.0.0' "$repo/dists/stable/main/binary-all/Packages"
grep -q '^Version: 1.0.1' "$repo/dists/stable/main/binary-all/Packages"
grep -q '^Architectures: all' "$repo/dists/stable/Release"

# signatures verify against the exported public key, and the Release hashes cover Packages.gz
export GNUPGHOME=$(mktemp -d /tmp/smokeverify.XXXXXX)
gpg --batch --quiet --import "$repo/caddy-tui.gpg"
gpg --batch --verify "$repo/dists/stable/Release.gpg" "$repo/dists/stable/Release" 2>&1 | grep -q "Good signature"
gpg --batch --verify "$repo/dists/stable/InRelease" 2>&1 | grep -q "Good signature"
sum=$(sha256sum "$repo/dists/stable/main/binary-all/Packages.gz" | cut -d' ' -f1)
grep -q "$sum" "$repo/dists/stable/Release"
gpg --list-keys --with-colons | grep -q "$fpr"
rm -rf "$GNUPGHOME"
echo "apt repository smoke test: OK"
