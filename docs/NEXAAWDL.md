# NexaAWDL Engine

An AWDL engine written for this project (`nexaawdl/`), integrated into the existing app. This page says what it
does, what it deliberately does not do yet, how it is built, and what has and has not been verified. Nothing
below claims interoperability that was not measured.

## Scope, stated plainly

NexaAWDL **observes and analyses** the AWDL air. It receives frames from a monitor interface, decodes and
validates them, follows each peer's availability-window clock, tracks peers, tells *AWDL presence* from
*AirDrop availability*, and publishes the result to the window. It **does not transmit**. Sending AWDL frames and
carrying the IPv6 data path stay with OWL, which AirDrop already depends on, because injection from a monitor
interface has not been verified for a new engine on this hardware. `capabilities()` in `engine.py` says so and
the window shows it. Replacing OWL is possible later; the steps are at the end of this page.

## Phase 1: what the repository already had

| Area | Implementation |
|---|---|
| Languages / frameworks | Python 3.14 (GTK 4 + libadwaita front end, privileged bridge, receiver), C/C++ (OWL, vendored), shell (packaging) |
| Discovery | OWL (AWDL) on `ldack0`/`ldmon0`; a BLE advertisement script (`backend/blewake.py`, BlueZ); mDNS through zeroconf inside OpenDrop |
| Wireless interfaces | `root_bridge.py`: a passive monitor `ldmon0`, an active monitor `ldack0`, the AWDL tap `awdl0` made by OWL; the managed parent is kept up and out of NetworkManager |
| Wi-Fi Direct | `p2p_anchor.py`: a temporary P2P group (channel 6 or 44) that *holds the radio on one channel*. It is an anchor for the stock driver, not a replacement for AWDL |
| AWDL components | OWL only (`backend/owl-modern`, GPL-3.0) |
| mDNS / Bonjour | zeroconf; the announcement interface is selected by index (the earlier fix: records left through the Wi-Fi Direct group and the phone never saw them); records live 120 s and are re-announced every half second |
| IPv6 | `awdl0` link-local address derived from OWL's MAC; IPv6 switched off on the Wi-Fi Direct group; firewall rule for TCP 8771 on `awdl0` only |
| File transfer | vendored OpenDrop client/server with 17 patches; `secure_receiver.py` (consent before every receive, archive checks, no overwrites) |
| Services | polkit/pkexec (optional passwordless root-owned helper), BlueZ, NetworkManager, a private `wpa_supplicant` |
| Authorisation | the bridge runs as root only for the radio; the HTTPS server and every file operation run as the user; every receive asks |
| Logging | per-session logs in `~/.local/state/linux-airdrop/`, the *Registro* window, a diagnostics page |
| Settings | `~/.config/linux-airdrop/settings.json` (validated on load) |

Reused as is: the bridge, OWL, Wi-Fi Direct anchor, OpenDrop, the receiver, settings and diagnostics. The
Wi-Fi Direct default, the channel handling and the announcement-interface fix are untouched.

### The hardware (read from this machine, 18 October 2026)

`python3 -m nexaawdl hardware` prints it. For this PC (HP Omen 40L):

* MediaTek **MT7922** 802.11ax, PCI `14c3:0616` at `04:00.0`, driver `mt7921e`, firmware `____000000-20260224103233`,
  kernel `7.0.0-38-generic`; NetworkManager sees `wlp4s0` as *disconnected*; rfkill: neither soft- nor hard-blocked.
* Modes: managed, AP, monitor, P2P-client, P2P-GO, P2P-device. **Active monitor** supported (acknowledges frames
  addressed to it). TX-status socket supported.
* Channels: 2.4 GHz 1-14, 5 GHz 36-177, 6 GHz. The social channels 6, 44 and 149 are all listed and unrestricted in
  the current regulatory domain (IT).
* Interface combinations: `#channels <= 2`; **monitor is not part of any listed combination**: monitor interfaces are
  added as software interfaces. This is a fact about the driver's advertised table, not a statement about whether
  it works: the app's existing sessions do run monitors next to the Wi-Fi Direct group.

What this does **not** establish, and the report says so under `not_verified`: that frames can be *injected* from
a monitor interface by a new engine, that the active monitor acknowledges an Apple device's unicast frames, that a
participant (not just an observer) can stay synchronised, and AirDrop interoperability with a given device and OS.
Monitor mode and injection do not by themselves prove AWDL compatibility.

