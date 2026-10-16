"""Dependency-free helpers shared by the window, the confirmation dialog and the history.

Everything here is pure (no GTK, no I/O) so the receiver, the preview process and the GUI can all
import it. Texts are Italian, like the rest of the interface.
"""
import os
import re
import time
import unicodedata

# kind -> (label, themed icon name)
KINDS = {
    "image": ("Immagine", "image-x-generic-symbolic"),
    "video": ("Video", "video-x-generic-symbolic"),
    "audio": ("Audio", "audio-x-generic-symbolic"),
    "pdf": ("Documento PDF", "x-office-document-symbolic"),
    "document": ("Documento", "x-office-document-symbolic"),
    "text": ("Testo", "text-x-generic-symbolic"),
    "archive": ("Archivio", "package-x-generic-symbolic"),
    "folder": ("Cartella", "folder-symbolic"),
    "link": ("Collegamento web", "emblem-web-symbolic"),
    "contact": ("Contatto", "avatar-default-symbolic"),
    "other": ("File", "text-x-generic-symbolic"),
}

_EXTENSIONS = {
    "image": "jpg jpeg jpe png gif webp heic heif avif bmp tif tiff svg raw dng cr2 nef arw ico jp2",
    "video": "mp4 m4v mov avi mkv webm mpg mpeg 3gp wmv flv ogv",
    "audio": "mp3 m4a aac wav flac ogg oga opus aif aiff wma caf",
    "pdf": "pdf",
    "document": "doc docx odt rtf pages ppt pptx odp key xls xlsx ods numbers csv epub",
    "text": "txt md log json xml yml yaml ini conf html htm py sh c h cpp js css tex",
    "archive": "zip tar gz tgz bz2 xz 7z rar zst cpio dmg iso deb rpm",
    "contact": "vcf",
}
_BY_EXTENSION = {ext: kind for kind, exts in _EXTENSIONS.items() for ext in exts.split()}

# Apple uniform type identifiers; the most specific rule wins, `public.data`/`public.content` fall through.
_UTI_RULES = (
    ("public.folder", "folder"), ("public.directory", "folder"),
    ("com.adobe.pdf", "pdf"),
    ("public.image", "image"), ("public.jpeg", "image"), ("public.png", "image"), ("public.heic", "image"),
    ("public.heif", "image"), ("public.tiff", "image"), ("com.compuserve.gif", "image"), ("public.camera-raw-image", "image"),
    ("public.movie", "video"), ("public.video", "video"), ("public.mpeg", "video"), ("public.mpeg-4", "video"),
    ("com.apple.quicktime-movie", "video"),
    ("public.audio", "audio"), ("public.mp3", "audio"), ("com.apple.m4a-audio", "audio"),
    ("public.zip-archive", "archive"), ("org.gnu.gnu-zip-archive", "archive"), ("public.archive", "archive"),
    ("public.url", "link"),
    ("public.vcard", "contact"),
    ("public.plain-text", "text"), ("public.text", "text"), ("public.html", "text"),
)


def kind_of(name="", uti=None, is_dir=False):
    """image | video | audio | pdf | document | text | archive | folder | link | contact | other."""
    if is_dir:
        return "folder"
    if isinstance(uti, str):
        lowered = uti.lower()
        for prefix, kind in _UTI_RULES:
            if lowered == prefix or lowered.startswith(prefix + "."):
                return kind
    extension = os.path.splitext(str(name))[1].lstrip(".").lower()
    return _BY_EXTENSION.get(extension, "other")


def kind_label(kind):
    return KINDS.get(kind, KINDS["other"])[0]


def kind_icon(kind):
    return KINDS.get(kind, KINDS["other"])[1]


# Controls, the bidirectional overrides used to disguise extensions ("photo‮gpj.exe") and zero-width characters.
_HIDDEN = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿]")


def clean_text(value, limit=200):
    """Text from the other device, made safe to show: no control or bidi characters, bounded length."""
    if not isinstance(value, str):
        value = "" if value is None else str(value)
    value = _HIDDEN.sub("", value)
    value = "".join(" " if unicodedata.category(c) in ("Cc", "Cs", "Co") else c for c in value)
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > limit:
        value = value[:limit - 1].rstrip() + "…"
    return value


def _decimal(number, places=1):
    text = f"{number:.{places}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", ",")


def format_size(size):
    """Decimal units, as GNOME shows them (1 MB = 1 000 000 bytes)."""
    if size is None or size < 0:
        return "—"
    if size < 1000:
        return f"{int(size)} byte"
    value = float(size)
    for unit in ("kB", "MB", "GB", "TB"):
        value /= 1000.0
        if value < 1000 or unit == "TB":
            return f"{_decimal(value, 0 if value >= 100 else 1)} {unit}"
    return "—"


def format_speed(bytes_per_second):
    if bytes_per_second is None:
        return "—"
    if bytes_per_second < 1000:
        return f"{int(bytes_per_second)} B/s"
    return format_size(bytes_per_second).replace(" kB", " kB/s").replace(" MB", " MB/s").replace(" GB", " GB/s")


def format_duration(seconds):
    """'circa 12 s', '2 min 5 s', '1 h 3 min'."""
    if seconds is None:
        return "—"
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {rest} s" if rest else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


_MONTHS = ("gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic")


def format_when(timestamp, now=None):
    """'oggi 14:32', 'ieri 09:10', '12 ott 18:45', '3 mar 2025'."""
    when = time.localtime(timestamp)
    today = time.localtime(now if now is not None else time.time())
    clock = time.strftime("%H:%M", when)
    if (when.tm_year, when.tm_yday) == (today.tm_year, today.tm_yday):
        return "oggi " + clock
    yesterday = time.localtime((now if now is not None else time.time()) - 86400)
    if (when.tm_year, when.tm_yday) == (yesterday.tm_year, yesterday.tm_yday):
        return "ieri " + clock
    if when.tm_year == today.tm_year:
        return f"{when.tm_mday} {_MONTHS[when.tm_mon - 1]} {clock}"
    return f"{when.tm_mday} {_MONTHS[when.tm_mon - 1]} {when.tm_year}"


def summarize_names(names, limit=2):
    """'foto.jpg', 'foto.jpg, nota.txt', 'foto.jpg, nota.txt e altri 3'."""
    names = [clean_text(n, 120) for n in names if n]
    if not names:
        return "Nessun nome"
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f" e altri {len(names) - limit}"


def device_label(model):
    """A friendly name for an Apple model string such as 'iPhone15,2' or 'MacBookPro'."""
    model = clean_text(model or "", 64)
    lowered = model.lower()
    for prefix, label in (("iphone", "iPhone"), ("ipad", "iPad"), ("ipod", "iPod touch"), ("macbook", "Mac portatile"),
                          ("imac", "iMac"), ("macmini", "Mac mini"), ("macpro", "Mac Pro"), ("mac", "Mac"),
                          ("watch", "Apple Watch")):
        if lowered.startswith(prefix):
            return label
    return model or "Dispositivo Apple"
