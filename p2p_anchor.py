"""An owned, temporary Wi-Fi Direct GO supplies the monitor transmit channel.

The caller releases the selected station from NetworkManager first and restores
its original state afterwards. No global supplicant control or PID files are used.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

SAFE_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
RUNTIME_BASES = ("/run", "/tmp")
GLOBAL_CONTROL = Path("/run/wpa_supplicant")
IPV6_CONF = Path("/proc/sys/net/ipv6/conf")
NAME = re.compile(r"[A-Za-z0-9_.:-]{1,15}\Z")
MAC = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\Z")


def interfaces(output):
    """Parse iw's phy/type identity, without assuming P2P interface names."""
    result, phy, current = [], None, None
    for raw in output.splitlines():
        line = raw.strip()
        match = re.fullmatch(r"phy#(\d+)", line)
        if match:
            phy, current = "phy" + match[1], None
        elif line == "Unnamed/non-netdev interface":
            current = None
        elif line.startswith("Interface "):
            name = line[len("Interface "):]
            if phy is None or not NAME.fullmatch(name):
                raise RuntimeError("Elenco delle interfacce Wi-Fi non valido")
            current = {"phy": phy, "name": name}
            result.append(current)
        elif current is not None:
            for key, prefix in (("type", "type "), ("mac", "addr "), ("wdev", "wdev ")):
                if line.startswith(prefix):
                    current[key] = line[len(prefix):].lower() if key == "mac" else line[len(prefix):]
    return result


