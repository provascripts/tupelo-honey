# Compact encodings: antirez's method

Read this when the access-pattern table of craft-design shows a structure that usually stays small (typical N <= ~1000) and a measurement says its memory or locality matters. Without a measurement, rule 9 ends the discussion. The Python examples run in the plugin's tests; every Redis fact below was read in its `redis.conf` and `src/` at commit `9da5a27` (2026-11-07) and on redis.io, "Memory optimization".

## The ten rules
1. **Dual representation.** A contiguous small form (array, length-prefixed blob) and a pointer-based large form (hash table, tree), picked by a named threshold constant whose Why comment cites where the value came from. The conversion goes one way: growth crosses it once and shrinking never undoes it, as with Redis hashes and sets. If memory must come back, convert back only well below the limit, as Redis lists do at half of it (`listTypeTryConvertQuicklist`), so a workload sitting at the limit cannot flip on every operation.
2. **Upstream defaults are a starting point, never a measurement.** The table below is what Redis ships for its own value sizes. redis.io calls these limits "a CPU / memory tradeoff" and, for much larger limits, suggests benchmarks "to check the conversion time". A copied number says so in its Why comment, and gets replaced by your own measurement.
3. **No cascades: the ziplist to listpack lesson.** A ziplist entry stored the length of the *previous* entry in 1 byte (under 254) or 5: growing one entry could enlarge the next header, then the next, an O(N) ripple (`__ziplistCascadeUpdate`). A listpack entry stores *its own* length at its end (`backlen`), so a backward walk needs nothing from a neighbor and an edit touches one element.
4. **Varint plus delta for monotone sequences** (IDs, timestamps, offsets). Stream entries hold "the millisecond and sequence difference" from the key of their radix tree node, in listpack's variable-width integers, and a flag drops the field names when they equal the node's master entry.
5. **Variable-width headers.** A header as wide as the value it describes: sds uses 1, 3, 5, 9 or 17 bytes for strings under 2^5, 2^8, 2^16, 2^32 bytes and beyond; a listpack `backlen` takes 1 to 5.
6. **One-way width upgrades.** All integers of an intset share the width of the largest (16, 32 or 64 bits). A wider one re-encodes the whole set once (`intsetUpgradeAndAdd`); no removal narrows it, so the width is a single field and lookups stay a plain binary search.
7. **Presence bits instead of a stored sentinel.** `raxNode` has `isnull`: "Associated value is NULL (don't store it)". A bit costs less than a pointer, and no value of the domain is reserved to mean "absent".
8. **Lossy quantization only with the measured recall in the Why comment.** Vector Sets default to Q8; the README says BIN "is much faster and uses less memory, but has impacts on the recall quality". State the recall measured on your data, and on which queries, next to the choice.
9. **When not to.** No measurement means complexity without benefit: stop at the simplest structure. The gain depends on the language. In CPython a `list` of integers is a pointer plus a boxed object each, so `array` wins by a large factor (measured below); a Rust `Vec<u32>` or Go `[]uint32` is already contiguous, and a second representation buys little beyond its cost.
10. **A persisted threshold is a data format.** If the encoding or its threshold reaches disk or the wire, changing it changes the format: stop for approval (AGENTS.md §1), and keep a reader for the old form.

## Redis defaults (rule 2)
| Structure | Parameter | Default |
|---|---|---|
| Hash | `hash-max-listpack-entries` | 512 |
| Hash | `hash-max-listpack-value` | 64 |
| Sorted set | `zset-max-listpack-entries` | 128 |
| Sorted set | `zset-max-listpack-value` | 64 |
| Integer set | `set-max-intset-entries` | 512 |
| Small set | `set-max-listpack-entries` | 128 |
| Small set | `set-max-listpack-value` | 64 |
| List (quicklist) | `list-max-listpack-size` | -2 (8 KB per node) |
| Stream | `stream-node-max-bytes` | 4096 |
| Stream | `stream-node-max-entries` | 100 |
| HyperLogLog | `hll-sparse-max-bytes` | 3000 |
| embstr string | `OBJ_ENCODING_EMBSTR_SIZE_LIMIT` (compile time) | 44 bytes |

## Teacher template for a compact layout
A `[field:size]` strip, an offset table and the threshold with its source:
```text
Small form, one allocation:

  [count:4B][width:1B][elements: count*width bytes]

  0..3   count      little-endian
  4      width      2, 4 or 8, the width of the largest element
  5..    elements   ascending, binary searched

Threshold: SMALL_MAX = 512 entries, the default of set-max-intset-entries
(redis.conf), not measured here. Re-measure if the element size changes.
```

## Rule 1: dual representation, one-way conversion
```python
import array
import bisect

# Why 512: the default of set-max-intset-entries in redis.conf, a starting
# point not yet measured on this workload (rule 2).
SMALL_MAX = 512


class CompactIntSet:
    """A set of integers: a sorted array up to SMALL_MAX entries, a `set` past it.

    The conversion is one-way: removing elements never brings the array back.
    """

    def __init__(self):
        self.small = array.array("q")
        self.large = None

    def add(self, value):
        if self.large is not None:
            self.large.add(value)
            return
        i = bisect.bisect_left(self.small, value)
        if i == len(self.small) or self.small[i] != value:
            self.small.insert(i, value)
            if len(self.small) > SMALL_MAX:
                self.large, self.small = set(self.small), None

    def discard(self, value):
        if self.large is not None:
            self.large.discard(value)
            return
        i = bisect.bisect_left(self.small, value)
        if i < len(self.small) and self.small[i] == value:
            del self.small[i]

    def __contains__(self, value):
        if self.large is not None:
            return value in self.large
        i = bisect.bisect_left(self.small, value)
        return i < len(self.small) and self.small[i] == value

    def __len__(self):
        return len(self.small if self.large is None else self.large)
```

