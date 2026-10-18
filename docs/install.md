# Installing caddy-tui

[Español](install.es.md) · [Back to the README](../README.md)

caddy-tui is a single Python 3 program. The **recommended** way to install it on Debian-based systems is its own
signed **apt repository**: you add it once and from then on `apt upgrade` keeps caddy-tui up to date like any other package.

- [Requirements](#requirements)
- [Option 1 · apt repository (recommended)](#option-1--apt-repository-recommended)
- [Option 2 · the `.deb` file](#option-2--the-deb-file)
- [Option 3 · run it from the source](#option-3--run-it-from-the-source)
- [After installing](#after-installing)
- [Updating](#updating)
- [Uninstalling](#uninstalling)
- [Troubleshooting](#troubleshooting)
- [Key and repository reference](#key-and-repository-reference)

## Requirements

| | |
|---|---|
| System | Debian, Ubuntu, Raspberry Pi OS, Armbian or any apt-based Linux. Any CPU architecture: the package is `all` (it is just Python). |
| Python | 3.8 or newer (`python3` is installed as a dependency). No Python libraries are needed. |
| Docker | A working Docker (`docker.io` or Docker's own `docker-ce`). The package depends on `docker.io \| docker-ce-cli`, so apt installs `docker.io` only if you have neither. |
| Privileges | `sudo` to install. To *use* it you need access to the Docker socket and to the `Caddyfile` (often root-owned): run it with `sudo` or be in the `docker` group. |

> **No Docker or no Caddy yet?** You do not need to prepare anything: `sudo caddy-tui --install` installs Docker if it is missing and sets Caddy up for you
> (see [No Caddy yet?](../README.md#no-caddy-yet-let-caddy-tui-set-it-up)).

## Option 1 · apt repository (recommended)

The repository lives at `https://mgoreiro.github.io/caddy-tui/apt` (suite `stable`, component `main`) and is **signed with a GPG key**, so
apt refuses anything that does not come from it.

### Step 1 — install the signing key

```bash
curl -fsSL https://mgoreiro.github.io/caddy-tui/apt/caddy-tui.gpg | sudo tee /usr/share/keyrings/caddy-tui.gpg >/dev/null
```

This downloads the repository's **public** key and stores it in `/usr/share/keyrings/`. (Using a dedicated keyring file instead of the old `apt-key`
means the key is only trusted for *this* repository.)

Optional but recommended — check that it is the right key:

```bash
gpg --show-keys --with-fingerprint /usr/share/keyrings/caddy-tui.gpg
```

The fingerprint must be **`BA7E 2381 6880 1639 21BC  70DE 15C2 632D D390 10EE`**.

### Step 2 — add the repository

```bash
echo "deb [signed-by=/usr/share/keyrings/caddy-tui.gpg] https://mgoreiro.github.io/caddy-tui/apt stable main" | sudo tee /etc/apt/sources.list.d/caddy-tui.list
```

What the line says: *a binary package source (`deb`), whose index must be signed by the key in `/usr/share/keyrings/caddy-tui.gpg`, at that URL,
suite `stable`, component `main`.*

<details>
<summary>Prefer the modern deb822 format (<code>.sources</code>)?</summary>

Same thing, in the format newer apt versions use (Debian 12+, Ubuntu 22.04+). Use **either** this **or** the `.list` file above, not both:

```bash
sudo tee /etc/apt/sources.list.d/caddy-tui.sources >/dev/null <<'EOT'
Types: deb
URIs: https://mgoreiro.github.io/caddy-tui/apt
Suites: stable
Components: main
Signed-By: /usr/share/keyrings/caddy-tui.gpg
EOT
```
</details>

### Step 3 — install

```bash
sudo apt update
sudo apt install caddy-tui
```

`apt update` must show a line `Get: … https://mgoreiro.github.io/caddy-tui/apt stable InRelease` **without errors**. Then:

```bash
caddy-tui --version        # caddy-tui 1.x.y
sudo caddy-tui             # open the interface (see "After installing")
```

You can confirm that apt sees the repository and every available version with:

```bash
apt-cache policy caddy-tui     # shows the candidate version and where it comes from
apt list -a caddy-tui          # all published versions
```

### Tested on

Debian 12 *bookworm* and Ubuntu 24.04, on arm64 (also the index is visible from amd64 and armhf). The package is architecture independent.

## Option 2 · the `.deb` file

Every [release](https://github.com/mgoreiro/caddy-tui/releases) has the package and a `SHA256SUMS` file. Useful on machines that cannot reach the
repository, or to install one specific version:

```bash
# download both files from the release page, then:
sha256sum -c SHA256SUMS --ignore-missing      # must say: caddy-tui_X.Y.Z_all.deb: OK
sudo apt install ./caddy-tui_X.Y.Z_all.deb    # apt resolves the dependencies
```

A package installed this way is **not** updated by `apt upgrade` (apt does not know where it came from). Add the repository (Option 1) to get updates.

## Option 3 · run it from the source

caddy-tui is one file, `caddy-tui.py`, without third-party dependencies. Handy on systems that are not apt-based, or on macOS with Docker Desktop:

```bash
git clone https://github.com/mgoreiro/caddy-tui.git
cd caddy-tui
make mo               # compiles the Spanish translation (needs gettext's msgfmt); without it the interface is English only
./caddy-tui.py        # or: python3 caddy-tui.py
```

To install it by hand: `sudo make install` (puts the program in `/usr/bin`, the translation in `/usr/share/locale` and the manual in `/usr/share/man/man1`;
`PREFIX=/usr/local` changes the destination).

## After installing

```bash
sudo caddy-tui                  # the usual way: the Caddyfile is often owned by root
caddy-tui --help                # all options
man caddy-tui                   # the manual page
caddy-tui --lang es             # Spanish interface (or press L inside the program)
```

- It **detects your Caddy container by itself** (and its Caddyfile, image and docker compose project). See [Usage](../README.md#usage) to force any value.
- What the package installs: `dpkg -L caddy-tui` — the program (`/usr/bin/caddy-tui`), the manual, the Spanish catalogue and the documentation.
- What it creates later: `~/.config/caddy-tui/` (language preference), `.caddy-backups/` next to your configuration (backups), and, if you enable
  alerts, `/etc/caddy-tui/` and `/var/lib/caddy-tui/`.

## Updating

With the repository configured (Option 1):

```bash
sudo apt update && sudo apt upgrade                  # everything, including caddy-tui
sudo apt install --only-upgrade caddy-tui            # only caddy-tui
sudo apt install caddy-tui=1.7.0                     # a specific version (apt list -a caddy-tui shows them)
sudo apt-mark hold caddy-tui                         # do not update it (apt-mark unhold caddy-tui to undo)
```

**Automatic updates** with `unattended-upgrades`: allow the repository's origin by adding a file such as `/etc/apt/apt.conf.d/52caddy-tui`:

```
Unattended-Upgrade::Origins-Pattern {
        "origin=caddy-tui,codename=stable";
};
```

caddy-tui also tells you when a newer **Caddy image** exists (key `y`); that is independent of this package's updates.

## Uninstalling

```bash
sudo apt remove caddy-tui            # removes the program (keeps nothing of yours in the package)
sudo apt purge caddy-tui             # same, plus package configuration files (there are none)
sudo rm /etc/apt/sources.list.d/caddy-tui.list /usr/share/keyrings/caddy-tui.gpg      # stop following the repository
```

Your Caddy, its configuration and its data are **not touched**. Files caddy-tui created are removed by hand if you want: `~/.config/caddy-tui`,
`.caddy-backups/` folders, and — if you used alerts — `sudo caddy-tui --remove-alerts` first (removes the timer/cron job), then `/etc/caddy-tui` and `/var/lib/caddy-tui`.

## Troubleshooting

| What you see | Cause and fix |
|---|---|
| `NO_PUBKEY 15C2632DD39010EE` · `The repository … is not signed` | The key is missing or the path in `signed-by=` is wrong. Redo step 1 and check `ls -l /usr/share/keyrings/caddy-tui.gpg` (it must not be empty: if `curl` failed, the file is empty). |
| `EXPKEYSIG` · `The key has expired` | The signing key expires on 2029-10-17 (or was rotated). Run step 1 again to fetch the current key. |
| `Conflicting values set for option Signed-By` | The repository is configured twice (a `.list` **and** a `.sources` file). Keep only one. |
| `E: Unable to locate package caddy-tui` | Missing `sudo apt update`, or the source line is wrong. `apt-cache policy caddy-tui` should list the repository. |
| `404 Not Found` while updating | A typo in the URL. It must be exactly `https://mgoreiro.github.io/caddy-tui/apt` followed by `stable main`. |
| `Release file … is not valid yet (invalid for another …)` | The machine clock is wrong (typical on boards without a real-time clock after a power cut). Fix the time: `sudo timedatectl set-ntp true`, wait a moment, `sudo apt update`. |
| `Could not resolve 'mgoreiro.github.io'` | No DNS/Internet. Behind a proxy, add `Acquire::https::Proxy "http://host:port";` to a file in `/etc/apt/apt.conf.d/`. |
| `unmet dependencies … docker.io` on Ubuntu | `docker.io` is in the *universe* repository: `sudo add-apt-repository universe && sudo apt update`. |
| `You are not allowed to use Docker` when running it | Use `sudo caddy-tui`, or add your user to the `docker` group (`sudo usermod -aG docker $USER`, then log in again). |
| `Docker is installed but its service is not running` | `sudo systemctl enable --now docker` (caddy-tui offers to do it when run as root). |

Still stuck? Open an [issue](https://github.com/mgoreiro/caddy-tui/issues) with the output of `apt-cache policy caddy-tui` and `sudo apt update`.

## Key and repository reference

| | |
|---|---|
| Repository URL | `https://mgoreiro.github.io/caddy-tui/apt` |
| Suite · component · architecture | `stable` · `main` · `all` |
| Public key | `https://mgoreiro.github.io/caddy-tui/apt/caddy-tui.gpg` (binary, ready for `signed-by=`) |
| Fingerprint | `BA7E 2381 6880 1639 21BC  70DE 15C2 632D D390 10EE` (RSA 4096, expires 2029-10-17) |
| Origin / label (for apt pinning and unattended-upgrades) | `caddy-tui` |
| Web page | <https://mgoreiro.github.io/caddy-tui/> |

How the repository is built and how to publish a release: [apt-repository.md](apt-repository.md).
