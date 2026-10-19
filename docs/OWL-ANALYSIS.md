# OWL in this project: analysis and where it can be improved

Written before touching any OWL code. Source read: `backend/sources/owl-modern-source.tar.gz` (OWL
`832d70f` plus the nine omdrop-owl patches, 7261 lines of C), which is what `backend/owl-modern` is built from.
Numbers below come from this PC (MT7922, Linux 7.0.0-38) and from the captures of 18 October 2026, analysed
with `python3 -m nexaawdl analyze`; where something is inferred rather than measured it says so.

## What OWL is here

A single-threaded libev daemon. It owns three things:

* **RX**: `pcap` on the active monitor `ldack0` (radiotap frames) -> `awdl_rx()` -> action frames update the peer
  table, sync clock, election and channel sequences; data frames are converted to Ethernet and written to the
  TAP interface `awdl0`.
* **TX**: frames the kernel writes to `awdl0` are read by `poll_host_device()`. Multicast goes to a ring of
  pending frames; **unicast goes through a single slot, `state->next`**, one frame at a time. A frame is injected
  with `pcap_inject()` only while `awdl_can_send_unicast_in()` says the peer is on our channel.
* **Timers**: PSF every `af_period` (110 TU), MIF once per extended window, channel switching (off with `-F`),
  peer expiry, statistics.

Options in use (`root_bridge.py`): `-i ldack0 -c <ch> -N -S intersect -E -F [-A] [-R rate] -vv`.

| File | Role |
|---|---|
| `daemon/core.c` (773 lines) | event loop, TX/RX paths, channel adoption, statistics |
| `daemon/io.c`, `netutils.c` | pcap and TAP I/O, nl80211 channel setting |
| `src/rx.c`, `src/tx.c`, `src/frame.c` | frame parsing and building, TLVs, radiotap |
| `src/sync.c`, `src/schedule.c` | window clock, send gates and guards |
| `src/election.c`, `src/peers.c` | master election, peer table |
| `src/channel.c` | channel sequences and the strategies (`verbatim`, `pin`, `rotate`, `widen`, `intersect`) |
| `tests/` (gtest), `downstream-tests/` | 66 upstream unit tests, three harnesses added by omdrop-owl |

The build in this project is a CMake build with GCC 15 and libev linked statically; the binary uses the system
libpcap and libnl.

## What the captures say about OWL's behaviour

Measured on the 90 s capture of an iPhone sending a 2.3 MB file (5011 frames, channel 44):

* **The phone transmits to us at HT MCS7 (1598 frames) and MCS6 (282), in QoS data frames of 1466 bytes, not
  aggregated.** OWL transmits at **legacy 12 Mbit/s** (`awdl_data_tx_rate` default) in plain (non-QoS) data frames.
  Per frame the phone's airtime is about five times smaller than ours.
* **The phone uses the whole window**: its frames fall anywhere in the 64 TU slot, from 0.01 TU (1st percentile
  0.75 TU) to 63.97 TU (99th percentile 62.67 TU). OWL keeps a 3 TU guard at both ends of a slot unless the
  neighbouring slot is also shared (`AWDL_UNICAST_GUARD_TU`), which costs up to 6 of 64 TU (9 %) of an isolated
  window. Whether a smaller guard loses frames has not been measured.
* **Synchronisation is good**: the phone's window model matches its behaviour (99.7 % of 1889 frames in the slots
  its own sequence lists; our clock prediction error 0.56 TU mean). Nothing here points at a sync defect.
* **Throughput is set by the windows.** An idle iPhone is on our channel 1 slot in 16 (channel 6) or 3-4 in 16
  (channel 44); during a transfer it widens to 10 in 16 on channel 44. Receiving bulk data ran 3-4 times
  faster on channel 44 than on channel 6.
* **The monitor reads back our injected frames without a hardware timestamp**, radiotap rate or frequency, so the
  capture cannot tell OWL's own transmit timing; OWL itself has to say (see "Instrumentation").
* All interfaces of the card report `txpower 3.00 dBm`. Whether that is the real power of injected frames is not
  known; the card's regulatory limit on channel 44 is 23 dBm.

## What is *not* a defect

* Unicast frames are **not dropped** while the peer is away: `awdl_send_unicast()` re-arms until the gate opens.
  (The earlier "drop frame to non-peer" loss, caused by peers expiring mid-upload, is already fixed in this build.)
* The election, the sequence strategies and the peer table behave as intended for a phone that is always master.

## Candidate improvements, ranked

| # | Change | Why | Evidence | Risk |
|---|---|---|---|---|
| 1 | **Transmit at an HT rate** (`-R mcs7`, existing option) | five times less airtime per frame in every window we get | the phone itself uses MCS7 towards us at the same distance | link margin at the phone unknown; legacy 36 was *never* judged (every run failed at the tail for another reason, since fixed) |
| 2 | **Tunable unicast guard** (`-G`) | up to 9 % of an isolated window | phone frames at 0.75 TU and 62.7 TU | frames cut off at an edge |
| 3 | **Instrumentation**: per-slot send counts, gate open/shut, frames per window, sync error rate in `STATS` | the next live test can say *where* time is lost | the capture cannot show it | none |
| 4 | **TX A-MSDU aggregation** (QoS data, several frames per MPDU) | once the rate is high the fixed cost per frame dominates | the phone sends QoS data and accepts our frames; OWL already *receives* A-MSDU | the phone may not accept A-MSDU from us: needs a live test |
| 5 | **A real unicast queue** instead of the single `next` slot | needed by #4, and to coalesce stale TCP ACKs | tail stalls were caused by a lost last ACK | moderate: replaces the TX path |
| 6 | **TX power check/raise** in the bridge | a 3 dBm injection would cap the usable rate | `iw` reports 3 dBm | the driver may ignore or refuse it |

The first experiment needs no code: send a few MB from the PC to the iPhone at the default rate and at
`--data-rate mcs7` and compare the `TCP upload` line in the log. It decides how much of 2, 4 and 5 is worth doing.

## Status

Implemented in `owl-nexa` ([`OWL-NEXA.md`](OWL-NEXA.md)): #2 (guards, option `-G`, default unchanged), #3
(instrumentation), and one change found while reading the code that the table did not list: **the RSSI filter judged
each frame alone at -65 dBm and refused 9.4 % of the iPhone's action frames**; it now judges a running average with a
-80 dBm threshold. #1 needs no code (`--data-rate mcs7`); #4-#6 wait for live measurements.

## How a customised OWL would be integrated

* Source under `backend/owl-src/` with a plain `Makefile` (no CMake), the unit tests, and a build script that
  reproduces the shipped binary first; changes land as separate, reviewable patches.
* A second binary, `backend/owl-nexa`, selected by a setting; `owl-modern` stays the default and the fallback: if the
  variant fails to start, the bridge starts `owl-modern` and says so.
* Every new behaviour is behind an option whose default is today's behaviour, until a live A/B test shows it is
  better on this card.
