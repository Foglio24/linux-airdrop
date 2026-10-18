"""Read-only identification of the Wi-Fi hardware and an honest AWDL capability assessment.

Everything is gathered with read-only commands (lspci, iw, nmcli, ethtool) and sysfs, each with a timeout
and a fixed C locale. Parsing is separated from running so it can be tested on recorded output.

The report keeps three things apart:
* `facts`: what the tools and sysfs reported;
* `inferences`: what follows from the facts (labelled as such);
* `not_verified`: what only a live test with an Apple device can show. Monitor mode and injection do not,
  on their own, prove AWDL interoperability, and the report never says they do.
"""
from __future__ import annotations

import os
import platform
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"}
SOCIAL_CHANNELS = {6: "2.4 GHz", 44: "5 GHz", 149: "5 GHz"}      # the AWDL social channels
TIMEOUT = 5


def run_readonly(argv, timeout=TIMEOUT):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=ENV)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


# -------------------------------------------------------------------------------------------------- parsers

def parse_lspci(text, pci_address=None):
    """Find the Network controller [0280] entry (optionally for one PCI address). Returns a dict or None."""
    if not text:
        return None
    blocks, current = [], []
    for line in text.splitlines():
        if line and not line[0].isspace():
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(current)
    for block in blocks:
        head = block[0]
        if "[0280]" not in head and "Network controller" not in head:
            continue
        match = re.match(r"(\S+)\s+.*?:\s+(.*?)\s+\[([0-9a-f]{4}):([0-9a-f]{4})\](?:\s+\(rev (\w+)\))?", head)
        if not match:
            continue
        address, label, vendor, device, rev = match.groups()
        if pci_address and not (pci_address.endswith(address) or address.endswith(pci_address)):
            continue
        body = "\n".join(block[1:])
        driver = re.search(r"Kernel driver in use:\s*(\S+)", body)
        modules = re.search(r"Kernel modules:\s*(.+)", body)
        return {"address": address, "label": label, "vendor_id": vendor, "device_id": device, "revision": rev,
                "driver": driver.group(1) if driver else None,
                "modules": [m.strip() for m in modules.group(1).split(",")] if modules else []}
    return None


def parse_iw_dev(text):
    """`iw dev` -> list of {'phy','name','type','addr','ifindex'} (unnamed wdevs have name None)."""
    out, phy, cur = [], None, None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if raw.startswith("phy#"):
            phy = raw.strip()[4:]
            cur = None
        elif line.startswith("Interface "):
            cur = {"phy": phy, "name": line.split(None, 1)[1], "type": None, "addr": None, "ifindex": None}
            out.append(cur)
        elif line.startswith("Unnamed/non-netdev"):
            cur = {"phy": phy, "name": None, "type": None, "addr": None, "ifindex": None}
            out.append(cur)
        elif cur is not None:
            key, _, value = line.partition(" ")
            if key == "type":
                cur["type"] = value
            elif key == "addr":
                cur["addr"] = value
            elif key == "ifindex":
                cur["ifindex"] = int(value) if value.isdigit() else None
    return out


def parse_nmcli(text):
    """`nmcli -t -f DEVICE,TYPE,STATE device` -> {device: (type, state)}."""
    out = {}
    for line in (text or "").splitlines():
        parts = line.split(":")
        if len(parts) >= 3:
            out[parts[0]] = (parts[1], ":".join(parts[2:]))
    return out


_CHANNEL = re.compile(r"^\s*\*\s*(\d+)(?:\.\d+)?\s+MHz\s+\[(\d+)\](.*)$")


def parse_iw_phy(text):
    """Bands, channels with restrictions, modes, interface combinations and feature lines from `iw phy <p> info`."""
    channels = {}
    for line in (text or "").splitlines():
        match = _CHANNEL.match(line.expandtabs(8))
        if match:
            freq, number, rest = int(match[1]), int(match[2]), match[3]
            flags = []
            low = rest.lower()
            if "disabled" in low:
                flags.append("disabled")
            if "no ir" in low or "no-ir" in low or "no initiating" in low:
                flags.append("no-ir")
            if "radar" in low or "dfs" in low:
                flags.append("radar")
            channels.setdefault(freq, {"number": number, "flags": flags})
    modes = []
    in_modes = False
    for line in (text or "").splitlines():
        if "Supported interface modes:" in line:
            in_modes = True
            continue
        if in_modes:
            m = re.match(r"\s*\*\s+(\S+)\s*$", line)
            if m:
                modes.append(m.group(1))
            else:
                in_modes = False
    combos = []
    chunk = re.search(r"valid interface combinations:(.*?)(?:\n\t[A-Za-z]|\Z)", text or "", re.S)
    if chunk:
        for entry in re.split(r"\n\s+\*\s+", "\n" + chunk.group(1)):
            entry = " ".join(entry.split())
            if not entry:
                continue
            total = re.search(r"total\s*<=\s*(\d+)", entry)
            chans = re.search(r"#channels\s*<=\s*(\d+)", entry)
            limits = re.findall(r"#\{\s*([^}]*)\}\s*<=\s*(\d+)", entry)
            combos.append({"limits": [([m.strip() for m in modes_.split(",")], int(n)) for modes_, n in limits],
                           "total": int(total.group(1)) if total else None,
                           "channels": int(chans.group(1)) if chans else None})
    text = text or ""
    return {"channels": channels, "modes": modes, "combinations": combos,
            "active_monitor": "supports active monitor" in text,
            "tx_status_socket": "TX status socket option" in text,
            "p2p_go": "P2P-GO" in modes, "monitor": "monitor" in modes,
            "p2p_device": "P2P-device" in modes,
            "software_modes": "software interface modes" in text,
            "mac_config_on_create": "configuring vdev MAC-addr on create" in text,
            "ht": bool(re.search(r"HT20/HT40|HT Capabilities|Capabilities: 0x", text)),
            "he": "HE Iftypes" in text}


