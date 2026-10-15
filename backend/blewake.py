#!/usr/bin/env python3
"""AirDrop Continuity advertisement through BlueZ's D-Bus API.

The OS 26 shape A is documented from native captures in OmDrop's primary
source. It uses a connectable advertisement and a 100 ms interval:
https://github.com/brentkearney/omdrop-awdl/blob/534f91a525337951ca19c311e53d333bd6d55100/userspace/ble-airdrop-adv.py

ManufacturerData[0x004C] contains 05 12, then an 18-byte body:
40 | random tag (3) | reserved (4) | 03 | contact hashes (8) | 00.
Zero contact hashes target Everyone visibility; they cannot match Contacts
Only. The random tag follows the observed packet shape, not an Apple identity.
Registration only confirms BlueZ accepted the request, not phone discovery.
"""
import os
import signal
# Adapted from jedbillyb/airdrop-mt7921 @ 27edf28900d6b9a948816ee5e864afe1ce245056.
# Licensed GPL-3.0; see backend/licenses/airdrop-mt7921-GPL-3.txt.
import sys

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

PATH = "/org/bluez/linuxairdrop/adv0"
APPLE_COMPANY_ID = 0x004C
ADVERTISEMENT_INTERVAL_MS = 100
PAYLOAD_SOURCE = "https://github.com/brentkearney/omdrop-awdl/blob/534f91a525337951ca19c311e53d333bd6d55100/userspace/ble-airdrop-adv.py"


def build_payload(tag, hashes=bytes(8)):
    """Build the public shape A without including the company ID twice."""
    if not isinstance(tag, bytes) or len(tag) != 3:
        raise ValueError("Il tag AirDrop deve contenere 3 byte")
    if not isinstance(hashes, bytes) or len(hashes) != 8:
        raise ValueError("Gli hash AirDrop devono contenere 8 byte")
    body = bytes([0x40]) + tag + bytes(4) + bytes([0x03]) + hashes + bytes(1)
    if len(body) != 18:
        raise ValueError("Lunghezza del corpo AirDrop non valida")
    payload = bytes([0x05, len(body)]) + body
    if len(payload) != 20 or payload[1] != len(payload) - 2:
        raise ValueError("Lunghezza del messaggio AirDrop non valida")
    return payload


# Keep one tag stable for this registration, including repeated GetAll calls.
PAYLOAD = build_payload(os.urandom(3))


class Advertisement(dbus.service.Object):
    def __init__(self, bus, path, on_release):
        super().__init__(bus, path)
        self.on_release = on_release

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface != "org.bluez.LEAdvertisement1":
            raise dbus.exceptions.DBusException(
                "org.freedesktop.DBus.Error.InvalidArgs")
        return {
            "Type": dbus.String("peripheral"),
            "ManufacturerData": dbus.Dictionary(
                {dbus.UInt16(APPLE_COMPANY_ID): dbus.Array(PAYLOAD, signature="y")},
                signature="qv"),
            # BlueZ adds required fields. No local name or TX power is added;
            # the manufacturer AD field uses 24 of the legacy 31 bytes.
            "Includes": dbus.Array([], signature="s"),
            "MinInterval": dbus.UInt32(ADVERTISEMENT_INTERVAL_MS),
            "MaxInterval": dbus.UInt32(ADVERTISEMENT_INTERVAL_MS),
        }

    @dbus.service.method("org.bluez.LEAdvertisement1")
    def Release(self):
        # THE ONLY NOTICE WE GET that the advert is gone. When the controller
        # goes away, BlueZ's manager_destroy runs client_destroy on every
        # registered advert, which calls Release here and frees the object
        # (src/advertising.c). The controller comes back with no adverts at
        # all. Without clearing the flag, arm() below still believes we are up
        # and never registers again.
        #
        # Not observed on this hardware: across three reset events the advert
        # was never released. Kept because it is the only notice BlueZ gives,
        # and an advert that is gone without one is expensive to diagnose.
        self.on_release()


