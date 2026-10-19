Native AirDrop backend for Ubuntu 26.10 amd64

The OWL and OpenDrop binaries are used by the app's interface.
Do not start OWL directly: the Wi-Fi interfaces need the privileged
bridge and a network restore. OpenDrop runs as a normal user.

./opendrop --help shows the full CLI; ./owl does not change the network
when no interface is given. The OpenDrop launcher loads the
secure_receiver.py protections shipped in the parent directory.

Runtime: /usr/bin/python3, CPython 3.14 ABI, libarchive.so.13; OWL uses
libpcap.so.0.8 and libnl-{3,genl-3,route-3}.so.200 from the system. libev 4.33
is linked statically. No dependency is installed into the system.

Exact versions and pins: manifest.json. Wheel hashes: wheels.lock.json.
Licences: licenses/ and python/*dist-info/. OWL and libev sources: sources/.
The seventeen OpenDrop patches are kept in sources/opendrop-patches.

OWL build: GCC 15.3.0, CMake 4.3.4, Release, -fno-strict-aliasing,
CMAKE_POLICY_VERSION_MINIMUM=3.5, CMAKE_SKIP_RPATH=TRUE, -lm.
Local patch owl-gcc15-safety.patch: initialises Netlink cleanup pointers
and missing radiotap metadata, checks chdir during daemonize.
The 65 upstream unit tests pass; the bundled googletest needs
CMAKE_CXX_FLAGS='-include cstdint -Wno-error=maybe-uninitialized'.

Radio compatibility with iOS 27 was first verified with a real transfer on
15 October 2026 (one MT7922, one iPhone); see ../verification.json.
The upstream fork documents tests on MT7921/MT7922 and iOS 26.

Changes to the vendored OpenDrop in 1.1 (small, additive; the rest of the protocol code is as before):
- opendrop/client.py: a folder passed to send is announced as a public.folder entry and travels with its
  contents (symbolic links and special files are skipped); a plain file produces exactly the archive it always did.
  Two optional class hooks, start_hook and progress_hook, report real byte counts (used by secure_receiver.py).
- opendrop/cli.py: send accepts folders as well as files.
Everything else (events for the window, progress, cancelling a receive, the preview of the request) lives in the
parent directory: secure_receiver.py, transfer_events.py, preview.py, identity.py.

owl-nexa (customised OWL, built from owl-src/ with its Makefile): see ../docs/OWL-NEXA.md. owl-modern and owl-original stay
as they were; owl-modern is the fallback when owl-nexa cannot start.

The 19 October 2026 owl-nexa core rewrite is built with backend/owl-src/Makefile.
Run `make check` and `make sanitize` there; verification and limits are documented
in ../docs/OWL-NEXA.md and ../docs/OWL-VERIFICATION.json.
