# Pin the diagram: layout assertions in Swift

Illustrative, not compiled: no toolchain for this language was available when the examples were last checked.

The script can check that a diagram names the fields in order with plausible widths; only the compiler knows padding and real offsets. Put the numbers of the diagram in an assertion next to the type:

```swift
// Hdr in memory: [len:1B][kind:1B][pad:2B][crc:4B]; Swift inserts the pad.
struct Hdr {
    var len: UInt8
    var kind: UInt8
    var crc: UInt32
}

assert(MemoryLayout<Hdr>.size == 8, "Hdr layout changed: update the diagram")
assert(MemoryLayout<Hdr>.offset(of: \Hdr.crc) == 4)
```