class GoAnchor:
    def __init__(self, iface, phy, channel, command, emit):
        if not NAME.fullmatch(iface) or not re.fullmatch(r"phy\d+", phy):
            raise RuntimeError("Interfaccia Wi-Fi Direct non valida")
        if channel != 6:
            raise RuntimeError("La modalità Wi-Fi Direct richiede il canale 6")
        self.iface, self.phy, self.channel = iface, phy, channel
        self.command, self.emit = command, emit
        self.process = None
        self.runtime = None
        self.log = None
        self.group = None
        self.before = None
        self.executables = {}
        self.log_reported = False

    def _inventory(self):
        return interfaces(self.command(["iw", "dev"]))

    def preflight(self):
        """Read-only checks; call before unloading drivers or changing NM."""
        if os.geteuid() != 0:
            raise RuntimeError("Wi-Fi Direct deve essere avviato dall'app con autenticazione Ubuntu")
        for name in ("wpa_supplicant", "wpa_cli"):
            path = shutil.which(name, path=SAFE_ENV["PATH"])
            if not path:
                raise RuntimeError("Programma necessario per Wi-Fi Direct mancante: " + name)
            self.executables[name] = path
        found = self._inventory()
        if not any(i["name"] == self.iface and i["phy"] == self.phy and i.get("type") == "managed" for i in found):
            raise RuntimeError("La scheda Wi-Fi selezionata non è in modalità normale")
        if any(i["phy"] == self.phy and i.get("type") == "P2P-GO" for i in found):
            raise RuntimeError("Una sessione Wi-Fi Direct è già attiva sulla scheda selezionata")
        capabilities = self.command(["iw", "phy", self.phy, "info"])
        if not re.search(r"^\s*\* P2P-GO\s*$", capabilities, re.MULTILINE):
            raise RuntimeError("La scheda Wi-Fi non supporta Wi-Fi Direct P2P-GO")
        return found

    def _cli(self, iface, *args, check=True):
        output = self.command([self.executables["wpa_cli"], "-p", str(self.runtime / "control"),
                               "-i", iface, *args], check=check)
        if check and any(line.strip().startswith("FAIL") for line in output.splitlines()):
            raise RuntimeError("Wi-Fi Direct: " + output.strip()[:250])
        return output.strip()

    def _make_runtime(self):
        last_error = None
        for base in RUNTIME_BASES:
            try:
                self.runtime = Path(tempfile.mkdtemp(prefix="linux-airdrop-p2p-", dir=base))
                break
            except OSError as exc:
                last_error = exc
        else:
            raise RuntimeError("Impossibile creare la cartella temporanea Wi-Fi Direct") from last_error
        os.chmod(self.runtime, 0o700)
        (self.runtime / "control").mkdir(mode=0o700)
        config = self.runtime / "supplicant.conf"
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(f"ctrl_interface={self.runtime / 'control'}\nupdate_config=0\n"
                         "device_name=LinuxAirDrop\ndevice_type=1-0050F204-1\np2p_go_intent=15\n")
        fd = os.open(self.runtime / "supplicant.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.log = os.fdopen(fd, "w")
        return config

    def _keep_out_of_network_manager(self, name):
        """Best effort: NetworkManager usually never registers a P2P-GO netdev.

        On the MT7922 a live run showed `nmcli device set <group> managed no`
        failing with "Device not found", which aborted a group that was
        otherwise up. Only act when NetworkManager really lists the device as
        managed, and never let this step fail the session.
        """
        try:
            if self.command(["nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", name],
                            check=False).strip() == "yes":
                self.command(["nmcli", "device", "set", name, "managed", "no"])
        except RuntimeError as exc:
            self.emit("log", source="wifi-direct", message="NetworkManager non gestisce il gruppo Wi-Fi Direct: " + str(exc)[:200])

    def report_supplicant_log(self):
        """Keep the daemon's last lines in the app log before the runtime dir is removed."""
        if self.runtime is None or self.log_reported:
            return
        self.log_reported = True
        try:
            if self.log is not None:
                self.log.flush()
            text = (self.runtime / "supplicant.log").read_bytes()[-6000:].decode("utf-8", "replace")
        except OSError:
            return
        for line in [l for l in text.splitlines() if l.strip()][-15:]:
            # The throwaway group is never joined, but its passphrase is still not log material.
            line = re.sub(r'(passphrase|psk)=("[^"]*"|\S+)', r"\1=<nascosto>", line)
            self.emit("log", source="wpa_supplicant", message=line[:300])

    def _drop_group_ipv6(self, name):
        """The group shares its MAC with awdl0, so it would hold the same fe80:: address.

        Two interfaces with one address made zeroconf announce AirDrop on the group
        (lower ifindex) instead of awdl0. The group never needs IPv6; best effort.
        """
        try:
            (IPV6_CONF / name / "disable_ipv6").write_text("1\n")
        except FileNotFoundError:
            pass
        except OSError as exc:
            self.emit("log", source="wifi-direct", message="IPv6 non disattivato sul gruppo Wi-Fi Direct: " + str(exc)[:200])

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def start(self):
        if self.process is not None or self.runtime is not None:
            raise RuntimeError("Il collegamento Wi-Fi Direct è già stato avviato")
        found = self.preflight()
        deadline = time.monotonic() + 8
        # NetworkManager must first release its interface. Never send commands to
        # the system supplicant or launch a second controller on its station.
        release_deadline = time.monotonic() + 4
        while (GLOBAL_CONTROL / self.iface).exists():
            if time.monotonic() >= release_deadline:
                raise RuntimeError("La scheda Wi-Fi è ancora controllata dal servizio di sistema")
            time.sleep(0.1)
        self.before = {i["name"] for i in found}
        try:
            config = self._make_runtime()
            self.command(["ip", "link", "set", self.iface, "up"])
            # -C only applies without -c in wpa_supplicant 2.11; the isolated
            # ctrl_interface is set in our private config instead.
            self.process = subprocess.Popen(
                [self.executables["wpa_supplicant"], "-Dnl80211", "-i", self.iface, "-c", str(config)],
                env=SAFE_ENV, stdin=subprocess.DEVNULL, stdout=self.log,
                stderr=subprocess.STDOUT, close_fds=True)
            deadline = time.monotonic() + 8
            while True:
                if not self.alive():
                    raise RuntimeError("Il servizio Wi-Fi Direct non si è avviato")
                if (self.runtime / "control" / self.iface).exists():
                    if self._cli(self.iface, "ping", check=False) == "PONG":
                        break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Il servizio Wi-Fi Direct non risponde")
                time.sleep(0.1)
            if self._cli(self.iface, "p2p_group_add", "freq=2437") != "OK":
                raise RuntimeError("Impossibile creare il gruppo Wi-Fi Direct")
            deadline = time.monotonic() + 8
            observed_group = False
            while time.monotonic() < deadline:
                if not self.alive():
                    raise RuntimeError("Il servizio Wi-Fi Direct si è interrotto")
                candidates = [i for i in self._inventory() if i["phy"] == self.phy
                              and i.get("type") == "P2P-GO" and i["name"] not in self.before]
                if len(candidates) > 1:
                    raise RuntimeError("Più gruppi Wi-Fi Direct rilevati: impossibile attribuirli alla prova")
                if candidates:
                    observed_group = True
                    group = candidates[0]
                    mac = group.get("mac", "")
                    if not MAC.fullmatch(mac) or int(mac[:2], 16) & 1 or mac == "00:00:00:00:00:00":
                        raise RuntimeError("Indirizzo del gruppo Wi-Fi Direct non valido")
                    if not re.fullmatch(r"0x[0-9a-fA-F]+", group.get("wdev", "")):
                        raise RuntimeError("Identità del gruppo Wi-Fi Direct non disponibile")
                    # This private control socket establishes ownership, beyond
                    # merely observing an interface appearing on the same phy.
                    status = dict(line.split("=", 1) for line in self._cli(group["name"], "status", check=False).splitlines() if "=" in line)
                    if status.get("mode") != "P2P GO" or status.get("address", "").lower() != mac:
                        # The netdev precedes its control socket, and status can
                        # initially report "P2P GO - group formation".
                        time.sleep(0.1)
                        continue
                    if status.get("freq") != "2437":
                        if status.get("freq"):
                            raise RuntimeError("Il gruppo Wi-Fi Direct usa un canale diverso da quello richiesto")
                        time.sleep(0.1)
                        continue
                    self.group = group
                    self._keep_out_of_network_manager(group["name"])
                    self._drop_group_ipv6(group["name"])
                    self.emit("status", message="Collegamento Wi-Fi Direct temporaneo pronto sul canale 6.")
                    return group["name"], mac
                time.sleep(0.1)
            if observed_group:
                raise RuntimeError("Il gruppo Wi-Fi Direct non è pronto o non appartiene al servizio della prova")
            raise RuntimeError("Il servizio non ha creato il gruppo Wi-Fi Direct")
        except Exception:
            self.cleanup()
            raise

    def cleanup(self):
        """Return False if radio objects or a live owned daemon prevent unload."""
        if self.runtime is None and self.process is None:
            return True
        self.report_supplicant_log()
        if self.process is not None:
            try:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=1)
                if self.process.poll() is None:
                    raise RuntimeError("Il servizio Wi-Fi Direct è ancora attivo")
            except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
                self.emit("error", message="Arresto Wi-Fi Direct incompleto: " + str(exc))
                return False
        try:
            found = self._inventory() if self.before is not None else []
            if self.group is not None:
                current = next((i for i in found if i["name"] == self.group["name"]), None)
                if current is not None:
                    if any(current.get(k) != self.group.get(k) for k in ("phy", "type", "mac", "wdev")):
                        raise RuntimeError("L'interfaccia Wi-Fi Direct è stata sostituita: non verrà rimossa")
                    self.command(["iw", "dev", current["name"], "del"])
                    found = self._inventory()
                    if any(i["name"] == current["name"] for i in found):
                        raise RuntimeError("Il gruppo Wi-Fi Direct non è stato rimosso")
            # A group created just before startup failure may not have been
            # identified through our control socket. Preserve it, block unload.
            if self.before is not None and any(i["phy"] == self.phy and i.get("type") == "P2P-GO"
                                              and i["name"] not in self.before for i in found):
                raise RuntimeError("Un gruppo Wi-Fi Direct non attribuibile alla prova è ancora presente")
        except Exception as exc:
            self.emit("error", message="Ripristino Wi-Fi Direct incompleto: " + str(exc))
            return False
        if self.log is not None:
            self.log.close()
            self.log = None
        if self.runtime is not None:
            try:
                shutil.rmtree(self.runtime)
            except OSError as exc:
                self.emit("error", message="Pulizia della cartella Wi-Fi Direct incompleta: " + str(exc))
                return False
            self.runtime = None
        self.process, self.group, self.before = None, None, None
        self.log_reported = False
        return True
