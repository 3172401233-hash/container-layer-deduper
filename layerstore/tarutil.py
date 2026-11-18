"""Tar layer normalization and deterministic rebuilding.

Normalization rules (so logically identical layers chunk identically):

* entries are keyed by normalized path and sorted by path, so tar entry
  order does not matter;
* mtime/uid/gid/uname/gname are discarded;
* a path appearing multiple times (e.g. duplicate whiteout ``.wh.*``
  files) collapses to its last occurrence;
* only regular files, directories, symlinks and hardlinks are kept.
"""

import io
import tarfile
from dataclasses import dataclass


@dataclass
class RawEntry:
    name: str
    type: str  # "file" | "dir" | "symlink" | "hardlink"
    mode: int
    linkname: str
    data: bytes


def _norm_path(name):
    parts = []
    for part in name.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def normalize_layer(blob):
    """Extract a compressed or plain tar layer into sorted RawEntry items."""
    entries = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tf:
        for member in tf:
            name = _norm_path(member.name)
            if not name:
                continue
            mode = member.mode & 0o7777
            if member.isdir():
                entry = RawEntry(name, "dir", mode, "", b"")
            elif member.issym():
                entry = RawEntry(name, "symlink", mode, member.linkname, b"")
            elif member.islnk():
                entry = RawEntry(name, "hardlink", mode, member.linkname, b"")
            elif member.isreg():
                data = tf.extractfile(member).read()
                entry = RawEntry(name, "file", mode, "", data)
            else:
                continue  # skip devices, fifos, etc.
            entries[name] = entry  # last occurrence wins
    return [entries[name] for name in sorted(entries)]


def build_layer(raw_entries):
    """Rebuild a deterministic uncompressed tar from sorted RawEntry items."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for entry in raw_entries:
            info = tarfile.TarInfo(entry.name)
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mode = entry.mode
            if entry.type == "dir":
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            elif entry.type == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = entry.linkname
                tf.addfile(info)
            elif entry.type == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = entry.linkname
                tf.addfile(info)
            else:
                info.size = len(entry.data)
                tf.addfile(info, io.BytesIO(entry.data))
    return buf.getvalue()
