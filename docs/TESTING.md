# What was tested, and what that does and does not prove

## 1.2

New automated tests cover the capability-based radio plan (`test_radio_compat.py`), the nl80211 monitor flags and cleanup
ordering (`test_root_bridge.py`), the regulatory check of the Wi-Fi Direct channel including `iw`'s decimal frequencies
(`test_p2p_anchor.py`), the sender's upload-only TCP counters and confirmation state (`test_send_transport.py`) and the
receiver's keepalive probes on a real TCP connection (`test_receiver_security.py`). Real-device results are in
[`ANDUINOS.md`](ANDUINOS.md) and [`SENDING.md`](SENDING.md): receiving tried with an iPhone and an iPad, sending only with
small files.

## 1.1

Run everything with `tests/run-all.sh` (one process per file; 27 files). The GUI, tray and single-instance tests need a desktop
session; the loopback and interface tests need `unshare -Urn`; none of them needs root, a Wi-Fi card or a phone.

## Automated tests (354 tests, all passing on the development machine; one slow emulation case runs with AIRDROP_SLOW_TESTS=1)

| File | What it checks |
|---|---|
| `test_fileinfo.py` | file kinds from Apple type identifiers and extensions, removal of control and direction-override characters, size / speed / duration / date formats |
| `test_progress.py` | speed, time left and percentage computed only from measured byte counts: unknown totals give no percentage, a counter passing its total drops the total, stalls read as zero, stale updates ignored; the event line parser |
| `test_store.py` | history and devices in SQLite: round trips, file mode 0600, the four final states, pruning, clearing, crash recovery, damaged or newer database moved aside, recording switched off, device counters, weak vs. record identity keys |
| `test_receiver_events.py` | the real HTTP handler with locally built archives: ask / accept / decline events, real byte totals from `Content-Length` and `TotalBytes`, unknown totals, failure, user cancel (partial data removed), the cancel command reader, shared links; the prompt description and thumbnail passed to the confirmation hook |
| `test_preview.py` | description of a request (sizes, totals, hostile names, huge lists, links); the thumbnail decoder: valid images, wrong formats, garbage, truncated, oversized, decompression-bomb dimensions, timeout, forged output, resource limits |
| `test_identity.py` | fingerprint of a signed record using a locally generated test CA (stable, order independent, per account, unknown signer, garbage) |
| `test_folder_send.py` | what goes into the archive for files and folders (links and special files left out), that our receiver reads it back, the folder entries in `/Ask`, the sender's byte-count hooks |
| `test_outgoing.py` | what may be sent (missing, unreadable, special, duplicate names, size limit, huge folders) and the ZIP form of a folder |
| `test_diagnostics.py`, `test_repair.py` | every check in every state against a fake `/sys` and `/proc`, `iw`/`systemctl` parsing, hanging commands, the anonymised report; detection of each known fault, the attempt limits and the log, ending leftover processes safely |
| `test_app_transfers.py` | the window with fake processes: live rows, history and device records, declined / failed / cancelled, queue, cancel, drag and drop, second launch with files, session ending mid-transfer, automatic repair bounded and switchable, page switching |
| `test_nautilus.py` | the Files extension against a stand-in for the Nautilus API, and the fallback script |
| `test_single_instance.py` | two real processes on the session bus: files given to a second launch arrive in the first |
| `test_awdl_link.py` | the sender's TCP settings and its TCP report; real OpenDrop processes over an emulated AWDL link (phone listening 2 of 16 slots, frames lost or not): with acknowledgements a send completes, without them (slow case, `AIRDROP_SLOW_TESTS=1`) it stalls as on 16 October |
| `test_loopback_transfer.py` | **a real OpenDrop sender and receiver** over a dummy `awdl0` in a private network namespace: TLS, `/Ask`, `/Upload`, our confirmation hook, files on disk byte for byte (24 MB), progress events agreeing on both sides, a native folder |
| the 12 existing files | unchanged behaviour of 1.0: radio bridge, driver trial, Wi-Fi Direct anchor, system helper, tray, autostart, receiver security, interface selection, TTL, background behaviour (two tests updated for the two new settings keys) |

Also done by hand, read-only, on the working machine while its AirDrop session was running: `linux-airdrop --diagnose` and the
checks of the *Diagnostica* page (all green and matching `iw dev`, `/sys` and `/proc`); screenshots of every page in light and dark
themes and at 360 px width; the packaged `.deb` unpacked into a read-only tree, where `app.py --check`, `--diagnose`,
`backend/opendrop --help`, the thumbnail decoder and `system_helper.py status` ("ready": the root-side files are byte-identical to
1.0, so the passwordless helper stays valid) were run.

## What this does NOT prove

* **No transfer with an iPhone was made with 1.1.** The loopback test shows our sender and receiver agree with each other; it says
  nothing about what iOS accepts. In particular: the native folder form, the thumbnail and sizes iOS really puts in `/Ask` (the
  code handles their absence), whether iOS attaches a validation record (and in which layout), and the speed and progress figures
  over a real AWDL link are all unverified.
* **The GNOME Files menu entry was not run inside Files**: `python3-nautilus` is not installed on the development machine, so the
  extension was tested only against a stand-in for its API. The fallback script was tested with a fake `gio`.
* **Drag and drop was tested by calling its handlers**, not by dragging with a pointer from Files.
* **The repairs that restart the session** were tested with fakes; nothing that touches the Wi-Fi card was run.
* **The `.deb` was built and inspected but not installed** (no administrator rights in the development session).
* There is a harmless GTK warning ("baselines must be inside the widget size") in one test run; it did not appear in the
  screenshots of the real window.
