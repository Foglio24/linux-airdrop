# Trusted devices and automatic accepting: analysis and decision

**Decision: automatic accepting is not implemented. Every incoming transfer asks for confirmation, from any device, always.**

The requirement was: allow automatic accepting only if the sender's identity can be authenticated reliably, and never rely on a
name, a MAC address or any other easily forged identifier as the only proof. This document records what was examined, why none of
the available identifiers meets that bar, and what would have to change for it to be possible.

## What the receiver can see about a sender

Everything below comes from reading the code in this repository (`backend/python/opendrop`, `secure_receiver.py`) and from
published AirDrop research (Stute et al., USENIX Security 2019; Heinrich et al., "PrivateDrop", PETS 2021). Statements about what
iOS does are **not** re-verified with captures from the author's own phone; "not verified" means exactly that.

| Identifier | Who controls it | Stable between sessions | Usable as proof of identity |
|---|---|---|---|
| `SenderComputerName` (device name) | the owner, free text | yes | **No.** Anyone can name a phone "Anna's iPhone". |
| `SenderModelName` | the sender | yes | **No.** Free text as far as the receiver can tell. |
| `SenderID` / mDNS instance name (12 hex digits) | the sender; OpenDrop picks a random one per run | not for OpenDrop; not verified for iOS | **No.** Unauthenticated, trivially copied. |
| AWDL MAC address and link-local IPv6 address | the sender; the research describes them as randomised per session | no | **No.** Forgeable, and a spoofed one is not detectable here. |
| TLS certificate of the sender | the sender | not verified | **No.** `AirDropConfig.get_ssl_context()` sets `verify_mode = ssl.CERT_NONE` on the server: the receiver does not even ask for a client certificate. (OpenDrop's own comment: "we accept self-signed certificates as does Apple".) |
| `SenderRecordData` (Apple "validation record") | issued by Apple to a signed-in Apple ID | per Apple ID, refreshed periodically (not verified) | **No, see below.** |

### The Apple validation record

Phones signed in to iCloud may attach a CMS-signed record to `/Discover` and `/Ask`, listing hashes of the verified contact
identifiers of the Apple ID. `identity.py` can verify its signature chain against the Apple root certificate bundled with
OpenDrop and derive a stable fingerprint from the hashes; the app uses that fingerprint only to recognise a device in the
*Dispositivi* list ("Riconosciuto da un record firmato da Apple").

It cannot be used to authenticate a sender, for three reasons that follow from how the record is built:

1. **It is a bearer document.** As described in the research, nothing in it is signed by the presenting device, and it is not
   bound to the TLS session or to a challenge chosen by the receiver. Whoever holds a copy can present it again.
2. **Copies can be obtained.** The record travels in the requests a phone sends to the devices it talks to; a device that has
   received one (for example a stranger's, when the user's phone sent a file to it) has a copy for as long as it stays valid.
3. **It identifies an account, not a device**, and it is absent for senders that are not signed in to Apple services.

Also: the layout of the record's payload used by `identity.py` follows published AirDrop research and has **not** been checked
against a record from the author's own phone. If the layout differs, no fingerprint is derived and the device is remembered by
name only, marked as such. The receiver logs the *names* of the fields of every `/Ask` ("Campi della richiesta: …") and whether
a record was attached, so the next real transfer gives evidence either way.

## Why a "pair once, accept always" feature cannot be made safe on this protocol

An automatic-accept list is only as strong as the weakest identifier that can put a device on it. Every identifier above can be
chosen or replayed by an attacker standing within radio range with AirDrop set to "Everyone", which is exactly the situation
the confirmation window protects against. A stranger who copies the name (or replays a record) would then be able to write files
into the user's Downloads folder with no prompt. The confirmation window is the only control that does not depend on any of
these identifiers, so it stays.

Remembering devices (the *Dispositivi* page) is kept because it is harmless: it never changes what is accepted, it only helps the
user recognise a device, and each entry says whether it was recognised by name alone.

## What would make it possible

Any one of these would change the answer, and each needs hardware experiments that were not possible here:

1. **A stable, device-held key that the receiver can challenge.** If iOS presented a client certificate (or any key) that stays the
   same across sessions and proved possession of it in the TLS handshake, the receiver could pin it after one explicit approval.
   This needs a test session where the server *requests* a client certificate (`verify_mode = CERT_OPTIONAL`), recording whether
   iOS sends one, whether it is stable over many sessions and reboots, and whether it survives AirDrop mode changes. That changes
   the TLS handshake of a working setup, so it was deliberately **not** done in this release.
2. **A secret shared out of band.** For example an iOS Shortcut that computes an HMAC over the file name or contents with a key
   the user pasted once, and a receiver that accepts only transfers whose tag verifies. This authenticates the *content* of a
   transfer, not AirDrop itself, and would be a separate, opt-in feature with its own threat model.
3. **A change in Apple's protocol** that binds the validation record to the connection.

Until one of them is demonstrated on real hardware, the answer stays "always ask".

## Where this shows up in the app

* **Dispositivi → Accettazione automatica → Non disponibile** (with the explanation above in plain Italian).
* Every confirmation window says that the sender's name is not verified.
* Nothing in `store.py`, `identity.py` or the UI reads the remembered-devices list when deciding whether to accept: the accept
  decision is `secure_receiver._confirm()` and nothing else.