def parse_ethtool(text):
    out = {}
    for line in (text or "").splitlines():
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return out


# --------------------------------------------------------------------------------------------------- report

@dataclass
class HardwareReport:
    kernel: str
    interface: str | None = None
    phy: str | None = None
    chip: dict | None = None
    driver: str | None = None
    firmware: str | None = None
    parent_state: tuple | None = None
    rfkill: list = field(default_factory=list)
    iw: dict = field(default_factory=dict)
    other_interfaces: list = field(default_factory=list)
    facts: list = field(default_factory=list)
    inferences: list = field(default_factory=list)
    limitations: list = field(default_factory=list)       # (kind, text); kind is 'hardware' | 'software' | 'policy'
    not_verified: list = field(default_factory=list)
    social: dict = field(default_factory=dict)
    verdict: str = "unknown"

    def as_dict(self):
        chip = self.chip or {}
        return {
            "kernel": self.kernel, "interface": self.interface, "phy": self.phy,
            "chipset": chip.get("label"), "pci_id": f"{chip.get('vendor_id')}:{chip.get('device_id')}" if chip else None,
            "pci_address": chip.get("address"), "driver": self.driver, "firmware": self.firmware,
            "manager_state": list(self.parent_state) if self.parent_state else None, "rfkill": self.rfkill,
            "monitor": self.iw.get("monitor"), "active_monitor": self.iw.get("active_monitor"),
            "p2p_go": self.iw.get("p2p_go"), "modes": self.iw.get("modes", []),
            "combinations": self.iw.get("combinations", []), "social_channels": self.social,
            "bands": bands_of(self.iw.get("channels", {})),
            "limitations": [{"kind": k, "text": t} for k, t in self.limitations],
            "facts": self.facts, "inferences": self.inferences, "not_verified": self.not_verified,
            "verdict": self.verdict,
        }


def bands_of(channels):
    """Channel numbers per band; `channels` is keyed by frequency (numbers repeat between 2.4 and 6 GHz)."""
    out = {"2.4": [], "5": [], "6": []}
    for freq, info in channels.items():
        key = "2.4" if freq < 3000 else "5" if freq < 5900 else "6"
        out[key].append(info["number"])
    return {k: sorted(v) for k, v in out.items()}


def social_channel_status(channels):
    """For each AWDL social channel: whether the card lists it and whether transmitting there is unrestricted."""
    out = {}
    by_number = {i["number"]: i for f, i in channels.items() if f < 5900}      # 2.4 and 5 GHz only
    for number, band in SOCIAL_CHANNELS.items():
        info = by_number.get(number)
        if info is None:
            out[number] = {"band": band, "listed": False, "usable": False, "flags": []}
        else:
            usable = not any(f in info["flags"] for f in ("disabled", "no-ir", "radar"))
            out[number] = {"band": band, "listed": True, "usable": usable, "flags": info["flags"]}
    return out