## Proof 4 of craft-verify: cross the threshold from both sides
```python
import random

for size in (SMALL_MAX - 1, SMALL_MAX, SMALL_MAX + 1):
    rng = random.Random(size)
    sut, oracle = CompactIntSet(), set()
    values = rng.sample(range(-10**9, 10**9), size)
    for value in values:
        sut.add(value)
        oracle.add(value)
        assert value in sut and len(sut) == len(oracle)
    assert (sut.large is not None) == (size > SMALL_MAX)
    # A differential run in whichever form the structure is now.
    for _ in range(2000):
        value = rng.choice(values)
        if rng.random() < 0.5:
            sut.add(value)
            oracle.add(value)
        else:
            sut.discard(value)
            oracle.discard(value)
        assert (value in sut) == (value in oracle) and len(sut) == len(oracle)
    # The conversion never goes back, even with the structure emptied.
    if size > SMALL_MAX:
        for value in list(oracle):
            sut.discard(value)
        assert sut.large is not None and len(sut) == 0
```

## Rule 3: backlen, a length read from its last byte backwards
```python
def encode_backlen(n):
    """Return `n` as 1 to 5 bytes readable backwards; the limits are lpEncodeBacklen's."""
    width = 1 if n <= 127 else 2 if n < 16383 else 3 if n < 2097151 else 4 if n < 268435455 else 5
    return bytes((n >> (7 * (width - 1 - i))) & 127 | (128 if i else 0) for i in range(width))


def decode_backlen(buf, last):
    """Return the length whose encoding ends at buf[last]; a clear high bit is its first byte."""
    value = shift = 0
    while True:
        value |= (buf[last] & 127) << shift
        if not buf[last] & 128:
            return value
        shift += 7
        last -= 1


for n in (0, 127, 128, 16382, 16383, 2097150, 2097151, 268435454, 268435455, 2**32 - 1):
    encoded = encode_backlen(n)
    assert decode_backlen(encoded, len(encoded) - 1) == n
assert [len(encode_backlen(n)) for n in (127, 128, 16382, 16383, 2097151, 268435455)] == [1, 2, 2, 3, 4, 5]
```

## Rule 4: varint plus delta
```python
def encode_deltas(values):
    """Encode an ascending sequence as little-endian base-128 deltas: small gaps cost one byte."""
    out, previous = bytearray(), 0
    for value in values:
        delta = value - previous
        previous = value
        while delta > 127:
            out.append(delta & 127 | 128)
            delta >>= 7
        out.append(delta)
    return bytes(out)


def decode_deltas(data):
    """Return the sequence that `encode_deltas` encoded in `data`."""
    values, current, delta, shift = [], 0, 0, 0
    for byte in data:
        delta |= (byte & 127) << shift
        if byte & 128:
            shift += 7
        else:
            current += delta
            values.append(current)
            delta = shift = 0
    return values


stamps = [1700000000000 + i * 3 for i in range(1000)]
assert decode_deltas(encode_deltas(stamps)) == stamps
# 6 bytes for the first value, then 1 per gap, against 8000 at a fixed width.
assert len(encode_deltas(stamps)) == 6 + 999
```

## Rule 5: a header as wide as its value
```python
def sds_header_size(length):
    """Return the header bytes antirez/sds uses for a string of `length` bytes."""
    for limit, size in ((1 << 5, 1), (1 << 8, 3), (1 << 16, 5), (1 << 32, 9)):
        if length < limit:
            return size
    return 17


assert [sds_header_size(n) for n in (31, 32, 255, 256, 65535, 65536, 2**32 - 1, 2**32)] == [1, 3, 3, 5, 5, 9, 9, 17]
```

## Rule 6: one-way width upgrade
```python
TYPECODE = {2: "h", 4: "i", 8: "q"}


def width_of(value):
    """Return the bytes an intset needs for `value`: 2, 4 or 8."""
    return 2 if -2**15 <= value < 2**15 else 4 if -2**31 <= value < 2**31 else 8


def add_int(items, value):
    """Return `items` with `value` appended, re-encoded once if `value` needs a wider type."""
    if width_of(value) > items.itemsize:
        items = array.array(TYPECODE[width_of(value)], items)
    items.append(value)
    return items


items = add_int(array.array("h"), 7)
assert items.itemsize == 2
items = add_int(items, 70000)
assert items.itemsize == 4 and list(items) == [7, 70000]
items.remove(70000)
assert items.itemsize == 4
```

## Rule 7: presence bits
```python
def get_slot(bits, values, slot):
    """Return the value of `slot`, or None; `values` holds only the slots whose bit is set."""
    if not bits >> slot & 1:
        return None
    return values[bin(bits & ((1 << slot) - 1)).count("1")]


assert [get_slot(0b101, [10, 30], slot) for slot in range(3)] == [10, None, 30]
```

## Rule 9: measure before you encode
```python
import sys

numbers = list(range(1000000, 1001000))
as_list = sys.getsizeof(numbers) + sum(sys.getsizeof(n) for n in numbers)
as_array = sys.getsizeof(array.array("q", numbers))
assert as_array * 3 < as_list
```
