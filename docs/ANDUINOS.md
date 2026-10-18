# Local AnduinOS installation

Version: `1.3.0`.

## System

- AnduinOS 2.0.4, GNOME Wayland, Linux `7.0.0-38-generic`.
- MediaTek MT7922 (`14c3:0616`), stock `mt7921e` driver.
- Python 3.14.4, GTK 4.22 and libadwaita 1.9.
- The native driver advertises active-monitor support. The bundled 7.3 driver
  modules are incompatible with this kernel and are not loaded on this system.

## Compatibility changes

- Native-driver sessions require a Wi-Fi Direct group on channel 6 (default) or 44 (5 GHz, faster link; only if the regulatory domain allows a GO there - checked before starting). Both the
  GUI and the privileged bridge reject monitor-only native sessions before
  changing network state.
- Native Wi-Fi Direct sessions keep the disconnected managed parent interface
  up until both monitor interfaces have been removed. This prevents allocation
  of a hidden monitor channel owner while retaining the passive monitor's
  receive filters and firmware sniffer mode. The first active-only experiment
  started correctly but the iPhone did not discover the PC; it is not used in
  the final radio strategy.
- The native monitor retains active ACK support and disables capture of its
  own transmitted packets using `NL80211_MNTR_FLAG_SKIP_TX`. This avoids the
  stock kernel's unbalanced transmit-monitor counter on repeated interface
  up/down cycles. OWL receives incoming packets only and ignores its own MAC.
- The existing patched-driver mode remains available only for its exact kernel.
  Its hash, kernel, vermagic and driver-restoration checks remain enabled.

## Installation configuration

The Debian package installs the app in `/usr/lib/linux-airdrop` and the
`linux-airdrop` launcher, including the GNOME Files context-menu extension.
The documented root-owned helper is configured for the installing user.

UFW is enabled on this system. This rule permits the receive service only on
the AWDL interface, from IPv6 link-local peers:

```sh
sudo ufw allow in on awdl0 from fe80::/10 to any port 8771 proto tcp comment 'Linux AirDrop AWDL'
```

The existing UFW rules already allow IPv6 mDNS multicast on UDP 5353. No global
receive-port exception or firewall shutdown is needed.

## Validation

The unmodified source passed 354 tests on this machine; one slow test was
intentionally skipped. This included the real sender/receiver loopback test
in an isolated network namespace, AWDL link emulation, mDNS, GTK and tray tests.
Run the focused compatibility tests and the full suite after changes:

```sh
python3 -I -B tests/test_root_bridge.py
python3 -I -B tests/test_app_background.py
sh tests/run-all.sh
```

Real-device validation is separate: make the PC visible, send a small file
from an unlocked iPhone using Share → AirDrop, then accept it on the PC. For
the reverse direction, enable Everyone for 10 Minutes on the iPhone and close
its Share sheet. End visibility and check that Wi-Fi management is restored.

## Removal

`sudo apt remove linux-airdrop` removes the package and its optional helper.
Remove the local firewall exception separately if no longer needed:

```sh
sudo ufw delete allow in on awdl0 from fe80::/10 to any port 8771 proto tcp
```

Received files remain in the Downloads folder under `LinuxAirDrop`.

## Real-device results (18 October 2026)

- iPhone → PC, channel 6: a 5.56 MB photo arrived whole in about 2 minutes.
- iPad → PC, channel 44: a 2.85 MB PDF stopped at 91 % before the keepalive fix and arrived whole in about 12 s after it.
- PC → iPhone: small files (up to 173 kB) only.
- Channel 36: the iPhone never discovered the PC (it does not use that channel); removed from the settings.
