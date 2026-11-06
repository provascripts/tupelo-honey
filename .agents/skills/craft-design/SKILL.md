---
name: craft-design
description: >-
  Use before writing a new module, data structure, public header or API, binary
  or wire format, or when restructuring an architecture. Freezes the data layout
  first (Pike: data dominates), compares 2-3 designs in a <= 120-token matrix
  (Ousterhout: design it twice), gates on special cases (antirez/Gabriel design
  sacrifices), prefers length-prefixed layouts (djb: don't parse), and writes the
  canonical Design + Teacher comment in the style of rax.h and sds.h.
license: MIT
---

# craft-design: data first, then the header

Run these steps in order, before any implementation code.

## 1. Freeze the data
Name every structure with its fields, sizes, ownership and allocations before writing procedures. "Data dominates. If you've chosen the right data structures and organized things well, the algorithms will almost always be self-evident" (Pike, rule 5). No speculative speed-ups: measure first (Pike, rules 1-2).

## 2. Design it twice in <= 120 chat tokens
Compare 2-3 candidates on the axes that decide systems code, pick one, say why in one line, proceed:

| Candidate | Layout / allocations | Lookup | Special cases | Verdict |
|---|---|---|---|---|
| A: one node per byte, 256 child slots | 1 malloc per node, mostly empty slots | O(k) | 1 | reject: memory |
| B: packed node `[hdr][chars][pad][ptrs]` | 1 malloc per node, contiguous | O(k) | 2 (split, merge) | **pick** |

Stop for user approval only when the choice changes a public API, a storage or wire format, or cannot be undone (AGENTS.md §1).

## 3. Gates (hard stops)
- **Special cases.** Count the cases needed to explain the interface. Aim for <= 2; above 3 (or an invariant longer than one sentence, or "except when" more than twice) STOP and simplify: drop the marginal feature and record it under "Known limitations", or pull the case down into the representation (a sentinel head node lets an empty tree and a full one share one pointer path).
- **Don't parse (djb).** Prefer length-prefixed, binary-safe framing (`[len][bytes]`, netstrings `5:hello,`) over delimiter-terminated text that needs a stateful parser: fewer lines, one bug class gone.
- **Safe primitive over checks everywhere.** One `sds`/`stralloc`-style type that owns length and capacity removes buffer overflows from every caller; a guard per call site does not.
- **Narrow interface.** <= 3 boolean parameters in any public signature; D_mod = implementation LOC / public functions, with < 15 as the signal to look for shallow entry points.
- **Zero-cost compatibility.** Hand callers a pointer to the payload and keep the metadata right before it (`s - sizeof(hdr)`, flags at `s[-1]`), so the value still works with existing APIs (`printf("%s", s)`).

## 4. Write the header
One Design comment per module at its entry point (AGENTS.md §4): problem, structures, layout, invariants, concurrency, rejected alternatives, known limitations. Then a Teacher comment with an exact diagram before every struct whose layout matters. Shapes: bracket strips, linear chains, indented trees, byte-offset tables. Never multi-column box grids.

For a physical layout, write the strip as `[field:size]` cells in declaration order: `[len:1B][alloc:1B][flags:1B][buf: len bytes]` (`B` bytes, `b` bits, a bare number is bits for a bitfield and bytes otherwise; `header`, `hdr`, `pad`, `padding`, `payload` are synthetic and need no field, and a sized `header` must equal the leading fields it summarizes, like raxNode's `[header:32b]` over `iskey:1 isnull:1 iscompr:1 size:29`). The Stop gate then checks names, order, gaps and fixed widths (a struct with members it cannot read, like a macro field or an `#if`, only for order and widths), and the assertion that craft-code puts next to the type lets the compiler check padding and offsets. Free-form teaching diagrams (trees, chains, examples) stay free-form and unchecked.

## Canonical examples

### A. A Design comment that teaches with trees (antirez/rax, `rax.h`, BSD-3-Clause, see LICENSE; verbatim, trimmed with `[...]`)
```c
/* Representation of a radix tree as implemented in this file, that contains
 * the strings "foo", "foobar" and "footer" after the insertion of each
 * word. When the node represents a key inside the radix tree, we write it
 * between [], otherwise it is written between ().
 *
 * This is the vanilla representation:
 *
 *              (f) ""
 *                \
 *                (o) "f"
 *                  \
 *                  (o) "fo"
 *                    \
 *                  [t   b] "foo"
 *                  /     \
 *         "foot" (e)     (a) "foob"
 *                /         \
 *      "foote" (r)         (r) "fooba"
 *              /             \
 *    "footer" []             [] "foobar"
 *
 * However, this implementation implements a very common optimization where
 * successive nodes having a single child are "compressed" into the node
 * itself as a string of characters, each representing a next-level child,
 * and only the link to the node representing the last character node is
 * provided inside the representation. So the above representation is turned
 * into:
 *
 *                  ["foo"] ""
 *                     |
 *                  [t   b] "foo"
 *                  /     \
 *        "foot" ("er")    ("ar") "foob"
 *                 /          \
 *       "footer" []          [] "foobar"
 *
 * However this optimization makes the implementation a bit more complex.
 * [...] */
```

### B. A bit-packed node with its layout spelled out (`rax.h`, verbatim, trimmed)
```c
typedef struct raxNode {
    uint32_t iskey:1;     /* Does this node contain a key? */
    uint32_t isnull:1;    /* Associated value is NULL (don't store it). */
    uint32_t iscompr:1;   /* Node is compressed. */
    uint32_t size:29;     /* Number of children, or compressed string len. */
    /* Data layout is as follows:
     *
     * If node is not compressed we have 'size' bytes, one for each children
     * character, and 'size' raxNode pointers, point to each child node.
     * [...]
     * [header iscompr=0][abc][a-ptr][b-ptr][c-ptr](value-ptr?)
     * [...]
     * [header iscompr=1][xyz][z-ptr](value-ptr?)
     * [...] */
    unsigned char data[];
} raxNode;
```
`rax.c` later inserts alignment padding between the characters and the pointers (`raxPadding()`): when you copy this idea, put the padding in your own diagram, `[hdr:32b][chars][pad][ptrs](value-ptr?)`. Diagrams must match the code.

### C. A prefix header behind the payload pointer (antirez/sds, `sds.h`, BSD-3-Clause, see LICENSE)
Verbatim:
```c
struct __attribute__ ((__packed__)) sdshdr8 {
    uint8_t len; /* used */
    uint8_t alloc; /* excluding the header and null terminator */
    unsigned char flags; /* 3 lsb of type, 5 unused bits */
    char buf[];
};
```
The Teacher comment such a header deserves, written as a byte-offset table relative to the `sds` pointer `s`, which points at `buf`:
```c
/* sdshdr8 layout, one allocation, header right before the string:
 *
 *   s-3       len    uint8   bytes used, excluding the terminator
 *   s-2       alloc  uint8   capacity, excluding header and terminator
 *   s-1       flags  uint8   low 3 bits = SDS_TYPE_8, read first to pick the header
 *   s..s+len  buf    the bytes, binary safe
 *   s+len     '\0'   so s is still a valid C string for printf() and libc
 */
```

### D. Polyglot: a length-prefixed codec header (Rust `//!` at the top of `lib.rs`)
```rust
//! Length-prefixed frame codec for the replication stream.
//!
//! Every frame is a fixed 5-byte header followed by the payload, so a reader
//! learns the payload size before touching it and never scans for a
//! delimiter. Rejected: newline-delimited JSON, which needs escaping, a
//! stateful parser and an unbounded read to find where a frame ends.
//!
//! Layout, little-endian:
//!   0..4   len      u32, payload bytes, header excluded
//!   4      kind     u8, 1 = data, 2 = ack, anything else is rejected
//!   5..n   payload  len bytes
//!
//! Invariant: `len <= MAX_FRAME` is checked before allocating, so a corrupt
//! or hostile header can never make us reserve 4 GiB.
//!
//! Known limitations: no internal buffering. A partial header returns
//! `Error::Incomplete` and the caller reads more bytes and retries.
```

## Translation table
| Concept | C / C++ / Zig | Rust / Go | Python / TypeScript / Java |
|---|---|---|---|
| Design comment home | top of the public header; Zig `//!` | `//!` in `lib.rs`/`mod.rs`; Go `doc.go` | module docstring or `__init__.py`; header of `index.ts`; `package-info.java` |
| Packed header + payload (`raxNode`, `sdshdr8`) | struct + bitfields + flexible array member; Zig `packed struct` / `extern struct` | `#[repr(C)]` struct, `enum` as tagged union; Go struct + `[]byte` with `encoding/binary` | `struct.Struct`, `ctypes`, `memoryview`, `@dataclass(slots=True)`, `TypedDict`; `DataView`/`Buffer`; `ByteBuffer`, `record` |
| Length-prefixed read | `uint32_t len` + `memcpy` | `u32::from_le_bytes`; `binary.LittleEndian.Uint32` | `struct.unpack_from("<I", buf)`; `view.getUint32(0, true)`; `ByteBuffer.order(LITTLE_ENDIAN).getInt()` |
| Sentinel instead of a special case | head node | sentinel node; `Option` niche | sentinel object |
