"""Stub StatusNotifierWatcher (under a unique name, so the real tray is untouched) plus a client that
behaves like the GNOME host. Prints one JSON document describing what the host saw; test_tray.py asserts on it.
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gi.repository import Gio, GLib  # noqa: E402
import tray  # noqa: E402

WATCHER_XML = """<node><interface name="org.kde.StatusNotifierWatcher">
  <method name="RegisterStatusNotifierItem"><arg type="s" name="service" direction="in"/></method>
  <property name="IsStatusNotifierHostRegistered" type="b" access="read"/>
</interface></node>"""

loop = GLib.MainLoop()
conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
watcher_name = f"org.kde.StatusNotifierWatcher.Test{os.getpid()}"
registered, clicks, availability = [], [], []
report = {}


def watcher_call(connection, sender, path, interface, method, parameters, invocation):
    registered.append(parameters.unpack()[0])
    invocation.return_value(None)


conn.register_object("/StatusNotifierWatcher", Gio.DBusNodeInfo.new_for_xml(WATCHER_XML).interfaces[0], watcher_call,
                     lambda *a: GLib.Variant("b", True), None)
icon = tray.TrayIcon("Linux AirDrop", ROOT / "icons", {"idle": "idle-symbolic", "active": "active-symbolic"},
                     lambda: clicks.append("open"), lambda: clicks.append("toggle"), lambda: clicks.append("quit"),
                     watcher=watcher_name)


def host():
    """What a tray host does, from a separate peer so the shell and the app never share a thread."""
    try:
        address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
        client = Gio.DBusConnection.new_for_address_sync(
            address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
        name = registered[0]

        def get(prop):
            return client.call_sync(name, tray.ITEM_PATH, "org.freedesktop.DBus.Properties", "Get",
                                    GLib.Variant("(ss)", ("org.kde.StatusNotifierItem", prop)), None,
                                    Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]

        def menu(method, args, signature):
            return client.call_sync(name, tray.MENU_PATH, "com.canonical.dbusmenu", method, GLib.Variant(signature, args), None,
                                    Gio.DBusCallFlags.NONE, 3000, None).unpack()

        report["registered_name"] = name
        report["props"] = {p: get(p) for p in ("Category", "Id", "Title", "Status", "IconName", "IconThemePath", "ItemIsMenu", "Menu")}
        report["tooltip"] = get("ToolTip")
        report["layout_idle"] = menu("GetLayout", (0, -1, []), "(iias)")
        report["group"] = menu("GetGroupProperties", ([4], []), "(aias)")
        report["about_to_show"] = menu("AboutToShow", (0,), "(i)")
        menu("Event", (1, "clicked", GLib.Variant("i", 0), 0), "(isvu)")
        menu("Event", (4, "hovered", GLib.Variant("i", 0), 0), "(isvu)")   # must be ignored
        menu("Event", (4, "clicked", GLib.Variant("i", 0), 0), "(isvu)")
        menu("EventGroup", ([(6, "clicked", GLib.Variant("i", 0), 0)],), "(a(isvu))")
        GLib.idle_add(lambda: (icon.set_state(True, False, "Visibile · pronto a ricevere"), False)[1])
        time.sleep(0.4)
        report["icon_active"] = get("IconName")
        report["layout_active"] = menu("GetLayout", (0, -1, []), "(iias)")
    except Exception as exc:  # reported to the test instead of hanging it
        report["error"] = repr(exc)
    GLib.idle_add(loop.quit)


def availability_changed(value):
    availability.append(value)
    if value and len(availability) == 1:
        threading.Thread(target=host, daemon=True).start()


icon.on_availability = availability_changed
Gio.bus_own_name_on_connection(conn, watcher_name, Gio.BusNameOwnerFlags.NONE, lambda c, n: icon.start(c), None)
GLib.timeout_add_seconds(15, loop.quit)
loop.run()
report["clicks"] = clicks
report["availability"] = list(availability)
icon.stop()
report["availability_after_stop"] = list(availability)
print(json.dumps(report, default=list))
