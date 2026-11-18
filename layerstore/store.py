"""Content-addressed layer store with chunk dedup, tags and GC."""

import gzip
import hashlib
import io
import json
import uuid

from .chunker import Chunker
from .tarutil import RawEntry, build_layer, normalize_layer


class LayerNotFound(KeyError):
    pass


class ManifestNotFound(KeyError):
    pass


class UploadNotFound(KeyError):
    pass


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _digest(data):
    return hashlib.sha256(data).hexdigest()


class LayerStore:
    def __init__(self, min_size=4096, avg_size=16384, max_size=65536):
        self.chunker = Chunker(min_size, avg_size, max_size)
        self.chunks = {}      # chunk digest -> bytes
        self.layers = {}      # layer digest -> {"entries": [...]}
        self.manifests = {}   # manifest digest -> canonical json bytes
        self.tags = {}        # tag -> manifest digest
        self.logical_bytes = 0  # total uncompressed file bytes ever pushed
        self._uploads = {}    # upload id -> list of parts

    # ---- layers -------------------------------------------------------

    def push_layer(self, blob):
        """Ingest a (possibly compressed) tar layer, return its digest."""
        entries = []
        for raw in normalize_layer(blob):
            entry = {
                "name": raw.name,
                "type": raw.type,
                "mode": raw.mode,
                "linkname": raw.linkname,
            }
            if raw.type == "file":
                chunk_digests = []
                for piece in self.chunker.chunk(raw.data):
                    digest = _digest(piece)
                    if digest not in self.chunks:
                        self.chunks[digest] = piece
                    chunk_digests.append(digest)
                entry["size"] = len(raw.data)
                entry["sha256"] = _digest(raw.data)
                entry["chunks"] = chunk_digests
                self.logical_bytes += len(raw.data)
            entries.append(entry)
        digest = _digest(_canonical(entries))
        self.layers[digest] = {"entries": entries}
        return digest

    def pull_layer(self, digest, compress="none"):
        """Rebuild a layer tar, restoring file contents and permissions.

        *compress* may be "none" or "gzip" (deterministic, mtime=0).
        """
        if digest not in self.layers:
            raise LayerNotFound(digest)
        raw_entries = []
        for entry in self.layers[digest]["entries"]:
            data = b""
            if entry["type"] == "file":
                data = b"".join(self.chunks[c] for c in entry["chunks"])
            raw_entries.append(
                RawEntry(
                    entry["name"],
                    entry["type"],
                    entry["mode"],
                    entry["linkname"],
                    data,
                )
            )
        tar_bytes = build_layer(raw_entries)
        if compress == "gzip":
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
                gz.write(tar_bytes)
            return buf.getvalue()
        return tar_bytes

    # ---- manifests and tags -------------------------------------------

    def put_manifest(self, manifest):
        """Store a manifest dict (must contain a "layers" list of
        {"digest": ...}); returns the manifest digest."""
        blob = _canonical(manifest)
        digest = _digest(blob)
        self.manifests[digest] = blob
        return digest

    def get_manifest(self, digest):
        if digest not in self.manifests:
            raise ManifestNotFound(digest)
        return json.loads(self.manifests[digest])

    def tag(self, name, manifest_digest):
        if manifest_digest not in self.manifests:
            raise ManifestNotFound(manifest_digest)
        self.tags[name] = manifest_digest

    def untag(self, name):
        self.tags.pop(name, None)

    def resolve(self, name):
        return self.tags.get(name)

    # ---- partial / resumable uploads -----------------------------------

    def begin_upload(self):
        upload_id = uuid.uuid4().hex
        self._uploads[upload_id] = []
        return upload_id

    def upload_part(self, upload_id, data):
        if upload_id not in self._uploads:
            raise UploadNotFound(upload_id)
        self._uploads[upload_id].append(bytes(data))

    def complete_upload(self, upload_id):
        if upload_id not in self._uploads:
            raise UploadNotFound(upload_id)
        parts = self._uploads.pop(upload_id)
        return self.push_layer(b"".join(parts))

    def abort_upload(self, upload_id):
        self._uploads.pop(upload_id, None)

    def has_chunk(self, digest):
        return digest in self.chunks

    def put_chunk(self, data):
        """Pre-upload a chunk (e.g. resume); returns its digest."""
        digest = _digest(bytes(data))
        if digest not in self.chunks:
            self.chunks[digest] = bytes(data)
        return digest

    # ---- garbage collection --------------------------------------------

    def gc(self):
        """Mark-and-sweep. Roots are tags: manifests not referenced by any
        tag are removed, then layers not referenced by any remaining
        manifest, then chunks not referenced by any remaining layer.
        Returns reclaim statistics."""
        live_manifests = set(self.tags.values())
        removed_manifests = [d for d in self.manifests if d not in live_manifests]
        for digest in removed_manifests:
            del self.manifests[digest]

        live_layers = set()
        for digest in live_manifests:
            manifest = json.loads(self.manifests[digest])
            for layer in manifest.get("layers", []):
                live_layers.add(layer["digest"])
        removed_layers = [d for d in self.layers if d not in live_layers]
        for digest in removed_layers:
            del self.layers[digest]

        referenced = set()
        for digest in live_layers:
            for entry in self.layers[digest]["entries"]:
                referenced.update(entry.get("chunks", []))
        chunks_removed = 0
        bytes_reclaimed = 0
        for digest in list(self.chunks):
            if digest not in referenced:
                bytes_reclaimed += len(self.chunks.pop(digest))
                chunks_removed += 1

        return {
            "manifests_removed": len(removed_manifests),
            "layers_removed": len(removed_layers),
            "chunks_removed": chunks_removed,
            "bytes_reclaimed": bytes_reclaimed,
        }

    # ---- statistics ------------------------------------------------------

    def stats(self):
        physical = sum(len(c) for c in self.chunks.values())
        logical = self.logical_bytes
        ratio = 1.0 - physical / logical if logical else 0.0
        return {
            "logical_bytes": logical,
            "physical_bytes": physical,
            "chunk_count": len(self.chunks),
            "layer_count": len(self.layers),
            "dedup_ratio": ratio,
        }
