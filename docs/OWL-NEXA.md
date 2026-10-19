# owl-nexa: the customised OWL

`backend/owl-nexa` is OWL built from the sources in `backend/owl-src/` (OWL `832d70f` + the nine omdrop-owl patches,
which is what `owl-modern` is built from, plus the changes below). It is a **separate binary**: `owl-modern` stays
in the package and is the automatic fallback. The analysis that led here, with the measurements, is in
[`OWL-ANALYSIS.md`](OWL-ANALYSIS.md). GPL-3.0, like OWL.

## What changed, and why

### 1. The RSSI filter judges a running average (default behaviour change)

Upstream OWL refuses a **new** peer whose action frame reads below -65 dBm and, for a known peer, refuses **every
frame** below -70 dBm, one frame at a time. The frames it refuses are the ones that carry the synchronisation
parameters, so they cost sync updates and peer refreshes.

Measured on this PC (MT7922, an iPhone about a metre away, 1388 action frames): median -52 dBm, but **9.4 % of
the frames read below -65 dBm and 6.5 % below -80 dBm (down to -94)**: the phone had not moved. The old filter
threw all of them away.

Now (`src/rx.c`, `src/peers.c`): the threshold is **-80 dBm** and a known peer is judged by a **running average of
its frames (weight 1/8)** with the same 5 dB grace, so a single deep fade neither drops the frame nor its sync
information, while a peer that really leaves is refused after a few frames (tested: fewer than 40). `-r <dBm>`
sets the threshold, `-f` still switches the filter off, `-I` restores upstream's frame-by-frame judgement, and
`-DOWL_UPSTREAM_RSSI` at build time restores the upstream numbers.

### 2. Separate start and end guards, `-G START[,END]` (default unchanged: 3,3)

The unicast gate keeps a guard at both edges of a shared slot. The iPhone transmits across the whole 64 TU slot
(1st percentile 0.75 TU, 99th percentile 62.7 TU), so for an isolated slot the 3 TU guards cost up to 6 of 64 TU.
`-G` sets them separately, 0..16 TU. **The default is still 3,3**: whether a smaller guard loses frames on this card
has not been measured, so it is an experiment, not a setting.

### 3. Statistics that say where unicast time goes

Every 10 s, besides the existing `STATS` line (unchanged):

```
GATE sent N wait_channel N wait_end N wait_start N queue_full N non_peer N held_ms N | rx_rssi_ignored N sync_miss M/T
SLOTS tx n0 n1 ... n15
```

`wait_*` count gate evaluations (one per timer wake-up), not frames. `held_ms` is the total time frames waited for the
gate. `SLOTS` is how many unicast frames left in each of the 16 slots. These answer questions the captures cannot
(the monitor reads our own frames back without a timestamp or channel): are we losing the time to the phone being on
another channel, to the guards, or to a full radio queue?

## Core rewrite, 19 October 2026

The receive, transmit, I/O and command-line paths were rewritten in the existing C backend. The AWDL wire format,
channel strategies, RSSI tuning and application interface remain compatible. This is a refactor of OWL, not an
independent implementation of the entire AWDL protocol.

* **Receive:** wire helpers check negative/overflowing ranges and use alignment-safe endian reads/writes. Supported
  action TLVs are checked before changing peer or synchronization state. Ethernet buffers have exactly the decoded
  length; the old fixed-size buffer could forward uninitialized tail bytes. Oversized payloads and invalid LLC OUIs
  are rejected. The existing A-MSDU limits and iPhone padding support remain.
* **Transmit:** unicast and multicast preserve queued frames while the driver reports a full queue. The next retry
  checks the channel gate again. TAP reads pause while queues are full and resume when space returns, preventing a
  busy event loop. End-guard retries use the next window boundary. Permanent injection failures no longer count as
  successful sends; `TXERR data N action N` reports them separately. IPv6 is checked before using OWL's IPv6 encoder.
* **I/O and shutdown:** failed setup closes pcap, filter, socket and TAP resources; invalid descriptors and short writes
  are errors. SIGINT/SIGTERM stop watchers and release queued frames and peer tables. Debug-dump failures cannot crash
  reception. Headless capture replay requires no wireless interface or privileges and terminates at EOF.
