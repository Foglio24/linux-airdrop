# Linux AirDrop

Native **AirDrop between Linux and iPhone/iPad** for PCs with a **MediaTek MT7921/MT7922** Wi-Fi card. The PC appears in the **Share → AirDrop** sheet of an iPhone or iPad and can receive files from it, and send files to it. It speaks AWDL (Apple Wireless Direct Link) through [OWL](https://github.com/jedbillyb/owl) and the AirDrop protocol through a hardened [OpenDrop](https://github.com/seemoo-lab/opendrop), with a GTK 4 / libadwaita front end, a tray icon, background mode and login autostart.

> **Language: the app's user interface is currently Italian only.** Buttons, menus, notifications and in-app messages are in Italian; this documentation is in English. A translation is planned. The UI labels quoted below are given in Italian with their English meaning.

> **Experimental software.** It takes the Wi-Fi card over exclusively while it is visible and, on kernels whose driver lacks active monitor, temporarily replaces the Wi-Fi driver modules. It has been tested on **two systems with one MT7922 each**: Ubuntu 26.10 with kernel 7.3.0-9-generic (patched modules) and AnduinOS 2.0.4 with kernel 7.0.0-38-generic (stock driver), with one iPhone on iOS 27 and one iPad. A kernel lock-up is possible; a reboot always restores the original modules. Use it at your own risk.

## Status

Working on the tested machine, in both directions: an iPhone sends files to the PC (`/Discover`, `/Ask`, `/Upload`), and the PC sends files to the iPhone. The iPhone can stay on **Receiving Off** (*Ricezione disattivata*) while sending to the PC. See [`verification.json`](verification.json) for what was checked, what was not, and the known limitations.

### What 1.3.1 changes: OWL core rewrite

The app now includes `owl-nexa`, a refactor of OWL's receive, transmit, I/O and command-line paths. It validates
frames before updating peer state, forwards exactly the decoded Ethernet length, preserves queued packets when the
radio is busy, pauses host reads when queues are full, and shuts down cleanly. **OWL ottimizzato** selects it in
*Visibilità*; `owl-modern` remains available as the startup fallback. The existing passwordless helper is refreshed
on package upgrade.

Verification: 130 OWL tests, three downstream C harnesses, 121 application integration tests, full AddressSanitizer
and UndefinedBehaviorSanitizer checks, and recorded radio traffic replay. **No live iPhone/iPad transfer has yet
been performed with this rewritten binary.** Details: [`docs/OWL-NEXA.md`](docs/OWL-NEXA.md) and
[`docs/OWL-VERIFICATION.json`](docs/OWL-VERIFICATION.json).

### What 1.3 adds: NexaAWDL

A new engine, `nexaawdl/`, observes and analyses the AWDL air next to OWL: it decodes the frames, follows each peer's
window clock, tells AWDL presence from AirDrop availability and shows it all in a new **NexaAWDL** page (state, wireless,
devices, diagnostics, settings). It runs without privileges, never touches OWL or a transfer, and **does not transmit**
yet. What is verified and what is not: [`docs/NEXAAWDL.md`](docs/NEXAAWDL.md). `python3 -m nexaawdl hardware` identifies the
card; `python3 -m nexaawdl analyze capture.pcap` replays a capture.

### What 1.2 changes

- **Stock driver support, chosen by capability, not by kernel version.** If the running `mt7921e` already offers active monitor and Wi-Fi Direct (P2P-GO), the app uses it as is: the privileged bridge asks nl80211 for `ACTIVE` + `SKIP_TX` on its own monitor before bringing it up, keeps the managed parent interface up while the monitors exist, and refuses a session it cannot clean up safely. The patched modules remain the fallback, only for the exact kernel they were built for. See [`docs/KERNEL-COMPATIBILITY.md`](docs/KERNEL-COMPATIBILITY.md) and [`docs/ANDUINOS.md`](docs/ANDUINOS.md).
- **Wi-Fi Direct on channel 6 or 44 (5 GHz).** The channel is checked against the driver's regulatory flags (no NO-IR/DFS/radar channels) before anything changes. Channel 36 was dropped: the iPhone never uses it and never found the PC there. On channel 44 the bulk of a receive ran 3-4 times faster than on channel 6; discovery can take a little longer.
- **Receiving from the iPhone/iPad no longer stops at 97-99 %.** The phone stopped once our last TCP acknowledgements were lost, because it listens on our channel one slot in sixteen at the end of a transfer and TCP never repeats a pure ACK. The receiver now sends TCP keepalive probes (each carries the current ACK) every second while the sender is quiet. The same 2.85 MB file that stopped at 91 % arrived whole in about 12 s on channel 44. Measurements and captures: [`docs/SENDING.md`](docs/SENDING.md).
- **Diagnostics for transfers.** The receiver logs the upload headers and, when a body ends early, where in the chunked framing it stopped and the last bytes read; the sender logs TCP counters for the upload only and shows *in attesa della conferma* (waiting for confirmation) once the body is sent. The receive read timeout is 120 s.
- **Keep the defaults:** radio acknowledgements on (*Conferme radio*) and the default data rate. Both turning acknowledgements off and the experimental `--data-rate 36` made receives fail at the tail.

### What 1.1 adds

All of it is documented in [`docs/FEATURES.md`](docs/FEATURES.md). The radio code, the driver modules and the privileged helper are unchanged from 1.0.

- **Transfer history** (*Cronologia*): device, direction, files and kinds, size, date, outcome, buttons to open the file or folder, clear the list. SQLite, private, no file contents.
- **Drag and drop** of files and folders onto the window, with a highlighted drop area; folders as a ZIP (default) or natively (experimental).
- **Preview before accepting**: file names, type icons, sizes when the sender gives them, and the sender's thumbnail, decoded in a restricted process. Nothing received is ever opened.
- **"Invia con Linux AirDrop"** in GNOME Files (files, folders, selections) and a single running instance, so no second session starts.
- **Live progress** per transfer (percent, MB, MB/s, time left, state), from real byte counts; cancel; several transfers.
- **Remembered devices** (*Dispositivi*), with a note on how sure the identification is.
- **Diagnostics** (*Diagnostica*) with green/yellow/red checks and a copyable, anonymised report; **guided repair** with attempt limits and a log.
- **Trusted devices / automatic accepting: not implemented, on purpose.** AirDrop gives the receiver no identifier that cannot be forged or replayed, so every transfer still asks. See [`docs/TRUSTED-DEVICES.md`](docs/TRUSTED-DEVICES.md).

Sending large files to the iPhone used to stall after a few hundred kB: frames to the phone went out without radio acknowledgements. 1.1 turns them on (*Visibilità → Conferme radio*) and makes the sender's TCP more persistent; the analysis, with measurements from a link emulator, is in [`docs/SENDING.md`](docs/SENDING.md) (not yet confirmed with the phone).

What was and was not tested is in [`docs/TESTING.md`](docs/TESTING.md). Short version: everything is covered by automated tests, including a real sender talking to a real receiver in a private network namespace; 1.2 was also tried with a real iPhone and iPad (receiving several files; sending only small files). The GNOME Files menu entry has not been run inside a real Files because `python3-nautilus` is not installed on the development machine.

## Requirements

- Ubuntu 26.10 on amd64 (CPython 3.14, GTK 4, libadwaita), GNOME. The tray icon needs the *AppIndicator* shell extension, enabled by default on Ubuntu.
- A MediaTek **MT7921/MT7922** Wi-Fi card (driver `mt7921e`) and **one** Wi-Fi card in the system.
- Either a **stock driver with active monitor and P2P-GO** (for example Linux 7.0.0-38-generic on AnduinOS 2.0.4; the app checks the capabilities itself), used with Wi-Fi Direct on channel 6 or 44; or kernel **7.3.0-9-generic** for the bundled driver modules. Ubuntu's 7.3 kernel disables *active monitor* for MT792x, which AirDrop needs, so there the app loads three patched modules (`mac80211`, `mt792x-lib`, `mt7921e`) built from the exact Ubuntu 7.3.0-9.9 sources. On a kernel that offers neither, the app tells you so before touching the network.
- Ethernet is recommended: the Wi-Fi card is used exclusively while the PC is visible.
- BlueZ, NetworkManager, `iw`, `ip`, `wpa_supplicant` and `pkexec` (all present on a default Ubuntu desktop).

## Install

### Debian package (recommended)

Download the latest `linux-airdrop_<version>_amd64.deb` from the [Releases](https://github.com/Foglio24/linux-airdrop/releases) page and install it:

```sh
sudo apt install ./linux-airdrop_<version>_amd64.deb
```

Then open **Linux AirDrop** from the application menu. For the "Invia con Linux AirDrop" entry in GNOME Files install `python3-nautilus` (the package recommends it) and run `nautilus -q` once. If an older version is running, quit it first (**Esci**): the new one cannot hand files to a version that predates this feature. Removing the package (`sudo apt remove linux-airdrop`) also removes the optional passwordless helper described below. Files you received stay in your Downloads folder.

### From source, for the current user (no sudo)

```sh
git clone https://github.com/Foglio24/linux-airdrop
cd linux-airdrop
python3 install-user.py            # add --autostart to start it at login; --no-files-menu skips the GNOME Files entries
```

This copies the program to `~/.local/share/linux-airdrop` and adds the launcher `~/.local/share/applications/it.foglio.LinuxAirDrop.desktop`. You can also run it in place with `./start.sh`. To uninstall, quit the app and delete those two paths.

The Python libraries the protocol needs are bundled under `backend/python`.

## Usage

1. Open **Linux AirDrop** from the application menu.
2. Keep the PC on Ethernet with Wi-Fi disconnected. Press **Rendi visibile** (*Make visible*). With the stock kernel the app loads the temporary driver fix by itself. Ubuntu asks for your password in an authentication dialog, unless you set up the passwordless helper (below).
3. On the Apple device turn Wi-Fi and Bluetooth on, unlock it and open **Share → AirDrop**. To **send from the iPhone to the PC** the iPhone does not need to be visible: it can stay on **Receiving Off**. *Everyone for 10 Minutes* is only needed if you want the iPhone to receive from the PC.
4. Choose the PC's name and accept the request on the PC (every receive asks for confirmation in a separate window).
5. Files land in `~/Downloads/LinuxAirDrop` (or your configured Downloads folder), one folder per transfer. **Interrompi visibilità** (*Stop visibility*) gives the Wi-Fi card back to the system.

To **send from the PC**: with a session running press **Invia file…** (*Send files…*), choose the files, then the recipient. The phone must be unlocked, receiving set to Everyone, and the Share sheet closed.

You can also **drag files or folders onto the window**, choose **Invia con Linux AirDrop** from the context menu of GNOME Files, or run `linux-airdrop FILE…`; if the PC is not visible yet the app asks before turning AirDrop on. `linux-airdrop --diagnose` prints the diagnostics report in a terminal.

If the PC does not show up, leave the AirDrop sheet open for about a minute. In **Visibilità** (*Visibility*) you can change the Wi-Fi channel (6, or 44 for faster transfers) and the Wi-Fi Direct option. **Registro** (*Log*; menu ☰ or Ctrl+L) shows what is going on and has a copy button; every session is also saved under `~/.local/state/linux-airdrop/`.

## Interface, tray icon and background

The interface uses GTK 4 and libadwaita: it follows the GNOME style, light/dark theme and system shortcuts. Settings (name, channel, Wi-Fi Direct, duration, start-up options, remembering transfers, automatic repair) are stored in `~/.config/linux-airdrop/settings.json`; history and repair state are in `~/.local/state/linux-airdrop/`.

- **Tray icon.** The logo appears in the top bar: brighter while the PC is visible, dimmed otherwise. Its menu has *Apri Linux AirDrop* (open the window), the state, *Rendi visibile / Interrompi visibilità* and *Esci* (quit).
- **Background.** With **Applicazione → Continua in background** on (default), closing the window stops nothing: the app stays in the tray, and a visible PC keeps receiving, with a notification that has an **Interrompi** (*Stop*) button. Reopen the window from the tray icon or by launching the app again. Receive requests and received files arrive as notifications; every receive still asks for confirmation. If a session ends because of an error or a timeout while the window is closed you get a notification and the app stays in the tray. Without a tray icon, closing the window with no session running quits the app. **Esci** (menu or Ctrl+Q) always stops the session and quits. With the option off, closing the window means quit.
- **Visibility duration.** 25 minutes (default), 2 hours, or *Finché non la interrompo* (until I stop it). The Wi-Fi card stays in exclusive use for the whole time.
- **Start at login.** *Avvia al login* creates `~/.config/autostart/it.foglio.LinuxAirDrop.desktop`, which runs `start.sh --background` (no window, tray icon only). *Rendi visibile all'avvio* (*Make visible at start*) also turns visibility on, but only if the passwordless helper is configured, otherwise the password prompt would appear at every login. For a PC that is always ready, also pick the *until I stop it* duration. From a terminal: `python3 install-user.py --autostart` (or `--no-autostart`).

## Passwordless activation (root-owned helper)

The program that drives the Wi-Fi card has to run as root. While it lives in your home folder Ubuntu asks for the password every time. Removing that prompt while leaving the file where any program running as you can rewrite it would hand root to every process of your user. So passwordless activation uses a **root-owned copy**.

Set it up **once**, with the administrator password, from **Applicazione → Attivazione senza password → Configura…** (*Application → Passwordless activation → Configure…*) or from a terminal:

```sh
sudo python3 /usr/lib/linux-airdrop/system_helper.py install        # Debian package
sudo python3 ~/.local/share/linux-airdrop/system_helper.py install  # per-user install
```

It installs this, and nothing else:

- `/usr/local/lib/linux-airdrop/`: a root-owned copy of only the files root executes (`root_bridge.py`, `p2p_anchor.py`, `awdl_diag.py`, OWL and the three driver modules), writable by nobody else, plus the small entry script `airdrop-bridge` and a list of hashes.
- `/usr/share/polkit-1/actions/it.foglio.LinuxAirDrop.policy`: declares the action and ties the authorisation to the `airdrop-bridge` path only (not to `python3`, which would be a root shell).
- `/etc/polkit-1/rules.d/49-linux-airdrop.rules`: lets **only your user**, from an active local session, start that action without a password. Other users still have to authenticate as administrators.

The app only uses the helper when it is *ready*: root-owned, not modifiable, with hashes identical to the bundle's. If anything does not match (*Da aggiornare*, outdated after an app update, or *Non sicura*, unsafe) it falls back to asking for the password. Use **Aggiorna…** (*Update…*) after the root-side files change. At worst, a program running as you with this permission can turn AirDrop on and off, and so take the Wi-Fi card away and give it back; it cannot run arbitrary commands as root. To remove everything: **Rimuovi** (*Remove*) on the same row, or `sudo python3 …/system_helper.py uninstall`. Terminal status: `python3 system_helper.py status`.

## Wi-Fi Direct (default mode)

With **Visibilità → Wi-Fi Direct** on (default; channel 6, or 44 if the regulatory domain allows a group owner there) the app creates a temporary Wi-Fi Direct group and gives its address to the two monitor interfaces. With monitor interfaces only, the phone saw the PC but AirDrop connections stayed pending; with the Wi-Fi Direct group the card holds the channel and transfers complete, as other MT7922 users reported ([report](https://github.com/t4t5/omdrop-owl/pull/24), [proposal](https://github.com/t4t5/omdrop-owl/pull/55)).

The Wi-Fi card must be disconnected from any network; Ethernet may stay connected. The phone does not join any new network. From a terminal: `./start.sh --activate` (Wi-Fi Direct) or `./start.sh --no-go-anchor --activate` (monitors only).

The app checks that no group exists on the card and starts its own `wpa_supplicant` with a private control directory, without touching the system one. It removes the group, the monitors and the process when done, then restores the original modules. If a session fails, the last lines of `wpa_supplicant` appear in the log (the group passphrase is hidden).

Things found along the way, for anyone debugging something similar:

- The Wi-Fi Direct group and `awdl0` share one MAC address and therefore one IPv6 link-local address. OpenDrop used to hand that address to zeroconf, which picked the lowest interface index, the group, so the `_airdrop._tcp` announcements left through the Wi-Fi Direct group and the phone never saw them. The interface is now selected by index and IPv6 is switched off on the group.
- NetworkManager never lists Wi-Fi Direct group interfaces, so `nmcli device set … managed no` failed with "Device not found" and aborted an otherwise working group. That step is now optional.
- The AirDrop instance name is random on every run, and the library default keeps the service records for 75 minutes, so a phone showed one entry per earlier session. The records now live for 120 seconds and the receiver re-announces every half second (an idle iPhone listens on our channel for one slot in sixteen, so slow announcements were only heard by chance).

## How it is built

- `app.py`: GTK 4 / libadwaita front end. `tray.py`: StatusNotifierItem and dbusmenu tray icon spoken directly over D-Bus. `confirm.py`: the confirmation window for every receive.
- `root_bridge.py`, `p2p_anchor.py`, `awdl_diag.py`: the small privileged bridge (newline-delimited JSON on stdout, `stop` on stdin) that prepares the card, loads the driver modules, creates the group and the monitors, runs OWL, and restores everything. No HTTP server runs as root.
- `secure_receiver.py`: hardening around OpenDrop's server (consent before every receive, archive path checks, no overwrites, incomplete transfers are not reported as complete), plus the progress/cancel events (`transfer_events.py`) and the preview of the request (`preview.py`, `identity.py`).
- `store.py` (SQLite history and remembered devices), `progress.py` (speed and time left from real counters), `outgoing.py` (what may be sent, ZIP of folders), `fileinfo.py` (kinds, safe text, number formats).
- `diagnostics.py` and `repair.py`: the health checks, the report, the known faults and the attempt limits. `ui_history.py`, `ui_devices.py`, `ui_diagnostics.py`, `ui_transfers.py`: the pages of the window.
- `nautilus/`: the GNOME Files extension and script.
- `radio_compat.py`: decides between the stock driver and the patched modules from the card's capabilities.
- `docs/`: [features](docs/FEATURES.md), [sending and receiving over AWDL](docs/SENDING.md), [kernel compatibility](docs/KERNEL-COMPATIBILITY.md), [the AnduinOS installation](docs/ANDUINOS.md), [trusted devices](docs/TRUSTED-DEVICES.md), [testing](docs/TESTING.md).
- `system_helper.py`, `autostart.py`: the passwordless helper and the login entry.
- `backend/`: OWL (`owl-modern`, plus the original `owl`), the vendored OpenDrop and its Python dependencies, the BLE advertisement script, and `driver-compat/` with the three kernel modules, their patches and sources.
- `packaging/`: the script that builds the `.deb` (`packaging/build-deb.sh`).
- `tests/`: automated tests (run each file with `python3 -I -B tests/test_<name>.py`, or all with `tests/run-all.sh`; the GUI, tray and single-instance tests need a desktop session, the loopback and interface tests need `unshare -Urn`).

## Behaviour and checks

Ubuntu's 7.3.0-9 kernel disables active monitor for MT792x drivers. The app detects that limit before touching the network. The test mode includes three modules built from the exact Ubuntu 7.3.0-9.9 sources: they re-enable the support for the MT7922 only and apply the published fixes for channel handling, filter restore and 5 GHz packet rates ([channel fix](https://marc.info/?l=linux-wireless&m=178823162355132&w=4), [filter fix](https://lists.openwall.net/linux-kernel/2026/09/04/2809)).

The test modules are loaded in memory for the session; the originals on disk are never replaced. At the end the app reloads the original modules and checks their versions; a kernel lock-up can require a reboot, and a reboot always uses Ubuntu's original modules. This mode is unavailable after a kernel update: the modules have to be rebuilt for the new version.

The bundled `owl-modern` engine includes the nine patches of [omdrop-owl](https://github.com/t4t5/omdrop-owl), with sync-master election by metric (`-E`). By default it uses a fixed channel (`-F`, 6 unless you pick 44) and the `intersect` strategy, which announces the overlap with the Apple device's channels. Pick the channel in the UI or with `--channel 44`; `--channel-hopping` enables an experimental alternative. The Bluetooth advertisement uses the current OmDrop format, without contact hashes.

The password is needed to set up the radio link temporarily; no passwordless sudo rules or permanent services are added (the optional polkit helper above is the only exception, and it is opt-in). The HTTPS server and all file reads/writes run as a normal user. The privileged bridge only manages the selected Wi-Fi card and its own interfaces; it saves and restores the previous state and does not stop NetworkManager.

Every receive requires confirmation. An upload without approval is rejected. Archive names are validated, existing files are never overwritten, and incomplete transfers are not reported as complete. A session ends after the chosen duration (25 minutes by default). The experimental link can be slow, especially for videos and large files.

## Credits and licences

The app's own files, OWL and OpenDrop are distributed under **GPL-3.0** (see [`LICENSE`](LICENSE)). The kernel modules keep their GPL-2.0 / BSD licences, with sources, patches and manifest in `backend/driver-compat`. Sources, versions and licences of the other components are in `backend/manifest.json`, `backend/sources` and `backend/licenses`; Python libraries carry their licences in their `*.dist-info` folders.

Built on [OWL](https://github.com/jedbillyb/owl), [OpenDrop](https://github.com/seemoo-lab/opendrop), [airdrop-mt7921](https://github.com/jedbillyb/airdrop-mt7921) and [omdrop-owl](https://github.com/t4t5/omdrop-owl), and on the published research on AWDL and AirDrop. Research on iOS changes is not a proof that it works on your device. Not affiliated with or endorsed by Apple; AirDrop is a trademark of Apple Inc.