## Architecture

```
root bridge (root)                          NexaAWDL process (desktop user, no privileges)
 opens an AF_PACKET socket on ldmon0 ──fd──▶ capture.SocketSource
 starts the engine as the user               dot11.split_capture   radiotap + 802.11, bounds-checked
 restarts it ≤ 3 times, then gives up        frames.parse_action   AWDL action frames and TLVs
 stops it first at cleanup                   engine.Engine         state machine, dispatch, metrics
                                             ├ peers.PeerTable     presence, sequences, AirDrop flag
                                             ├ sync.AwClock        window / slot model per peer
                                             ├ election            sync-tree election
                                             └ transport           IPv6, UDP, mDNS (AirDrop announcements)
                                             run.py ─▶ status.json (1 s) + events.jsonl (capped)
window (GTK) ◀── nexaawdl/view.py ◀──────────┘
```

* **Least privilege.** The only privileged act is opening the packet socket (`root_bridge.open_monitor_socket`).
  The engine then runs as the desktop user (`Popen(user=, group=, extra_groups=[])`), with the descriptor inherited,
  `no_new_privs` set, core dumps off, no network of its own. Its state directory must be a real directory owned by
  the app's user (not root, not a symlink); the engine's code comes from the app's install directory and runs
  after the privileges are dropped. No new file is executed as root, so the root-owned helper's list of files is
  unchanged apart from `root_bridge.py` itself.
* **Isolation / fallback.** A failure to start, a crash or a hang never reaches OWL, the Wi-Fi Direct group or a
  transfer: the bridge logs it and carries on. The setting *Attivazione automatica* switches it off.
* **Local API.** `status.json` (a complete snapshot, rewritten atomically every second), `events.jsonl` (one
  generation of history kept, 1 MB cap) and `python3 -m nexaawdl status|analyze|hardware`. The window reads the
  snapshot; a snapshot older than 6 s is shown as "the engine is not responding".
* **Hostile input.** Every length is checked before every read; TLV count and size, peer-table size (64), DNS
  names and compression pointers are bounded; the only exception for bad bytes is `MalformedFrame`; the engine
  counts and logs rejects instead of stopping. Unknown TLVs are kept verbatim, never guessed.

### Modules

| File | Role |
|---|---|
| `nexaawdl/frames.py` | AWDL action-frame and TLV codec (sync parameters, channel sequence, election v1/v2, version, arpa, data-path state, service parameters, sync tree), data-frame header |
| `nexaawdl/dot11.py` | radiotap and 802.11 header decoding, AWDL BSSID filter |
| `nexaawdl/sync.py` | availability-window clock (16 TU window, presence mode, 16-slot cycle, 16-bit counter wrap), clock offset, sequence overlap |
| `nexaawdl/election.py` | master / sync-tree election (metric-first as Apple, counter-first as the older rule) |
| `nexaawdl/peers.py` | peer table, expiry, sequence changes, AirDrop flag kept apart from presence |
| `nexaawdl/transport.py` | link-local rule, IPv6/UDP, bounded mDNS reader, traffic classes |
| `nexaawdl/hwprobe.py` | read-only hardware identification and the assessment (facts / inferences / limitations / not verified) |
| `nexaawdl/capture.py` | pcap and socket sources, the one privileged helper |
| `nexaawdl/engine.py` | lifecycle, dispatch, metrics, status |
| `nexaawdl/run.py` | the engine process |
| `nexaawdl/view.py`, `ui_nexaawdl.py` | texts and the *NexaAWDL* page: state, wireless, devices, diagnostics, settings |

## What is verified

