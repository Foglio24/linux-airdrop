# Features added in 1.1

The user interface is Italian; names in quotes below are the labels you will see. All new data stays on the computer, in
`~/.local/state/linux-airdrop/` (history, repair state) and `~/.config/linux-airdrop/settings.json`.

## Transfer history ("Cronologia")

A page in the main window lists every send and receive: device name, direction, file names and kinds, total size, date and time,
and the outcome (*Completato*, *Fallito*, *Annullato*, and *Rifiutato* for requests that were declined). Each row opens to show
the files; buttons open the file or the folder it is in. "Cancella cronologia" empties the list (received files are not touched).

Storage is a SQLite database, `history.db`, created with mode 0600, pruned to the last 1000 entries and 365 days. It holds file
**names, kinds, sizes and the location on this computer**, the other device's display name and model, and times. It never holds
file contents, thumbnails, network or MAC addresses, keys or certificates. The switch "Ricorda i trasferimenti" turns recording off
(existing entries stay until cleared). A transfer left "in corso" by a crash is marked failed at the next start. A damaged
database is moved aside (`history.db.damaged-…`) and a new one started.

## Drag and drop

Drop one or more files or folders anywhere on the window; a dashed accent-coloured area marks where. They go through the same path
as "Invia file…": checked (missing, unreadable or special files are listed and skipped; limit 1 GB per transfer because the archive
is built in memory), queued, and sent once the Apple device has been chosen. If the PC is not visible yet the app asks before
turning AirDrop on. Items that are not local files (for example a browser image) are refused with a message.

**Folders** can be sent two ways, chosen in a dialog: *Come ZIP* (default; the folder becomes one `.zip` file, which is the
verified file path) or *Come cartella* (native AirDrop folder entry with its contents, like Finder does). The native form is
unit- and loopback-tested but **has not been tried with a real iPhone**, hence "sperimentale". Symbolic links and special files
inside a folder are never followed or sent. Receiving a folder keeps working as before: files are saved flat, as for iOS receipts.

## Preview before accepting

The confirmation window now shows what the request actually contains before any file is sent: the sender's name and model, the
files with an icon for their type (image, video, audio, PDF, document, archive, folder, link…), sizes when the sender states them,
and the thumbnail the sender attaches to the request (`FileIcon`, normally for the first photo). When a size is not given the
window says so instead of guessing. A caption says the sender's name is not verified.

Safety: the preview is built only from the request itself; no received file is opened or run. Text from the other device is
cleaned of control and direction-override characters. The thumbnail is decoded by a separate short-lived process
(`preview.py`) with memory, CPU, file-size and image-size limits, accepting only JPEG 2000, JPEG and PNG; the window then draws raw
pixels, so no image-loader plug-in sees data from the sender. If anything fails there is simply no thumbnail.

**PDF/document previews are not possible before accepting**: AirDrop sends the content only after the receiver accepts, so there
is nothing to render. Types are shown by icon and name.

## GNOME Files: "Invia con Linux AirDrop"

`nautilus/linux_airdrop_nautilus.py` adds the entry to the context menu of files, folders and mixed selections (local files only).
It needs the `python3-nautilus` package (recommended by the `.deb`); restart Files with `nautilus -q`. Without that package,
`install-user.py` also installs a Nautilus script (right-click → *Script* → *Invia con Linux AirDrop*) that does the same.
Both launch the app through its desktop entry with the files as arguments. The application is a single instance: a second launch
(`linux-airdrop FILE…`, the menu entry, the script) forwards the files to the running one, so no second AirDrop session is started.

## Live progress

The "Trasferimenti" section shows one row per transfer: progress bar, percentage, bytes moved, speed, estimated time left and the
state (*In attesa della tua conferma*, *In attesa che l'altro dispositivo accetti*, *Preparazione*, in progress, *Completato*,
*Non riuscito*, *Annullato*, *Rifiutato*). Numbers come from the real byte counters of the transfer processes:

* receiving: bytes read from the connection, against the size the sender announces (`Content-Length` or Apple's `TotalBytes`);
* sending: bytes handed to the network after the archive is built, against the archive size.

Speed is averaged over the last six seconds and drops to "fermo" after three seconds without data. If the total is unknown, or the
counter passes the announced total, there is **no** percentage or time estimate, only bytes and speed. Sending reports bytes
accepted by the kernel's send buffer, so it can run slightly ahead of what the phone has received. Several transfers can be listed
at once; sends are queued one at a time. Cancel works for sends (the process is stopped) and receives (the receiver stops reading
and deletes the partial data); the other device then sees the transfer fail.

## Remembered devices ("Dispositivi")

Devices you actually exchanged files with are listed with their last connection, last transfer and counts, and can be forgotten one
by one or all together. A device is only remembered after you accepted its request, or after it accepted yours; senders you
declined and phones merely nearby are not stored. The identifier is a fingerprint of Apple's signed record when the sender attached
one and it could be read, otherwise **only the display name**; each entry says which, and a name alone is never treated as proof
of identity. See [TRUSTED-DEVICES.md](TRUSTED-DEVICES.md) for why nothing is accepted automatically.

## Diagnostics ("Diagnostica")

Live checks, refreshed every few seconds while the page is open, each with a green / yellow / red / grey (not in use) indicator, a
plain explanation and what to do: MediaTek card, kernel, wireless driver, modified modules, OWL, AWDL, Wi-Fi Direct, mDNS,
Bluetooth, selected interface (with interface indexes), wireless channel, the AirDrop service, and the passwordless helper.
"Copia rapporto" puts a text report on the clipboard with MAC addresses, link-local IPv6 addresses, home folder and user name
masked. `linux-airdrop --diagnose` prints the same report in a terminal (read-only; exit status 1 on errors).

Checks only read `/sys`, `/proc` and the output of `iw dev`, `systemctl is-active bluetooth` and `bluetoothctl show`, with short
time limits, off the main thread, so a stuck driver cannot freeze the window.

## Guided repair

Known faults are recognised and, where it is safe, fixed:

| Fault | Fix | Asks first |
|---|---|---|
| mDNS announcing on a stale interface index (awdl0 recreated) | restart the receiver | no |
| Receiver stopped / not announcing | restart the receiver | no |
| Bluetooth wake-up signal stopped | restart that signal | no |
| OWL not running, AWDL missing, Wi-Fi Direct group not created | stop (normal restore path) and start the session again | yes |
| Leftover app processes of this user without a session | end them | yes |
| Leftover root-owned process, mixed driver modules, blocked driver | explain; only a reboot is safe | – |

Nothing changes system configuration or runs as root. Repairs are limited to 3 attempts per problem in 30 minutes, with a
15-second pause between them (state in `repair-state.json`), so a persistent fault ends in an explanation, never in a restart loop.
Every attempt is written to `repair.log` (page *Diagnostica* → *Registro delle riparazioni*). "Ripara da sola i problemi
semplici" lets the app apply, on its own, only the three no-confirmation fixes that address *certain* faults (changed interface
index, dead receiver, dead Bluetooth signal); a silent-announcements warning is only ever fixed on request, because restarting the
receiver makes the phone show a stale entry for up to two minutes.

There is deliberately **no forced root-level restore of the Wi-Fi card**: that path (module unloading while interfaces exist)
caused a kernel fault in earlier tests and cannot be validated here. The normal stop already restores the card; after a driver
lock-up the app asks for a reboot, as before.
