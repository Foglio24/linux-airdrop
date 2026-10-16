# Why sending to the iPhone stalled, and what changed (16 October 2026)

## What happened

A 664 MB file sent from the PC to an iPhone: the phone was found, accepted the transfer, the upload started, and
after **192 kB** nothing moved for 30 seconds, so the sender gave up ("Upload stalled … the receiver stopped
acknowledging"). Minutes earlier the same phone had sent 1.5 MB *to* the PC in about 3 seconds. No kernel or driver
errors, memory free, all diagnostics green.

## Cause

Read from the session log (`~/.local/state/linux-airdrop/session-*.log`) and from OWL's source:

1. **Frames to the phone were sent once, never acknowledged or retried.** With `-F` (radio fixed on one channel, our
   default because of the Wi-Fi Direct anchor) OWL marks every unicast frame `NOACK` unless `-A` is given
   (`src/tx.c`, `awdl_init_full_data_frame`). Upstream chose this for an Intel AX201, where a missed frame was retried
   for 75–140 ms and blocked the queue. The app never passed `-A`, although the bridge supports it.
2. **The phone listens on our channel only briefly.** In the log the iPhone's channel sequence has channel 6 in
   2 of 16 slots when idle (`44,44,149,0,0,0,0,0,6,6,44,0,…`) and 5 of 16 when busy, switching every second or two.
   Any frame lost to a collision or to a window edge is simply gone.
3. **TCP then backs off exponentially** (1, 2, 4, 8, 16 s…). In the log our side sent 76 TCP frames in the first 20 s
   of the upload, then 1 in the next 10 s, then none; the phone sent nothing back for 25 s.
4. **The sender gives up after 30 s without progress.**

The other direction works because the iPhone's own radio retries every frame, and the PC is on channel 6 all the time.

## Evidence from a link emulator

`tests/awdl_link_emulator.py` runs the real OpenDrop sender and receiver over a relay that only lets frames through in
the phone's slots, at 12 Mbit/s, losing PC→phone frames with a given probability (the model of sending without
acknowledgements). 2 MB, measured:

| Phone's slots on our channel | Frames lost | Kernel TCP defaults | With the new TCP settings |
|---|---|---|---|
| 2 of 16 | 0 % | 114 kB/s | 108 kB/s |
| 2 of 16 | 4 % | 101 kB/s | 101 kB/s |
| 2 of 16 | 15 % | 26 kB/s | 31 kB/s |
| 2 of 16 | 30 % | **stalled** | **stalled** (later) |
| 5 of 16 | 0 % | 277 kB/s | 277 kB/s |
| 5 of 16 | 15 % | 55 kB/s | 55 kB/s |
| 5 of 16 | 30 % | 10 kB/s | 13 kB/s |

Losing frames, not the short windows, is what kills the transfer; tuning TCP alone cannot save it. This is a model:
it does not prove how the MT7922 behaves on the air.

## What changed

* **Radio acknowledgements on by default** (*Visibilità → Conferme radio*): the app passes `--fixed-ack` to the bridge,
  which gives OWL `-A`. The MT7922 driver then sends each frame to the phone up to 15 times until the phone
  acknowledges it (`mt76_connac_mac.c`: `REM_TX_COUNT 15`, `NO_ACK` only on request). The root-side files are
  unchanged, so the passwordless helper stays valid. Can be switched off if receiving gets worse.
* **TCP of the sender** (`opendrop/client.py`, per connection, no root): linear retransmission timeouts while few
  packets are in flight, and a 2-second ceiling on the timeout, so it keeps knocking instead of waiting 16 s or more.
* **A send gives up after 90 s without progress** instead of 30.
* **Every upload logs the kernel's own TCP counters** (bytes acknowledged, segments, retransmitted share, RTT, RTO),
  so the next slow or failed send says how lossy the link really was.
* **Files over 100 MB ask first**, with the time to expect at 100–300 kB/s. The 664 MB file would take about
  40–120 minutes even when nothing goes wrong.

## First check with the phone

16 October, 12:03, radio acknowledgements on: a 62 KB image sent to the iPhone completed, and 1.5 MB received from it took
3.3 s, exactly as before, so acknowledgements do not slow receiving down. A send of several megabytes has not been tried
yet; that is the test that matters.

## Not verified yet

Whether the MT7922 really receives the iPhone's acknowledgements for injected frames (if it did not, every frame
would be sent 15 times: slower, but still delivered), whether the retries hold up the queue as on the AX201, and
whether receiving stays as fast. In the one earlier session with `-A` (15 October, before the Wi-Fi Direct anchor)
TLS handshakes to the phone completed, which means acknowledged frames did reach it. The next real test answers the
rest: the TCP line in the log gives the retransmitted share, and OWL's STATS lines the frames sent.
