# Pin the diagram: layout assertions in Rust

The script can check that a diagram names the fields in order with plausible widths; only the compiler knows padding and real offsets. Put the numbers of the diagram in an assertion next to the type:

```rust
/// [len:1B][kind:1B][crc:4B]
#[repr(C, packed)]
struct Hdr { len: u8, kind: u8, crc: u32 }
const _: () = assert!(std::mem::size_of::<Hdr>() == 6);
const _: () = assert!(std::mem::offset_of!(Hdr, crc) == 2);
```
