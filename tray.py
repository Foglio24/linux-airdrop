"""System tray icon for GNOME: a StatusNotifierItem with a dbusmenu, spoken straight over D-Bus.

GNOME Shell has no tray of its own; Ubuntu ships the AppIndicator extension, which hosts
StatusNotifierItems. Doing the small D-Bus protocol here avoids a GTK 3 AppIndicator library
(which cannot share a process with GTK 4). The icon only exists while that host is available.
"""
import os
from gi.repository import Gio, GLib

WATCHER = "org.kde.StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"

ITEM_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu"><arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/></method>
    <method name="Activate"><arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/></method>
    <method name="SecondaryActivate"><arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/></method>
    <method name="Scroll"><arg type="i" name="delta" direction="in"/><arg type="s" name="orientation" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg type="s" name="status"/></signal>
  </interface>
</node>"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/>
      <arg type="i" name="parent"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg type="i" name="id"/>
      <arg type="u" name="timestamp"/>
    </signal>
  </interface>
</node>"""

SHOW, SEP1, STATE, TOGGLE, SEP2, QUIT = 1, 2, 3, 4, 5, 6


class TrayIcon:
    """Icon plus a small menu: open the window, show/stop visibility, quit."""

    def __init__(self, title, icon_dir, icons, on_open, on_toggle, on_quit, on_availability=None, watcher=WATCHER):
        self.title, self.icon_dir, self.icons = title, str(icon_dir), icons
        self.watcher = watcher
        self.on_open, self.on_toggle, self.on_quit = on_open, on_toggle, on_quit
        self.on_availability = on_availability
        self.visible, self.busy = False, False
        self.state_text = "Non visibile"
        self.revision = 1
        self.available = False
        self.connection = None
        self.registered = False
        self.owner_id = self.watch_id = 0
        self.item_id = self.menu_id = 0
        self.bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"

    # ------------------------------------------------------------------ lifecycle
    def start(self, connection=None):
        self.connection = connection or Gio.bus_get_sync(Gio.BusType.SESSION, None)
        item_info = Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0]
        menu_info = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self.item_id = self.connection.register_object(ITEM_PATH, item_info, self.item_call, self.item_get, None)
        self.menu_id = self.connection.register_object(MENU_PATH, menu_info, self.menu_call, self.menu_get, None)
        self.owner_id = Gio.bus_own_name_on_connection(self.connection, self.bus_name, Gio.BusNameOwnerFlags.NONE, None, None)
        self.watch_id = Gio.bus_watch_name_on_connection(self.connection, self.watcher, Gio.BusNameWatcherFlags.NONE,
                                                         self.watcher_appeared, self.watcher_vanished)

    def stop(self):
        if self.connection is None:
            return
        if self.watch_id:
            Gio.bus_unwatch_name(self.watch_id)
        if self.owner_id:
            Gio.bus_unown_name(self.owner_id)
        for registered in (self.item_id, self.menu_id):
            if registered:
                self.connection.unregister_object(registered)
        self.watch_id = self.owner_id = self.item_id = self.menu_id = 0
        self.connection = None
        self.set_available(False)

    def watcher_appeared(self, connection, name, owner):
        self.connection.call(self.watcher, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                             GLib.Variant("(s)", (self.bus_name,)), None, Gio.DBusCallFlags.NONE, 3000, None,
                             self.registration_done)

    def registration_done(self, connection, result):
        try:
            connection.call_finish(result)
        except GLib.Error:
            self.set_available(False)
            return
        self.registered = True
        self.set_available(True)

    def watcher_vanished(self, connection, name):
        self.registered = False
        self.set_available(False)

    def set_available(self, value):
        if value != self.available:
            self.available = value
            if self.on_availability:
                self.on_availability(value)

    # ---------------------------------------------------------------------- state
    def set_state(self, visible, busy, text):
        changed = (visible, busy, text) != (self.visible, self.busy, self.state_text)
        self.visible, self.busy, self.state_text = visible, busy, text
        if not changed or self.connection is None:
            return
        self.revision += 1
        self.emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewIcon", None)
        self.emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewToolTip", None)
        self.emit(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated", GLib.Variant("(ui)", (self.revision, 0)))

    def emit(self, path, interface, signal, parameters):
        try:
            self.connection.emit_signal(None, path, interface, signal, parameters)
        except GLib.Error:
            pass

    def icon_name(self):
        return self.icons["active" if self.visible else "idle"]

    # ------------------------------------------------------------- item interface
    def item_get(self, connection, sender, path, interface, name):
        pixmap = GLib.Variant("a(iiay)", [])
        values = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", "linux-airdrop"),
            "Title": GLib.Variant("s", self.title),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconThemePath": GLib.Variant("s", self.icon_dir),
            "IconName": GLib.Variant("s", self.icon_name()),
            "IconPixmap": pixmap,
            "OverlayIconName": GLib.Variant("s", ""),
            "OverlayIconPixmap": pixmap,
            "AttentionIconName": GLib.Variant("s", ""),
            "AttentionIconPixmap": pixmap,
            "AttentionMovieName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", (self.icon_name(), [], self.title, self.state_text)),
            "ItemIsMenu": GLib.Variant("b", True),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(name)

    def item_call(self, connection, sender, path, interface, method, parameters, invocation):
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(self.on_open)
        invocation.return_value(None)

    # ------------------------------------------------------------ menu interface
    def entries(self):
        return {
            SHOW: {"label": GLib.Variant("s", "Apri Linux AirDrop")},
            SEP1: {"type": GLib.Variant("s", "separator")},
            STATE: {"label": GLib.Variant("s", self.state_text), "enabled": GLib.Variant("b", False)},
            TOGGLE: {"label": GLib.Variant("s", "Interrompi visibilità" if self.visible else "Rendi visibile"),
                     "enabled": GLib.Variant("b", not self.busy)},
            SEP2: {"type": GLib.Variant("s", "separator")},
            QUIT: {"label": GLib.Variant("s", "Esci")},
        }

    def node(self, ident, depth):
        if ident == 0:
            children = []
            if depth != 0:
                children = [GLib.Variant("(ia{sv}av)", self.node(i, depth - 1 if depth > 0 else depth)) for i in sorted(self.entries())]
            return (0, {"children-display": GLib.Variant("s", "submenu")}, children)
        return (ident, self.entries().get(ident, {}), [])

    def menu_get(self, connection, sender, path, interface, name):
        return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [])}.get(name)

    def menu_call(self, connection, sender, path, interface, method, parameters, invocation):
        args = parameters.unpack()
        if method == "GetLayout":
            parent, depth = args[0], args[1]
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, self.node(parent, depth))))
        elif method == "GetGroupProperties":
            ids = args[0] or [0, *self.entries()]
            known = {0: {}, **self.entries()}
            invocation.return_value(GLib.Variant("(a(ia{sv}))", ([(i, known[i]) for i in ids if i in known],)))
        elif method == "GetProperty":
            props = self.entries().get(args[0], {})
            if args[1] in props:
                invocation.return_value(GLib.Variant("(v)", (props[args[1]],)))
            else:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.InvalidArgs", "Unknown property")
        elif method == "Event":
            self.dispatch(args[0], args[1])
            invocation.return_value(None)
        elif method == "EventGroup":
            for ident, kind, _, _ in args[0]:
                self.dispatch(ident, kind)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def dispatch(self, ident, kind):
        if kind != "clicked":
            return
        callback = {SHOW: self.on_open, TOGGLE: self.on_toggle, QUIT: self.on_quit}.get(ident)
        if callback is not None:
            GLib.idle_add(lambda: (callback(), False)[1])
