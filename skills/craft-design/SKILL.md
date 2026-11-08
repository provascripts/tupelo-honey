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
Name every structure with its fields, sizes, ownership and allocations before writing procedures: data dominates (Pike, rule 5). No speculative speed-ups: measure first (Pike, rules 1-2). For each structure write the access pattern: operation, frequency, typical N, max N. If typical N <= ~1000 and a measurement says it matters, read `references/encodings.md` (ten rules, Redis defaults, Teacher template).

## 2. Design it twice in <= 120 chat tokens
Compare 2-3 candidates on the axes that decide systems code, pick one, say why in one line, proceed:

| Candidate | Layout / allocations | Lookup | Special cases | Verdict |
|---|---|---|---|---|
| A: one node per byte, 256 child slots | 1 malloc per node, mostly empty slots | O(k) | 1 | reject: memory |
| B: packed node `[hdr][chars][pad][ptrs]` | 1 malloc per node, contiguous | O(k) | 2 (split, merge) | **pick** |

Stop for user approval only when the choice changes a public API, a storage or wire format, or cannot be undone (AGENTS.md §1).

## 3. Gates (hard stops)
- **Special cases.** Count the exceptions to the invariant and the cases of the interface; an exhaustive enumeration of algorithmic cases is not an exception (rax's five insertion cases pass). Aim for <= 2; above 3 (or an invariant longer than one sentence, or "except when" more than twice) STOP and simplify: drop the marginal feature and record it under "Known limitations", or pull the case down into the representation (a sentinel head node lets an empty tree and a full one share one pointer path).
- **Don't parse (djb).** Prefer length-prefixed, binary-safe framing (`[len][bytes]`, netstrings `5:hello,`) over delimiter-terminated text that needs a stateful parser.
- **Safe primitive over checks everywhere.** One `sds`/`stralloc`-style type that owns length and capacity removes buffer overflows from every caller.
- **Zero-cost compatibility.** Hand callers a pointer to the payload and keep the metadata right before it (`s - sizeof(hdr)`, flags at `s[-1]`), so the value still works with existing APIs.

**Signals, not gates.** More than 3 boolean parameters asks whether some are really separate functions; D_mod below 15 (`craft-metrics.py --report`) asks whether shallow entry points should merge. Neither number decides.

## 4. Write the header
One Design comment per module at its entry point (AGENTS.md §4): problem, structures, layout, invariants, concurrency, rejected alternatives, known limitations. Then a Teacher comment with an exact diagram before every struct whose layout matters: bracket strips, linear chains, indented trees, byte-offset tables, never multi-column box grids.

Write a physical layout as `[field:size]` cells in declaration order: `[len:1B][alloc:1B][flags:1B][buf: len bytes]` (`B` bytes, `b` bits, a bare number is bits for a bitfield and bytes otherwise; `header`, `hdr`, `pad`, `padding`, `payload` are synthetic, and a sized `header` must equal the leading fields it summarizes). The Stop gate checks names, order, gaps and fixed widths; the assertion craft-code puts next to the type lets the compiler check padding and offsets. Free-form teaching diagrams stay unchecked.

## Example: a prefix header behind the payload pointer (antirez/sds, `sds.h`, BSD-3-Clause, see LICENSE; struct verbatim)
```c
struct __attribute__ ((__packed__)) sdshdr8 {
    uint8_t len; /* used */
    uint8_t alloc; /* excluding the header and null terminator */
    unsigned char flags; /* 3 lsb of type, 5 unused bits */
    char buf[];
};
```
The Teacher comment such a header deserves, relative to the `sds` pointer `s`, which points at `buf`:
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

## Language references
In `references/`: `header-<lang>.md` (a Design comment in its home, for c, cpp, go, java, python, rust, typescript, zig; `header-c.md` has rax's trees and bit-packed node). **Read only the file for the language in use.**

## Translation table
| Concept | C / C++ / Zig | Rust / Go | Python / TypeScript / Java |
|---|---|---|---|
| Design comment home | top of the public header; Zig `//!` | `//!` in `lib.rs`/`mod.rs`; Go `doc.go` | module docstring or `__init__.py`; header of `index.ts`; `package-info.java` |
| Packed header + payload (`raxNode`, `sdshdr8`) | struct + bitfields + flexible array member; Zig `packed struct` / `extern struct` | `#[repr(C)]` struct, `enum` as tagged union; Go struct + `[]byte` with `encoding/binary` | `struct.Struct`, `ctypes`, `memoryview`, `@dataclass(slots=True)`, `TypedDict`; `DataView`/`Buffer`; `ByteBuffer`, `record` |
| Length-prefixed read | `uint32_t len` + `memcpy` | `u32::from_le_bytes`; `binary.LittleEndian.Uint32` | `struct.unpack_from("<I", buf)`; `view.getUint32(0, true)`; `ByteBuffer.order(LITTLE_ENDIAN).getInt()` |
| Sentinel instead of a special case | head node | sentinel node; `Option` niche | sentinel object |
