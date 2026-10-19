Linux AirDrop 1.3.1 introduces `owl-nexa`, a refactor of OWL's receive, transmit, I/O and command-line paths, integrated with the app's **OWL ottimizzato** setting.

- Validate malformed/truncated frames before changing peer state and forward only the decoded Ethernet bytes.
- Retry queued unicast and multicast packets when the radio is busy; pause host reads while queues are full.
- Count successful injections accurately and report permanent transmission errors separately.
- Validate command-line values before startup, release resources on shutdown and support finite, unprivileged capture replay.
- Keep `owl-modern` as the startup fallback. Upgrades refresh the passwordless helper if it was already configured.
- Include the GPL sources, reproducible build commands, binary/source hashes and verification report.

Validation: **251 automated tests** (130 OWL and 121 app integration), **3 downstream C harnesses**, full AddressSanitizer/UndefinedBehaviorSanitizer checks and recorded traffic replay. The 390-frame reference capture decodes with zero unknown frames; daemon and replay checks also pass with leak detection enabled.

**The rewritten binary has not yet been tested in a live iPhone/iPad transfer.** Offline checks do not establish radio timing, throughput or Apple interoperability. Driver modules are unchanged.

Install or upgrade the attached package:

```sh
sudo apt install ./linux-airdrop_1.3.1_amd64.deb
```

The attached `.sha256` file verifies the Debian package. See [OWL details](https://github.com/Foglio24/linux-airdrop/blob/v1.3.1/docs/OWL-NEXA.md) and the [verification report](https://github.com/Foglio24/linux-airdrop/blob/v1.3.1/docs/OWL-VERIFICATION.json).
