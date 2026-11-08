# Design comment in C++

The same frame codec, at the top of the public header:

```cpp
// frame.h -- length-prefixed frame codec for the replication stream.
//
// Every frame is a fixed 5-byte header followed by the payload, so a reader
// learns the payload size before touching it and never scans for a delimiter.
//
// Layout, little-endian: [len:4B][kind:1B][payload: len bytes]
//
// Invariant: len <= kMaxFrame is checked before allocating.
#pragma pack(push, 1)
struct FrameHeader {
    std::uint32_t len;
    std::uint8_t kind;
};
#pragma pack(pop)
static_assert(sizeof(FrameHeader) == 5, "diagram: 5-byte header");

// Return the payload length of the header at `p`, which holds at least 5
// bytes. Assumes a little-endian host: memcpy avoids the unaligned read.
inline std::uint32_t payload_len(const char *p) {
    std::uint32_t len;
    std::memcpy(&len, p, sizeof len);
    return len;
}
```
