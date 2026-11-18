import gzip
import io
import random
import tarfile
import unittest

from layerstore import LayerStore, LayerNotFound, UploadNotFound


def make_tar(entries, mtime=0):
    """entries: list of (name, data, mode) tuples."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data, mode in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = mtime
            info.mode = mode
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def gz(data, level=6):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=level, mtime=0) as f:
        f.write(data)
    return buf.getvalue()


def blob(seed, size):
    rng = random.Random(seed)
    return bytes(rng.randrange(256) for _ in range(size))


def extract(tar_bytes):
    """Return {name: (type, mode, linkname, data)} for a tar blob."""
    out = {}
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tf:
        for m in tf:
            data = tf.extractfile(m).read() if m.isreg() else b""
            kind = (
                "dir" if m.isdir()
                else "symlink" if m.issym()
                else "hardlink" if m.islnk()
                else "file"
            )
            out[m.name] = (kind, m.mode & 0o7777, m.linkname, data)
    return out


def make_store():
    return LayerStore(min_size=64, avg_size=256, max_size=1024)


SHARED = blob(1, 5000)
APP_A = blob(2, 3000)
APP_B = blob(3, 3000)


class RepackDeterminismTest(unittest.TestCase):
    def test_entry_order_and_mtime_do_not_change_digest(self):
        store = make_store()
        files = [
            ("app/bin/run", SHARED, 0o755),
            ("etc/app.conf", APP_A, 0o644),
            ("app/lib/core.so", APP_B, 0o644),
        ]
        tar1 = make_tar(files, mtime=1000)
        tar2 = make_tar(list(reversed(files)), mtime=999999)

        d1 = store.push_layer(gz(tar1, 6))
        chunks_after_first = len(store.chunks)
        d2 = store.push_layer(gz(tar2, 9))

        self.assertEqual(d1, d2)
        self.assertEqual(len(store.chunks), chunks_after_first)

    def test_repacked_tar_shares_all_chunks(self):
        store = make_store()
        files = [("a.bin", SHARED, 0o644), ("b.bin", APP_A, 0o644)]
        d1 = store.push_layer(make_tar(files, mtime=1))
        d2 = store.push_layer(make_tar(files, mtime=2))
        self.assertEqual(d1, d2)
        self.assertEqual(store.stats()["layer_count"], 1)


class CompressionTest(unittest.TestCase):
    def test_compression_differences_do_not_change_digest(self):
        store = make_store()
        tar = make_tar([("x", SHARED, 0o644), ("y", APP_A, 0o600)], mtime=42)
        digests = {
            store.push_layer(tar),
            store.push_layer(gz(tar, 1)),
            store.push_layer(gz(tar, 9)),
        }
        self.assertEqual(len(digests), 1)


class DuplicateWhiteoutTest(unittest.TestCase):
    def test_duplicate_whiteout_entries_collapse(self):
        store = make_store()
        # Same whiteout path twice with different mtimes: last wins.
        tar_dup = make_tar(
            [(".wh.app", b"", 0o644), (".wh.app", b"", 0o644)],
            mtime=7,
        )
        tar_single = make_tar([(".wh.app", b"", 0o644)], mtime=7)
        self.assertEqual(store.push_layer(tar_dup), store.push_layer(tar_single))

    def test_duplicate_regular_file_last_wins(self):
        store = make_store()
        tar_dup = make_tar(
            [("f.txt", b"old", 0o644), ("f.txt", b"new", 0o644)],
            mtime=7,
        )
        tar_ref = make_tar([("f.txt", b"new", 0o644)], mtime=7)
        self.assertEqual(store.push_layer(tar_dup), store.push_layer(tar_ref))


class PartialUploadTest(unittest.TestCase):
    def test_chunked_upload_matches_direct_push(self):
        blob_data = gz(make_tar([("big.bin", SHARED, 0o644)], mtime=5))
        direct = make_store().push_layer(blob_data)

        store = make_store()
        upload_id = store.begin_upload()
        third = len(blob_data) // 3
        store.upload_part(upload_id, blob_data[:third])
        store.upload_part(upload_id, blob_data[third:2 * third])
        store.upload_part(upload_id, blob_data[2 * third:])
        self.assertEqual(store.complete_upload(upload_id), direct)

    def test_abort_upload(self):
        store = make_store()
        upload_id = store.begin_upload()
        store.upload_part(upload_id, b"partial")
        store.abort_upload(upload_id)
        with self.assertRaises(UploadNotFound):
            store.complete_upload(upload_id)

    def test_preuploaded_chunks_are_reused(self):
        store = make_store()
        content = b"tiny"  # smaller than min_size -> single chunk
        digest = store.put_chunk(content)
        self.assertTrue(store.has_chunk(digest))
        before = len(store.chunks)
        store.push_layer(make_tar([("tiny", content, 0o644)]))
        self.assertEqual(len(store.chunks), before)


class GCTest(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.layer1 = self.store.push_layer(
            make_tar([("shared", SHARED, 0o644), ("only-a", APP_A, 0o644)])
        )
        self.layer2 = self.store.push_layer(
            make_tar([("shared", SHARED, 0o644), ("only-b", APP_B, 0o644)])
        )
        self.m1 = self.store.put_manifest(
            {"schemaVersion": 2, "layers": [{"digest": self.layer1}]}
        )
        self.m2 = self.store.put_manifest(
            {"schemaVersion": 2, "layers": [{"digest": self.layer2}]}
        )
        self.store.tag("v1", self.m1)
        self.store.tag("v2", self.m2)

    def test_gc_reclaims_only_unreferenced_chunks(self):
        chunks_before = len(self.store.chunks)
        self.store.untag("v2")
        result = self.store.gc()

        self.assertEqual(result["manifests_removed"], 1)
        self.assertEqual(result["layers_removed"], 1)
        self.assertGreater(result["chunks_removed"], 0)
        self.assertLess(len(self.store.chunks), chunks_before)

        # Shared layer and its chunks survive; layer1 still pullable.
        pulled = extract(self.store.pull_layer(self.layer1))
        self.assertEqual(pulled["shared"][3], SHARED)
        self.assertEqual(pulled["only-a"][3], APP_A)

        # Layer2 is gone entirely.
        with self.assertRaises(LayerNotFound):
            self.store.pull_layer(self.layer2)

    def test_gc_without_untag_keeps_everything(self):
        chunks_before = len(self.store.chunks)
        result = self.store.gc()
        self.assertEqual(result["chunks_removed"], 0)
        self.assertEqual(len(self.store.chunks), chunks_before)

    def test_gc_all_tags_removed_reclaims_all(self):
        self.store.untag("v1")
        self.store.untag("v2")
        self.store.gc()
        self.assertEqual(len(self.store.chunks), 0)
        self.assertEqual(len(self.store.layers), 0)
        self.assertEqual(len(self.store.manifests), 0)


class RebuildTest(unittest.TestCase):
    def test_rebuild_restores_content_and_permissions(self):
        store = make_store()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            d = tarfile.TarInfo("app")
            d.type = tarfile.DIRTYPE
            d.mode = 0o750
            tf.addfile(d)
            for name, data, mode in [
                ("app/run", SHARED, 0o755),
                ("app/conf", APP_A, 0o600),
                ("app/readme", APP_B, 0o644),
            ]:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = mode
                tf.addfile(info, io.BytesIO(data))
            link = tarfile.TarInfo("app/current")
            link.type = tarfile.SYMTYPE
            link.linkname = "run"
            link.mode = 0o777
            tf.addfile(link)

        digest = store.push_layer(gz(buf.getvalue()))
        pulled = extract(store.pull_layer(digest, compress="gzip"))

        self.assertEqual(pulled["app"][0], "dir")
        self.assertEqual(pulled["app"][1], 0o750)
        self.assertEqual(pulled["app/run"], ("file", 0o755, "", SHARED))
        self.assertEqual(pulled["app/conf"], ("file", 0o600, "", APP_A))
        self.assertEqual(pulled["app/readme"], ("file", 0o644, "", APP_B))
        self.assertEqual(pulled["app/current"][0], "symlink")
        self.assertEqual(pulled["app/current"][2], "run")

    def test_rebuild_is_deterministic(self):
        store = make_store()
        digest = store.push_layer(make_tar([("f", SHARED, 0o644)]))
        self.assertEqual(store.pull_layer(digest), store.pull_layer(digest))
        self.assertEqual(
            store.pull_layer(digest, compress="gzip"),
            store.pull_layer(digest, compress="gzip"),
        )


class StatsTest(unittest.TestCase):
    def test_dedup_ratio_reflects_sharing(self):
        store = make_store()
        store.push_layer(make_tar([("shared", SHARED, 0o644)]))
        store.push_layer(make_tar([("shared", SHARED, 0o644)]))
        stats = store.stats()
        self.assertEqual(stats["logical_bytes"], 2 * len(SHARED))
        self.assertLess(stats["physical_bytes"], stats["logical_bytes"])
        self.assertGreater(stats["dedup_ratio"], 0.4)


if __name__ == "__main__":
    unittest.main()