| Claim | How | Result |
|---|---|---|
| Action-frame header decodes | 6192 PSF + 733 MIF from a real iPhone in a 12-minute capture (`python3 -m nexaawdl analyze`; 12 of the frames are kept, anonymised, in `tests/fixtures/nexaawdl/real_frames.json`) | all decoded, 0 malformed |
| Peers tracked | same capture: the iPhone (11 149 frames) and a second device that sent 8 | both entered the table |
| Peer clock vs ours | `phy_tx - tsft` over 6864 frames / 729 s | within -0.57..+0.13 ms of the median, 0.5 ppm drift, one isolated 2^22 µs jump |
| TLV decoding on real frames | a full-length capture (`tcpdump -s 0`, 90 s, 5011 frames, an iPhone transferring a 2.3 MB PNG to the PC) | 313 MIF action frames, all decoded with every TLV; **every one re-encodes byte for byte**; 0 malformed |
| Synchronisation model on real frames | same capture, the phone's sync parameters | 100 % of the last predictions within 3 TU, mean absolute error 0.56 TU (313 observations, 6 early misses while locking) |
| Window model on real frames | same capture: each of the phone's 1889 data frames checked against the slot its own channel sequence lists for the channel it arrived on | **99.7 % consistent** |
| AirDrop announcement detection | synthetic mDNS only: in the real capture the iPhone only *asked* for `_airdrop._tcp` (it was not receiving) | the announcement path is untested on real data |
| Hardware report | run on this PC; parsers also tested on recorded `iw`/`lspci`/`ethtool`/`nmcli` output | correct identification |
| Isolation and supervision | tests with a real child process, a real inherited socket, bounded restarts, ordered cleanup | pass |
| Existing AirDrop behaviour | the whole earlier suite | unchanged |

Facts learned from real frames, now in the code and in `tests/test_nexaawdl_real.py` (an anonymised 25 s excerpt,
`tests/fixtures/nexaawdl/real_full.pcap`):

* The sync-parameters TLV embeds a channel list in the *legacy* `{flags, channel}` encoding that names the 40 MHz
  **centre** channels (44 appears as 46, 149 as 151, 100 as 102). The separate channel-sequence TLV (18) lists the
  **primary** channels in `{channel, opclass}` form, which is what OWL logs and what the window check uses. They
  are kept apart (`sequence` and `sync_sequence`).
* A real phone also sends TLVs 2 (service response, with the `_companion-link` name), 7 (HT capabilities), 17 and
  33; they are preserved as unknown TLVs. The data-path-state, service-parameters and sync-parameters TLVs carry
  trailing fields that the codec keeps verbatim, so decoding and re-encoding is lossless.
* The monitor reads back **our own injected frames** (785 of 5011 here). The engine drops frames from the
  interface's own address instead of showing the PC as a peer.
* The phone changes its channel sequence as it gets busy (`100,0,149,…` idle, `44,44,149,…,6,44,44` during a
  transfer).

Bugs found by running it and fixed: the announced AW counter is 16 bits and wraps, so comparing it without modular
arithmetic reported a false error at the wrap; and mixing the radio's hardware timestamps with the wall clock made a
live peer look lost every few seconds.

## Limits

* **Hardware.** One channel at a time with infrastructure (`#channels <= 2` in the table, monitor outside it); the
  card cannot follow a peer's hopping sequence the way Apple's own radio does, so only the slots in which a peer is
  on our channel are usable (about 1 in 16 for an idle iPhone; see `SENDING.md`).
* **Software.** No transmit path yet; no AWDL data path of its own; election is implemented and tested but only
  observed, not participated in; AirDrop-announcement detection is unchecked on real mDNS responses.
* **Policy.** The social channels depend on the regulatory domain; the report flags NO-IR / DFS / disabled.

## Using it

```sh
python3 -m nexaawdl hardware                       # identify the card, assess it (read-only)
python3 -m nexaawdl analyze capture.pcap --own MAC # replay a monitor capture (--own: our AWDL address)
python3 -m nexaawdl status                         # the running engine's snapshot
```

Run from the install directory (or with `PYTHONPATH` set to it). To record a capture the engine can use, with the
PC visible: `sudo tcpdump -i ldmon0 -s 0 -w capture.pcap` (the default snapshot length of a short `-s` cuts the
TLVs off). In the window, *NexaAWDL* shows state, wireless, devices (AWDL presence and AirDrop announcement
separately), diagnostics and settings.

## Steps still needed for a native engine (not done)

1. Build PSF/MIF frames and inject them from a monitor interface on this firmware; confirm with a second capture
   that an Apple device reacts (peer table, election).
2. Replace OWL's TAP data path with a native one (IPv6 over AWDL data frames, unicast gated on slot overlap),
   keeping OWL as the fallback.
3. Only then offer a mode in which NexaAWDL, not OWL, owns `awdl0`, and test file transfers with real devices.
