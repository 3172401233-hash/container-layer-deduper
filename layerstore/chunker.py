"""FastCDC-style content-defined chunking.

Chunk boundaries depend only on the byte content of a file, never on
tar metadata (mtime, entry order, compression), so identical content
always produces identical chunks.
"""

MASK64 = (1 << 64) - 1


def _build_gear_table():
    # Deterministic 256-entry table generated with splitmix64 from a
    # fixed seed, so chunking is reproducible across runs and machines.
    table = []
    state = 0x9E3779B97F4A7C15
    for _ in range(256):
        state = (state + 0x9E3779B97F4A7C15) & MASK64
        z = state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        table.append(z ^ (z >> 31))
    return tuple(table)


GEAR = _build_gear_table()


class Chunker:
    """Splits bytes into content-defined chunks.

    Parameters
    ----------
    min_size: minimum chunk size in bytes (no boundary is emitted before
        this many bytes of a chunk have been seen).
    avg_size: target average chunk size; should be a power of two. The
        rolling-hash masks are derived from it.
    max_size: hard cap on chunk size.
    """

    def __init__(self, min_size=4096, avg_size=16384, max_size=65536):
        if not (0 < min_size <= avg_size <= max_size):
            raise ValueError("require 0 < min_size <= avg_size <= max_size")
        self.min_size = min_size
        self.avg_size = avg_size
        self.max_size = max_size
        bits = avg_size.bit_length() - 1
        # FastCDC: stricter mask before the avg point, looser mask after.
        self.mask_s = (1 << (bits + 1)) - 1
        self.mask_l = (1 << (bits - 1)) - 1

    def chunk(self, data):
        """Split *data* into a list of bytes chunks."""
        chunks = []
        n = len(data)
        start = 0
        while start < n:
            end = self._cut_point(data, start, n)
            chunks.append(data[start:end])
            start = end
        return chunks

    def _cut_point(self, data, start, n):
        remaining = n - start
        if remaining <= self.min_size:
            return n
        stop = start + min(remaining, self.max_size)
        mid = min(start + self.avg_size, stop)
        fp = 0
        i = start + self.min_size
        while i < stop:
            fp = ((fp << 1) + GEAR[data[i]]) & MASK64
            mask = self.mask_s if i < mid else self.mask_l
            if fp & mask == 0:
                return i + 1
            i += 1
        return stop
