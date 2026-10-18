# The apt repository and how releases are published

[Español](apt-repository.es.md) · [Installing caddy-tui](install.md) · [Back to the README](../README.md)

*For maintainers.* Users only need [install.md](install.md).

## Overview

```
 git tag / GitHub release ──► workflow "release" ──┬─► builds + lints the .deb ─► attached to the release (+ SHA256SUMS)
                                                   └─► tools/update-apt-repo.sh ─► branch gh-pages ─► GitHub Pages
                                                          (signs with the APT_GPG_PRIVATE_KEY secret)   https://mgoreiro.github.io/caddy-tui/apt
```

The repository is a **static tree of files** — no server, no database — so it can live in git and be served by GitHub Pages.

| Piece | Where |
|---|---|
| Publishing script | [`tools/update-apt-repo.sh`](../tools/update-apt-repo.sh) |
| Workflow | [`.github/workflows/release.yml`](../.github/workflows/release.yml) |
| Smoke test of the script | [`tests/apt_repo_smoke.sh`](../tests/apt_repo_smoke.sh) (CI job `apt-repo`) |
| The published tree | branch **`gh-pages`**, folder `apt/` |
| Signing key | private: repository secret **`APT_GPG_PRIVATE_KEY`** (+ the owner's offline backup) · public: `apt/caddy-tui.gpg` |
| Pages | Settings → Pages: *Deploy from a branch* → `gh-pages` `/ (root)` |

Layout of the `gh-pages` branch:

```
index.html  .nojekyll                      landing page with the three install commands
apt/
  caddy-tui.gpg                            public key (binary, for signed-by=)
  pool/main/c/caddy-tui/caddy-tui_X.Y.Z_all.deb     every published version is kept
  dists/stable/
    Release  Release.gpg  InRelease        index of indexes, detached + clear-signed
    main/binary-all/Packages  Packages.gz
```

Packages are `Architecture: all`, so a single `binary-all` index serves every CPU (checked from arm64, amd64 and armhf clients).
The `Release` file declares `Origin: caddy-tui`, `Suite`/`Codename: stable`, which is what `unattended-upgrades` patterns and apt pinning match on.

## What the release workflow does

Triggered when a GitHub release is **published** (or by hand, see below):

1. Checks that the tag, `__version__` in `caddy-tui.py` and the top entry of `debian/changelog` are the same version.
2. Builds the package (`dpkg-buildpackage`), runs `lintian --fail-on error`.
3. Uploads `caddy-tui_X.Y.Z_all.deb` and `SHA256SUMS` to the release.
4. Checks out `tools/` from `main` (so older tags can be republished too) and runs `update-apt-repo.sh`, which: copies the `.deb` into the pool,
   regenerates `Packages(.gz)` and `Release` with `apt-ftparchive`, signs `Release` (`Release.gpg` + `InRelease`) with the imported key in a throw-away keyring,
   and re-exports the public key.
5. Commits the result to `gh-pages` and pushes. Pages rebuilds in a minute or two.

Runs are serialized (`concurrency: apt-repository`) because each one rewrites the branch. If the secret is not set, step 4–5 are skipped with a message.

## Publishing a new version

1. Choose the version `X.Y.Z` and update **all three** places: `__version__` in `caddy-tui.py`, a new top entry in `debian/changelog`
   (same version; date from `date -R`) and the version in the `.TH` line of `man/caddy-tui.1`.
2. `make test` (the unit tests also check that translations are complete) and merge to `main` with green CI.
3. Create the release **pointing at the full commit hash** (an abbreviated hash is rejected by GitHub):

   ```bash
   gh release create vX.Y.Z --target "$(git rev-parse HEAD)" --title "vX.Y.Z" --notes "…"
   ```
4. Watch it: `gh run list --workflow release` → success; `gh release view vX.Y.Z --json assets`.
5. Verify the repository (see below). On a client with the repository configured, `sudo apt update && sudo apt upgrade` must offer the new version
   (Pages can take a couple of minutes: `curl -s https://raw.githubusercontent.com/mgoreiro/caddy-tui/gh-pages/apt/dists/stable/main/binary-all/Packages | grep ^Version` shows the branch immediately).

### Republishing an existing version

```bash
gh workflow run release.yml -f tag=vX.Y.Z
```

It rebuilds the package from that tag, **replaces the release assets** and republishes it to the repository. The rebuilt `.deb` has the same contents
but a different checksum (the archive is not bit-for-bit reproducible), and `SHA256SUMS` is updated accordingly.

## Verifying the published repository

From any clean Debian/Ubuntu (a container is enough) — these are exactly the user steps:

```bash
docker run --rm -it debian:bookworm bash
apt-get update && apt-get install -y curl ca-certificates gnupg
curl -fsSL https://mgoreiro.github.io/caddy-tui/apt/caddy-tui.gpg -o /usr/share/keyrings/caddy-tui.gpg
gpg --show-keys --with-fingerprint /usr/share/keyrings/caddy-tui.gpg          # BA7E 2381 6880 1639 21BC  70DE 15C2 632D D390 10EE
echo "deb [signed-by=/usr/share/keyrings/caddy-tui.gpg] https://mgoreiro.github.io/caddy-tui/apt stable main" > /etc/apt/sources.list.d/caddy-tui.list
apt-get update && apt-cache policy caddy-tui && apt-get install -y caddy-tui && caddy-tui --version
```

## Publishing by hand (without CI)

Needs the key backup, `apt-utils` and `gnupg`:

```bash
git clone --branch gh-pages https://github.com/mgoreiro/caddy-tui.git /tmp/site
APT_GPG_PRIVATE_KEY="$(cat caddy-tui-apt-PRIVATE.asc)" tools/update-apt-repo.sh /tmp/site/apt path/to/caddy-tui_X.Y.Z_all.deb
cd /tmp/site && git add -A && git commit -m "caddy-tui X.Y.Z" && git push origin gh-pages
```

### Removing a version

Delete its `.deb` from `apt/pool/main/c/caddy-tui/` in a clone of `gh-pages`, then run the script again passing **a copy of a `.deb` that is
not in the pool folder** (the script needs at least one package argument; copy the newest one to `/tmp` and pass that), commit and push.

## The signing key

| | |
|---|---|
| Algorithm · expiry | RSA 4096 · **2029-10-17** |
| Fingerprint | `BA7E 2381 6880 1639 21BC  70DE 15C2 632D D390 10EE` |
| Private key | secret `APT_GPG_PRIVATE_KEY` (armored, no passphrase) and the owner's offline backup — **never** commit it |
| Name / email in the key | `caddy-tui apt repository <caddy-tui-apt@users.noreply.github.com>` |

**Generating a new key** (in a throw-away container, so nothing is left on the host):

```bash
mkdir -m 700 out && docker run --rm -v "$PWD/out:/out" debian:bookworm bash -c '
  set -euo pipefail; apt-get update -qq && apt-get install -y -qq gnupg
  export GNUPGHOME=/tmp/g; mkdir -m 700 $GNUPGHOME
  printf "%%no-protection\nKey-Type: RSA\nKey-Length: 4096\nKey-Usage: sign\nName-Real: caddy-tui apt repository\nName-Email: caddy-tui-apt@users.noreply.github.com\nExpire-Date: 3y\n%%commit\n" > /tmp/p
  gpg --batch --gen-key /tmp/p
  gpg --armor --export-secret-keys "caddy-tui apt repository" > /out/caddy-tui-apt-PRIVATE.asc
  gpg --export "caddy-tui apt repository" > /out/caddy-tui.gpg'
gh secret set APT_GPG_PRIVATE_KEY --repo mgoreiro/caddy-tui < out/caddy-tui-apt-PRIVATE.asc
```

**Rotating or renewing** (before it expires, or if it leaks):
1. Generate the new key as above and update the secret.
2. Publish a release (or `gh workflow run release.yml -f tag=<latest>`): the script re-signs everything and replaces `apt/caddy-tui.gpg` with the new public key.
3. Update the fingerprint and expiry in [install.md](install.md), [install.es.md](install.es.md), the READMEs and `index.html` on `gh-pages`.
4. Tell users to run step 1 of the installation again (`curl … | sudo tee /usr/share/keyrings/caddy-tui.gpg`). Until they do, `apt update` fails with
   `NO_PUBKEY`/`EXPKEYSIG` — that is the point of signing. If the old key leaked, say so in the release notes.

## Security notes

- The secret is only exposed to runs triggered by `release` and `workflow_dispatch` in this repository; pull requests (including from forks) never see it.
  Keep it that way: do not add triggers such as `pull_request_target` that run code from a PR with secrets.
- Whoever can push to `gh-pages` or read the secret can publish packages that every client trusts. Limit who has write access to the repository.
- Clients trust the key only for this repository (`signed-by=`), not for the whole system.

## Troubleshooting a publication

| Symptom | Fix |
|---|---|
| `Release.target_commitish is invalid` creating the release | Use the **full** commit hash with `--target`. |
| The workflow succeeded but clients do not see the version | Pages is still building (≈1–2 min); check the raw branch URL above. |
| `git push … gh-pages` rejected | Two runs overlapped; re-run the failed one (`gh run rerun`). |
| Step "Publish to the apt repository" says the secret is not set | Re-create it with `gh secret set APT_GPG_PRIVATE_KEY …`. |
| `gpg: can't connect to the gpg-agent` / `File name too long` | The script already uses a short `/tmp` keyring; if you changed `GNUPGHOME`, keep the path short. |
| `apt update` on a client: `Hash Sum mismatch` right after publishing | Pages/CDN is serving a mix of old and new files for a minute; retry. |