def find_adapter(bus):
    """Select a powered BlueZ controller that supports LE advertising."""
    objects = dbus.Interface(bus.get_object("org.bluez", "/"),
                             "org.freedesktop.DBus.ObjectManager").GetManagedObjects()
    candidates = sorted(str(path) for path, interfaces in objects.items()
                        if "org.bluez.LEAdvertisingManager1" in interfaces
                        and interfaces.get("org.bluez.Adapter1", {}).get("Powered", False))
    if not candidates:
        raise RuntimeError("Nessun controller Bluetooth acceso supporta la pubblicita LE")
    return candidates[0]


def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    # follow_name_owner_changes: a bluetoothd restart hands org.bluez to a new
    # owner, and a proxy bound to the old one throws on every later call.
    manager = dbus.Interface(
        bus.get_object("org.bluez", find_adapter(bus), follow_name_owner_changes=True),
        "org.bluez.LEAdvertisingManager1")
    loop = GLib.MainLoop()
    # `pending` exists because RegisterAdvertisement is ASYNCHRONOUS and `up` is
    # only set when its reply arrives. With nothing marking the call in flight, a
    # reply slower than the 3 s timer got a second registration on top of the
    # first: reply one succeeded and set `up`, reply two came back
    # AlreadyExists, and `ko` then reported the advert lost and cleared `up`.
    # From there every tick re-registered, got AlreadyExists again, and printed
    # "advert lost" once every three seconds - while the advert was up and
    # working the whole time. That output is indistinguishable from a real loss,
    # which is the exact diagnostic cost the timer was added to avoid.
    state = {"up": False, "pending": False}

    def ok():
        state["pending"] = False
        if not state["up"]:
            print("advert up", flush=True)
        state["up"] = True

    def ko(error):
        state["pending"] = False
        print("advert error (%s) - re-arming" % error, flush=True)
        state["up"] = False

    def released():
        if state["up"]:
            print("advert released by BlueZ - re-arming", flush=True)
        state["up"] = False

    # THE REFERENCE IS LOAD-BEARING: without it Python collects the object, its
    # D-Bus path disappears, and BlueZ drops the advert without a word.
    adv = Advertisement(bus, PATH, released)

    # A SAFEGUARD, NOT A FIX FOR A KNOWN FAILURE. The combo-chip argument this
    # started from does not survive measurement: across three separate reset
    # events on the MT7922 - a layer 1 radio sweep, an `hci0` power cycle and a
    # `modprobe -r mt7921e` - the advert stayed registered every time, and
    # `Release` was never called. Resetting the Wi-Fi side does not take
    # bluetoothd's advert down here.
    #
    # What is measured, and what actually motivates this script, is on the
    # btmgmt side: after layer 1 `btmgmt add-adv` fails outright with "failed to
    # register the advertising instance", and on the occasions it does register,
    # the phone still does not react to it. Going through
    # LEAdvertisingManager1 avoids both.
    #
    # The timer and the Release handler stay because neither costs anything and
    # an advert that disappears without a word is expensive to diagnose - not
    # because either is answering a failure observed on this hardware.
    def arm():
        if not state["up"] and not state["pending"]:
            state["pending"] = True
            try:
                manager.RegisterAdvertisement(
                    PATH, dbus.Dictionary({}, signature="sv"),
                    reply_handler=ok, error_handler=ko)
            except Exception as exc:
                # Cleared here too: a throw at call time means no reply is
                # coming, and leaving the flag set would wedge the timer shut -
                # trading a false "lost" for a real one nobody ever reports.
                state["pending"] = False
                print("register: %s" % exc, file=sys.stderr, flush=True)
        return True

    arm()
    GLib.timeout_add_seconds(3, arm)

    def stop(*_):
        try:
            manager.UnregisterAdvertisement(PATH)
        except Exception:
            pass
        loop.quit()
        return False

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, stop)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, stop)
    loop.run()


if __name__ == "__main__":
    main()
