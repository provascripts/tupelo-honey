# Design comment in Python

The same frame codec, as the module docstring:

```python
"""Length-prefixed frame codec for the replication stream.

Every frame is a fixed 5-byte header followed by the payload, so a reader
learns the payload size before touching it and never scans for a delimiter.

Layout, little-endian: [len:4B][kind:1B][payload: len bytes]

Invariant: len <= MAX_FRAME is checked before allocating.
"""
import struct

HEADER = struct.Struct("<IB")
assert HEADER.size == 5, "diagram: 5-byte header"


def payload_len(buf: bytes) -> int:
    """Return the payload length of the frame at the start of `buf`.

    Raises struct.error if `buf` is shorter than the header.
    """
    return HEADER.unpack_from(buf)[0]
```
