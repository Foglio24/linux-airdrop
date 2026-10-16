"""What can be shown about an incoming transfer before the user accepts it.

The /Ask request of AirDrop carries, before any file is sent: the sender's display name and model, one
entry per file (name, Apple type identifier, directory flag, sometimes a size), links, and a small
thumbnail (`FileIcon`, a JPEG 2000 image) of the first item. Nothing else is available before the user
agrees, so no file content is ever opened for a preview: PDFs, videos and documents get an icon chosen
from their declared type and name, never a rendering.

`describe()` is plain Python on already-parsed values. The only parser of untrusted binary data is the
thumbnail decoder, and it runs in a separate short-lived process (`python3 -I preview.py thumb IN OUT`)
with address-space, CPU, file-size and image-size limits. It only decodes one still image to raw pixels;
it executes nothing and writes nothing but the output file. If anything goes wrong there is simply no
thumbnail and the confirmation window is shown without it.
"""
import os
from pathlib import Path
import struct
import subprocess
import sys

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:      # `python3 -I` does not put the script's folder on the path
    sys.path.insert(0, str(HERE))
from fileinfo import clean_text, device_label, kind_of

MAX_ICON_BYTES = 2 * 1024 * 1024
THUMB_SIZE = 256
MAX_PIXELS = 4096 * 4096
MAX_FILES_LISTED = 512
ALLOWED_FORMATS = ("JPEG2000", "JPEG", "PNG")
MAGIC = b"ADRP"      # header of the raw-pixel file: magic, width, height (big endian), then RGBA bytes


def _size(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 1 << 50 else None


def describe(request):
    """A bounded, display-safe description of a parsed /Ask request. The icon bytes are returned separately."""
    sender = clean_text(request.get("SenderComputerName") or request.get("SenderPseudonym") or "Dispositivo sconosciuto", 120)
    model = clean_text(request.get("SenderModelName"), 64)
    files = []
    raw_files = request.get("Files") if isinstance(request.get("Files"), list) else []
    for entry in raw_files[:MAX_FILES_LISTED]:
        if not isinstance(entry, dict):
            continue
        raw_name = entry.get("FileName") or entry.get("FileBomPath") or ""
        name = clean_text(os.path.basename(str(raw_name).rstrip("/")) or str(raw_name), 255)
        uti = entry.get("FileType") if isinstance(entry.get("FileType"), str) else None
        is_dir = entry.get("FileIsDirectory") in (True, 1)
        files.append({"name": name or "?", "uti": clean_text(uti, 80) or None, "is_dir": is_dir,
                      "kind": kind_of(name, uti, is_dir), "size": _size(entry.get("FileSize"))})
    sizes = [f["size"] for f in files]
    items = []
    for item in request.get("Items", []) if isinstance(request.get("Items"), list) else []:
        if isinstance(item, bytes):
            item = item.decode("utf-8", "replace")
        elif isinstance(item, dict):
            item = item.get("URL") or item.get("url") or ""
        if isinstance(item, str) and item:
            items.append(clean_text(item, 2000))
    icon = request.get("FileIcon")
    return {
        "sender": sender, "model": model, "model_label": device_label(model),
        "files": files, "file_count": len(raw_files),
        "total_size": sum(sizes) if files and all(s is not None for s in sizes) else None,
        "items": items[:8],
        "has_icon": isinstance(icon, bytes) and 0 < len(icon) <= MAX_ICON_BYTES,
    }, (icon if isinstance(icon, bytes) and 0 < len(icon) <= MAX_ICON_BYTES else None)


# ------------------------------------------------------------------ the thumbnail decoder
def decode_thumbnail(data):
    """Decode image bytes to (width, height, RGBA bytes) no larger than THUMB_SIZE. Raises ValueError."""
    if len(data) > MAX_ICON_BYTES:
        raise ValueError("immagine troppo grande")
    sys.path.insert(0, str(HERE / "backend/python"))
    import io
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        picture = Image.open(io.BytesIO(data), formats=ALLOWED_FORMATS)
        if picture.width * picture.height > MAX_PIXELS:
            raise ValueError("immagine troppo grande")
        picture.draft("RGB", (THUMB_SIZE * 2, THUMB_SIZE * 2))
        picture.load()
        picture = picture.convert("RGBA")
        picture.thumbnail((THUMB_SIZE, THUMB_SIZE))
    except (OSError, SyntaxError, Image.DecompressionBombError, Image.UnidentifiedImageError, EOFError) as exc:
        raise ValueError("immagine non valida") from exc
    return picture.width, picture.height, picture.tobytes()


def _limit_resources():
    import resource
    for which, value in ((resource.RLIMIT_AS, 1024 * 1024 * 1024), (resource.RLIMIT_CPU, 5),
                         (resource.RLIMIT_FSIZE, 2 * 1024 * 1024), (resource.RLIMIT_NOFILE, 32),
                         (resource.RLIMIT_CORE, 0)):
        try:
            resource.setrlimit(which, (value, value))
        except (ValueError, OSError):
            pass


def _main(argv):
    if len(argv) != 4 or argv[1] != "thumb":
        return 2
    _limit_resources()
    os.umask(0o077)
    try:
        source = Path(argv[2])
        if source.stat().st_size > MAX_ICON_BYTES:
            return 3
        width, height, pixels = decode_thumbnail(source.read_bytes())
        Path(argv[3]).write_bytes(MAGIC + struct.pack(">II", width, height) + pixels)
    except (OSError, ValueError, MemoryError):
        return 3
    return 0


def make_thumbnail(icon, workdir, timeout=6):
    """Run the decoder in a limited subprocess. Returns {'file', 'width', 'height'} or None. Never raises."""
    if not icon:
        return None
    try:
        source, target = Path(workdir) / "icon.bin", Path(workdir) / "thumb.rgba"
        source.write_bytes(icon)
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        done = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "thumb", str(source), str(target)],
                              env=env, cwd="/", stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=timeout, check=False)
        source.unlink(missing_ok=True)
        if done.returncode != 0 or not target.is_file():
            return None
        width, height = read_header(target)
        return {"file": str(target), "width": width, "height": height}
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def read_header(path):
    with open(path, "rb") as handle:
        head = handle.read(12)
    if len(head) != 12 or head[:4] != MAGIC:
        raise ValueError("miniatura non valida")
    width, height = struct.unpack(">II", head[4:])
    if not (0 < width <= THUMB_SIZE and 0 < height <= THUMB_SIZE) or os.path.getsize(path) != 12 + width * height * 4:
        raise ValueError("miniatura non valida")
    return width, height


def read_pixels(path):
    """(width, height, RGBA bytes) from a file written by the decoder, or ValueError."""
    width, height = read_header(path)
    with open(path, "rb") as handle:
        handle.seek(12)
        return width, height, handle.read(width * height * 4)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