def assess(report):
    """Fill facts, inferences, limitations and the verdict from what was read. Pure function of the report."""
    iw = report.iw
    report.social = social_channel_status(iw.get("channels", {}))
    facts, inf, lim, nv = [], [], [], []
    if report.chip:
        facts.append(f"{report.chip['label']} ({report.chip['vendor_id']}:{report.chip['device_id']}) at "
                     f"{report.chip['address']}, driver {report.driver or 'unknown'}")
    if report.firmware:
        facts.append(f"firmware {report.firmware}")
    facts.append(f"kernel {report.kernel}")
    facts.append("monitor mode: " + ("yes" if iw.get("monitor") else "no"))
    facts.append("active monitor (ACKs frames addressed to it): " + ("yes" if iw.get("active_monitor") else "no"))
    facts.append("P2P group owner: " + ("yes" if iw.get("p2p_go") else "no"))
    for number, status in report.social.items():
        facts.append(f"social channel {number} ({status['band']}): "
                     + ("listed, unrestricted" if status["usable"] else
                        "listed but restricted " + "/".join(status["flags"]) if status["listed"] else "not listed"))
    combos = iw.get("combinations", [])
    max_channels = max((c["channels"] or 0 for c in combos), default=None)
    has_monitor_combo = any("monitor" in modes for c in combos for modes, _n in c["limits"])
    if combos:
        facts.append(f"interface combinations allow up to {max_channels} channel(s) at once; "
                     + ("monitor is part of a combination" if has_monitor_combo else
                        "monitor is not listed in any combination"))
    if not iw.get("monitor"):
        lim.append(("hardware", "the driver does not offer monitor mode: AWDL frames cannot be received"))
    if iw.get("monitor") and not iw.get("active_monitor"):
        lim.append(("software", "no active monitor: frames addressed to us are not acknowledged in hardware"))
    if not iw.get("p2p_go"):
        lim.append(("software", "no P2P group owner: the Wi-Fi Direct anchor that holds the channel is unavailable"))
    missing = [n for n, s in report.social.items() if not s["listed"]]
    restricted = [n for n, s in report.social.items() if s["listed"] and not s["usable"]]
    if missing:
        lim.append(("hardware", f"social channel(s) {missing} are not offered by the card"))
    if restricted:
        lim.append(("policy", f"social channel(s) {restricted} carry regulatory restrictions in the current domain"))
    if max_channels is not None and max_channels <= 1:
        lim.append(("hardware", "only one channel at a time: AWDL channel hopping is not possible together with infrastructure"))
    elif max_channels == 2:
        inf.append("the card can serve two channels at once, so an infrastructure link and one fixed AWDL "
                   "channel can coexist; following a peer's hopping sequence in software is still not possible")
    if iw.get("tx_status_socket"):
        inf.append("TX status is available, which lets a sender learn whether a frame was acknowledged")
    if iw.get("monitor") and iw.get("active_monitor"):
        inf.append("passive reception of AWDL frames is possible; sending depends on frame injection through "
                   "a monitor interface, which the kernel's capability flags cannot confirm")
    nv.extend(["frame injection from a monitor interface on this firmware",
               "acknowledgement of Apple devices' unicast frames by the active monitor",
               "AWDL synchronisation as a participant (not only as an observer)",
               "AirDrop interoperability with a given Apple device and OS version"])
    ready = iw.get("monitor") and iw.get("active_monitor") and iw.get("p2p_go") and not missing
    report.facts, report.inferences, report.limitations, report.not_verified = facts, inf, lim, nv
    report.verdict = ("passive-rx-possible" if iw.get("monitor") and not missing else "incapable") if not ready \
        else "capable-needs-live-verification"
    return report


def rfkill_states(root="/sys/class/rfkill"):
    out = []
    try:
        for entry in sorted(Path(root).iterdir()):
            try:
                kind = (entry / "type").read_text().strip()
                if kind != "wlan":
                    continue
                out.append({"name": (entry / "name").read_text().strip(),
                            "soft_blocked": (entry / "soft").read_text().strip() == "1",
                            "hard_blocked": (entry / "hard").read_text().strip() == "1"})
            except OSError:
                continue
    except OSError:
        pass
    return out


def probe(interface=None, run=run_readonly, sys_root="/sys"):
    """Identify the card and assess it. `run(argv)` returns stdout or None; everything is read-only."""
    sysp = Path(sys_root)
    devices = parse_iw_dev(run(["iw", "dev"]))
    named = [d for d in devices if d["name"]]
    if interface is None:
        picked = next((d for d in named if d["type"] == "managed"), named[0] if named else None)
        interface = picked["name"] if picked else None
    report = HardwareReport(kernel=platform.release())
    report.interface = interface
    if interface:
        base = sysp / "class/net" / interface
        report.phy = next((d["phy"] for d in devices if d["name"] == interface), None)
        try:
            address = (base / "device").resolve().name
        except OSError:
            address = None
        if not re.fullmatch(r"(?:[0-9a-f]{4}:)?[0-9a-f]{2}:[0-9a-f]{2}\.\d", address or ""):
            address = None                       # not a PCI address (USB, virtual, missing sysfs)
        try:
            report.driver = (base / "device/driver").resolve().name
        except OSError:
            pass
        eth = parse_ethtool(run(["ethtool", "-i", interface]))
        report.firmware = eth.get("firmware-version") or None
        report.driver = report.driver or eth.get("driver")
        report.chip = parse_lspci(run(["lspci", "-nnk"]), address)
        report.parent_state = parse_nmcli(run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"])).get(interface)
    report.rfkill = rfkill_states(str(sysp / "class/rfkill"))
    report.other_interfaces = [d for d in devices if d["name"] != interface]
    if report.phy is not None:
        report.iw = parse_iw_phy(run(["iw", "phy", f"phy{report.phy}", "info"]))
    return assess(report)
