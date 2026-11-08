# Pin the diagram: layout assertions in C++

The script can check that a diagram names the fields in order with plausible widths; only the compiler knows padding and real offsets. Put the numbers of the diagram in an assertion next to the type:

```cpp
// Wire header: [len:1B][kind:1B][crc:4B]
#pragma pack(push, 1)
struct Hdr {
    std::uint8_t len;
    std::uint8_t kind;
    std::uint32_t crc;
};
#pragma pack(pop)
static_assert(sizeof(Hdr) == 6, "diagram: 6-byte header");
static_assert(offsetof(Hdr, crc) == 2, "diagram: crc at byte 2");
```
