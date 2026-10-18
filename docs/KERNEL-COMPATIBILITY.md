# Compatibility across Linux kernels

The native radio path is selected by observed capabilities, not by comparing
`uname` against a list of kernel releases. Kernel release equality is required
only for the optional prebuilt legacy driver modules, where Linux module ABI
compatibility and symbol versions must match exactly.

## Native path

The driver must support the candidate MediaTek hardware, active monitor and
P2P Group Owner mode. The required userspace tools must also be present.
Before the active monitor is brought up, the privileged bridge asks nl80211
to set `ACTIVE` and `SKIP_TX` on that owned, down interface and requires an
acknowledgement from the kernel. This is a real compatibility probe; an `iw`
capability string alone does not certify AirDrop interoperability.

The Wi-Fi Direct group holds the chosen channel (6 or 44). The passive monitor supplies receive
filters and firmware sniffer mode; the active monitor supplies ACK support.
The managed parent remains up, disconnected and outside NetworkManager until
the monitors are removed. This avoids the hidden-monitor channel-ownership
path, including when the Wi-Fi Direct group stops. Cleanup restores the
original link and NetworkManager state after removing owned radio interfaces.

This approach uses Linux's existing nl80211 interface and does not require a
new driver build merely because a kernel release string has changed.

## Legacy module fallback

If the native capabilities are insufficient, the app can use the existing
patched-driver bundle only after checking the running kernel, module hashes,
vermagic and source versions. The privileged bridge repeats these checks
before loading anything. A module compiled for one kernel is never relabelled
or force-loaded on another kernel.

Adding a legacy bundle for another unsupported kernel still requires its exact
matching sources, headers and configuration, patch review, ABI checks and real
hardware validation. This repository does not silently download or load kernel
modules from an unverified source.

## Limits and testing

This is capability-based portability, not a claim that every Linux kernel or
Wi-Fi chipset can implement AirDrop. Drivers without the necessary monitor,
injection or ACK behavior may require driver work. Hardware, firmware, Apple
device behavior, Bluetooth and the firewall can affect actual transfers even
when all kernel capability probes pass.

Unit tests cover capability combinations, safe rejection, matching and damaged
legacy bundles, netlink errors and cleanup ordering. They do not substitute for
testing multiple kernels on real hardware. The local machine and real-device
results are recorded in `ANDUINOS.md`; the original `verification.json` retains
the historical tests from the previous system.
