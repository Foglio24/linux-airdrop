"""Builders for synthetic captured frames used by the NexaAWDL tests."""
import json
import struct
from pathlib import Path

BUNDLE = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "nexaawdl"

import sys
sys.path.insert(0, str(BUNDLE))
from nexaawdl import frames, transport                    # noqa: E402
from nexaawdl.dot11 import AWDL_BSSID                     # noqa: E402

PEER_A = bytes.fromhex("02aabbccdd01")
PEER_B = bytes.fromhex("02aabbccdd02")
BROADCAST = bytes.fromhex("ffffffffffff")
OWN = bytes.fromhex("02000000000f")


def fixture(name):
    return (FIXTURES / name).read_text()


def real_frames(kind):
    return [bytes.fromhex(item["hex"]) for item in json.loads(fixture("real_frames.json"))[kind]]


def radiotap(tsft=1_000_000, freq=2437, signal=-45, rate_500k=24, flags=0):
    body = struct.pack("<Q", tsft) + bytes((flags, rate_500k)) + struct.pack("<HH", freq, 0x0140) + struct.pack("<b", signal)
    header = struct.pack("<BBHI", 0, 0, 8 + len(body) + 1, 0x2F)
    return header + body + b"\0"


def action_frame(src, body, tsft=1_000_000, freq=2437, signal=-45, dst=BROADCAST, flags=0, bssid=AWDL_BSSID):
    mac = struct.pack("<HH", 0x00D0, 0) + dst + src + bssid + struct.pack("<H", 0)
    return radiotap(tsft, freq, signal, flags=flags) + mac + body


def data_frame(src, payload, tsft=1_000_000, freq=2437, signal=-45, dst=OWN, bssid=AWDL_BSSID):
    mac = struct.pack("<HH", 0x0188, 0) + dst + src + bssid + struct.pack("<H", 0) + struct.pack("<H", 0)
    return radiotap(tsft, freq, signal) + mac + payload


def sequence(*pairs, encoding=frames.ENC_OPCLASS):
    """A 16-slot sequence from {slot: channel} pairs."""
    channels = [0] * 16
    for slot, channel in pairs:
        channels[slot] = channel
    return frames.make_chanseq(channels, encoding)


def sync_params(down=20, seq=40, chanseq=None, master=PEER_A, presence=4, period=16):
    return frames.SyncParams(
        next_aw_channel=6, tx_down_counter=down, master_channel=6, guard_time=0, aw_period=period, af_period=110,
        flags=0x1800, aw_ext_length=64, aw_com_length=16, remaining_aw_length=16, min_ext=3, max_ext_multicast=3,
        max_ext_unicast=3, max_ext_af=3, master_addr=master, presence_mode=presence, next_aw_seq=seq, ap_alignment=seq,
        chanseq=chanseq or sequence((0, 149), (8, 6), (9, 149), (10, 44)))


def psf(tlvs, subtype=frames.SUBTYPE_PSF, phy_tx=5000, target_tx=4900):
    return frames.encode_action(frames.ActionFrame(subtype, phy_tx, target_tx, tuple(tlvs)))


def election_v2(master=PEER_A, sync=PEER_A, metric=100, counter=7, distance=0):
    return frames.ElectionParamsV2(master, sync, counter, distance, metric, metric, counter)


def mdns_response(service="_airdrop._tcp.local", instance="abc123"):
    def name(text):
        return b"".join(bytes((len(l),)) + l.encode() for l in text.split(".")) + b"\0"
    ptr_target = name(f"{instance}.{service}")
    answer = name(service) + struct.pack(">HHIH", 12, 1, 120, len(ptr_target)) + ptr_target
    return struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 0) + answer


def ipv6_udp(src_mac, payload, dport=5353, sport=5353, dst=b"\xff\x02" + bytes(13) + b"\xfb"):
    udp = struct.pack(">HHHH", sport, dport, 8 + len(payload), 0) + payload
    head = struct.pack(">IHBB", 0x60000000, len(udp), 17, 255) + transport.link_local(src_mac) + dst
    return head + udp


def awdl_data(src_mac, packet, ethertype=frames.ETH_IPV6):
    return frames.encode_data(1, ethertype, packet)