* **Command line:** options are parsed and validated before startup or daemonization. Numeric overflow, trailing junk,
  overlong names and malformed guards/MAC addresses are rejected. `--help` is read-only. Background startup errors
  reach the launching process.
* **Build:** dependency tracking includes headers and effective compiler flags. Sanitizer builds instrument OWL itself,
  the daemon and all test objects in a separate directory. The manifest records the source file hashes, build flags,
  compiler and static libev digest.

## Options added

| Flag | Meaning | Default |
|---|---|---|
| `-G START[,END]` | unicast guards in TU (0..16) | `3,3` |
| `-r DBM` | RSSI threshold for discovering a peer (-100..-30) | `-80` |
| `-I` | judge each frame alone (upstream) | running average |

Everything else (`-i -c -N -S -E -F -A -R -W -m -f -v -d -D`) is unchanged.

## Integration

* **Selection**: the window has *OWL ottimizzato* (*Visibilità*), on by default; from a terminal
  `linux-airdrop --owl-guard 1 --owl-rssi -75`. The app passes `--owl-variant nexa` to the bridge, which validates
  the flags (`owl_extras`) before passing `-G`/`-r`/`-I` to the binary.
* **Fallback**: if `owl-nexa` is missing or exits before creating `awdl0`, the bridge says so in the log and starts
  `owl-modern` with the same options and none of the new ones. The fallback covers *start-up* failures only.
* **Passwordless helper**: `backend/owl-nexa` is in the helper's list of root-owned files, so it is hashed and
  checked like the others; the package refreshes the helper on upgrade.

## Build and tests

```sh
cd backend/owl-src
make                  # build/owl; needs libpcap/libnl/libev development packages
make check            # 99 unit + 15 daemon + 16 CLI tests, plus 3 downstream C harnesses
make sanitize         # same checks with full AddressSanitizer + UndefinedBehaviorSanitizer instrumentation
make install-backend  # copies the release build to backend/owl-nexa and writes its build manifest

# Replay an existing radiotap capture without accessing wireless interfaces:
build/owl -i ../../tests/fixtures/nexaawdl/real_full.pcap -h ''
```

`make test`, `make daemon-tests`, `make cli-tests` and `make harness` run individual groups. `B=...` selects a separate
build directory. Sanitizer unit tests disable leak detection because inherited upstream fixtures retain peer tables;
leak detection was additionally enabled for all daemon tests and capture replay, which passed.

The tests cover RSSI and scheduling, malformed/truncated input, exact Ethernet lengths, unaligned data, queue pressure,
radio failures, backpressure, shutdown, CLI validation and capture errors. The real reference capture contains 390
frames: replay reports 174 action frames, 216 data frames and zero unknown frames. Three larger radio captures were
also replayed under the sanitizers without a crash or memory error; those include unrelated or unsupported frames.
An Ethernet capture is correctly rejected because OWL's input expects radiotap.

Application integration passed: 59 bridge tests, 24 helper tests and 38 window/background tests, including variant
selection and startup fallback. Test details are recorded in [`OWL-VERIFICATION.json`](OWL-VERIFICATION.json).

## What is not verified

The rewrite is verified by local tests and replay of recorded traffic on 19 October 2026. **The rewritten binary has
not been tested in a live transfer with an iPhone or iPad**. Open questions, each answered by the `GATE`/`SLOTS` lines of one live session:

* does the looser RSSI filter change discovery or sync (compare `rx_rssi_ignored` and `sync_miss`);
* where does a transfer's time go (`wait_channel`, `wait_end`/`wait_start`, `held_ms`, `SLOTS`);
* does `-G 1` or `-G 1,1` lose frames (compare a transfer's time and retransmissions at 3,3 and at 1,1).

Candidates not started, because they depend on those answers or on a live test: transmitting at an HT rate
(`--data-rate mcs7`, an existing option), A-MSDU aggregation on transmit, a real unicast queue instead of the single
`next` slot, and checking or raising the transmit power.
