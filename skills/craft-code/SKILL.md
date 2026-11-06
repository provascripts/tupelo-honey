---
name: craft-code
description: >-
  Use when writing or substantially changing implementation code in any
  language. Enforces deep cohesive functions over single-caller helpers, Guide
  comments every 15-25 LOC, Why and Checklist comments, one centralized cleanup
  path for >= 2 fallible resources, allocate-before-mutate, layout diagrams
  pinned by compile-time size/offset assertions, and a *Show/*Dump
  introspection primitive, with real raxGenericInsert and raxShow excerpts as
  models.
license: MIT
---

# craft-code: cohesive implementation, readable top to bottom

## Rules (LOC = non-blank, non-comment lines)
1. **Cohesive deep functions.** Keep an algorithm that shares local state (tree insertion, a state-machine step, packet decoding) in one function instead of 8 helpers passing 7 variables by reference. A helper with 1 caller and < 30 LOC is inlined under a Guide comment unless a `Why:` comment names its benefit: semantic boundary, resource ownership, removed duplication, or test seam. `craft-metrics.py --gate` reports violations at Stop.
2. **Guide rhythm.** Functions >= 40 LOC (or with >= 3 phases): one Guide comment every 15-25 LOC, a short line stating the goal of the next block. >= 80 LOC or multi-branch: a numbered step map (`1.`, `2.`, `3a.`, `3b.`) in the Teacher or Function comment, the same numbers as headers above the matching blocks.
3. **Why comments** wherever the obvious rewrite would break something: ordering, timeouts, protocol quirks, regressions. Every bug fix gets one ("moved before X due to bug Y; do not reorder").
4. **Checklist comments** at both ends of a coupling the compiler cannot enforce (after trying a single table, an exhaustive `switch`/`match`, `static_assert`, or a failing test).
5. **One cleanup path** for >= 2 fallible acquisitions: handles start at NULL/None, every failure jumps to one teardown block. Never duplicate multi-line teardown across branches (translations below).
6. **Allocate before you mutate.** Acquire everything a structural change needs first; if any acquisition fails, release and return while the structure is still untouched (rax: "OOM? Abort now that the tree is untouched.").
7. **Teacher diagram before pointer math**, bitfields, packed or flexible-array layouts (craft-design for the format), a **Function comment** above every public function, a **Design comment** (or a pointer to it) at the top of every module. The Stop gate reports what is missing as NO-TEACHER, NO-DOC and NO-DESIGN.
8. **Pin every physical layout.** Draw it with `[field:size]` cells in declaration order (the edit hook checks names, order and fixed widths: DIAGRAM-FIELD, -ORDER, -SIZE) and assert its size or offsets where the compiler or the test suite runs (examples below; the Stop gate reports NO-LAYOUT-ASSERT). Logical diagrams (trees, `->` chains, state machines) need no assertion. Never assert `int`, `long`, `size_t` or pointer widths as constants: they change between LP64 and LLP64.
9. **Say something.** A comment made only of generic words ("Process the data", "Handle edge cases") is reported as VAGUE: name the domain fact, the limit or the reason.
10. **Introspection primitive.** Every non-trivial data structure ships a dumper (`raxShow` style) that prints its topology compactly, so humans, tests and agents can check invariants at a glance.

## Pin the diagram: layout assertions per language
The script can check that a diagram names the fields in order with plausible widths; only the compiler knows padding and real offsets. Put the numbers of the diagram in an assertion next to the type:
```c
/* sdshdr8 layout, one allocation, header right before the string:
 *   [len:1B][alloc:1B][flags:1B][buf: len bytes]
 */
struct __attribute__ ((__packed__)) sdshdr8 {
    uint8_t len;
    uint8_t alloc;
    unsigned char flags;
    char buf[];
};
_Static_assert(offsetof(struct sdshdr8, alloc) == 1, "diagram: alloc at byte 1");
_Static_assert(offsetof(struct sdshdr8, buf) == 3, "diagram: 3-byte header");
```
```rust
/// [len:1B][kind:1B][crc:4B]
#[repr(C, packed)]
struct Hdr { len: u8, kind: u8, crc: u32 }
const _: () = assert!(std::mem::size_of::<Hdr>() == 6);
const _: () = assert!(std::mem::offset_of!(Hdr, crc) == 2);
```
```zig
// [iskey:1b][size:31b]
const Node = packed struct(u32) { iskey: u1, size: u31 };
comptime {
    std.debug.assert(@bitSizeOf(Node) == 32);
    std.debug.assert(@bitOffsetOf(Node, "size") == 1);
}
```
```go
// Hdr in memory: [Len:1B][Flags:1B][pad:2B][Crc:4B]; the wire format has no pad.
type Hdr struct {
	Len   uint8
	Flags uint8
	Crc   uint32
}

// In hdr_test.go: the compiler inserts the pad, so the test pins it.
func TestHdrLayout(t *testing.T) {
	if unsafe.Sizeof(Hdr{}) != 8 || unsafe.Offsetof(Hdr{}.Crc) != 4 {
		t.Fatal("Hdr layout changed: update the diagram and the encoder")
	}
}
```
```python
class Hdr(ctypes.LittleEndianStructure):
    """Wire header: [len:1B][flags:3][rest:5]"""
    _pack_ = 1
    _fields_ = [("len", ctypes.c_uint8), ("flags", ctypes.c_uint8, 3), ("rest", ctypes.c_uint8, 5)]


assert ctypes.sizeof(Hdr) == 2, "Hdr layout changed: update the diagram"
```
C++ uses `static_assert(offsetof(...) == N)`, C# `Debug.Assert(Marshal.SizeOf<Hdr>() == 6)` in a test, Swift `assert(MemoryLayout<Hdr>.size == 6)`.

## Canonical examples (antirez/rax, `rax.c`, BSD-2-Clause; verbatim, trimmed with `[...]`)

### A. Numbered Teacher comment and its Guide anchors (`raxGenericInsert`)
The Teacher comment above the split code walks five concrete cases with diagrams, then states the algorithm as numbered steps:
```c
     * 1) Inserting "ANNIENTARE"
     *
     *               |B| -> "ALE" -> "SCO" -> []
     *     "ANNI" -> |-|
     *               |E| -> (... continue algo ...) "NTARE" -> []
     * [...]
     * ============================= ALGO 1 =============================
     * [...]
     * 1. Save the current compressed node $NEXT pointer (the pointer to the
     *    child element, that is always present in compressed nodes).
     *
     * 2. Create "split node" having as child the non common letter
     *    at the compressed node. The other non common letter (at the key)
     *    will be added later as we continue the normal insertion algorithm
     *    at step "6".
     *
     * 3a. IF $SPLITPOS == 0:
     *     Replace the old node with the split node, by copying the auxiliary
     *     data if any. Fix parent's reference. Free old node eventually
     *     (we still need its data for the next steps of the algorithm).
     *
     * 3b. IF $SPLITPOS != 0:
     *     Trim the compressed node (reallocating it as well) in order to
     *     contain $splitpos characters. [...]
```
The code then carries the same numbers, so a reader can jump between plan and implementation:
```c
        /* 1: Save next pointer. */
        raxNode **childfield = raxNodeLastChildPtr(h);
        raxNode *next;
        memcpy(&next,childfield,sizeof(next));
        [...]
        /* 2: Create the split node. Also allocate the other nodes we'll need
         *    ASAP, so that it will be simpler to handle OOM. */
        raxNode *splitnode = raxNewNode(1, split_node_is_key);
        raxNode *trimmed = NULL;
        raxNode *postfix = NULL;
        [...]
        /* OOM? Abort now that the tree is untouched. */
        if (splitnode == NULL ||
            (trimmedlen && trimmed == NULL) ||
            (postfixlen && postfix == NULL))
        {
            rax_free(splitnode);
            rax_free(trimmed);
            rax_free(postfix);
            errno = ENOMEM;
            return 0;
        }
        splitnode->data[0] = h->data[j];

        if (j == 0) {
            /* 3a: Replace the old node with the split node. */
            [...]
        } else {
            /* 3b: Trim the compressed node. */
            [...]
        }
```
Later steps that can fail after the tree was modified share one exit, whose comment says how the invariant is restored:
```c
oom:
    /* This code path handles out of memory after part of the sub-tree was
     * already modified. Set the node as a key, and then remove it. However we
     * do that only if the node is a terminal node, otherwise if the OOM
     * happened reallocating a node in the middle, we don't need to free
     * anything. */
```

### B. Introspection primitive (`raxShow`)
```c
/* This function is mostly used for debugging and learning purposes.
 * It shows an ASCII representation of a tree on standard output, outling
 * all the nodes and the contained keys.
 *
 * The representation is as follow:
 *
 *  "foobar" (compressed node)
 *  [abc] (normal node with three children)
 *  [abc]=0x12345678 (node is a key, pointing to value 0x12345678)
 *  [] (a normal empty node)
 * [...] */
void raxShow(rax *rax) {
    raxRecursiveShow(0,0,rax->head);
    putchar('\n');
}
```

## One cleanup path, per language
C, `goto` to a single teardown:
```c
/* Read the whole file at 'path' into a new NUL-terminated buffer and store
 * its size in '*len'. Returns NULL on any failure; on success the caller
 * owns the buffer and frees it. */
char *load_file(const char *path, size_t *len) {
    char *buf = NULL;
    FILE *fp = fopen(path, "rb");
    if (fp == NULL) goto err;
    if (fseek(fp, 0, SEEK_END) == -1) goto err;
    long size = ftell(fp);
    if (size < 0 || fseek(fp, 0, SEEK_SET) == -1) goto err;
    if ((buf = malloc((size_t)size + 1)) == NULL) goto err;
    if (fread(buf, 1, (size_t)size, fp) != (size_t)size) goto err;
    buf[size] = '\0';
    fclose(fp);
    *len = (size_t)size;
    return buf;

err:
    /* fp and buf start as NULL, so this is safe whichever step failed. */
    free(buf);
    if (fp) fclose(fp);
    return NULL;
}
```
Go, named result plus one deferred closure:
```go
// CopyFile copies src to dst. Both files are always closed; on failure dst
// is removed, so a partial copy never survives.
func CopyFile(dst, src string) (err error) {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	defer func() {
		if cerr := out.Close(); err == nil {
			err = cerr
		}
		if err != nil {
			os.Remove(dst)
		}
	}()
	_, err = io.Copy(out, in)
	return err
}
```
Rust, `?` plus `Drop`:
```rust
/// Copy `src` to `dst`, returning the bytes written. Both files close when
/// they go out of scope; on failure the partial `dst` is removed.
fn copy_file(src: &Path, dst: &Path) -> io::Result<u64> {
    let mut input = File::open(src)?;
    let mut output = File::create(dst)?;
    let result = io::copy(&mut input, &mut output);
    // Why: close before removing, Windows cannot delete an open file.
    drop(output);
    if result.is_err() {
        let _ = fs::remove_file(dst);
    }
    result
}
```
Zig, `defer` plus `errdefer`:
```zig
/// Read the whole file at `path`. The caller owns the returned slice and
/// frees it with `allocator.free`.
fn loadFile(allocator: std.mem.Allocator, path: []const u8) ![]u8 {
    const file = try std.fs.cwd().openFile(path, .{});
    defer file.close();
    const buf = try allocator.alloc(u8, @intCast(try file.getEndPos()));
    errdefer allocator.free(buf);
    _ = try file.readAll(buf);
    return buf;
}
```
Python, one `ExitStack`:
```python
def merge(paths, out_path):
    """Concatenate the files in `paths` into `out_path`; every opened file is closed on all paths."""
    with contextlib.ExitStack() as stack:
        sources = [stack.enter_context(open(p, "rb")) for p in paths]
        out = stack.enter_context(open(out_path, "wb"))
        for src in sources:
            shutil.copyfileobj(src, out)
```
TypeScript, handles start `undefined`, one `finally` (the shape of C's `goto err`):
```ts
/** Copy src to dst; both handles are closed on every path, errors included. */
export async function copyFile(src: string, dst: string): Promise<void> {
  let input: FileHandle | undefined;
  let output: FileHandle | undefined;
  try {
    input = await open(src, "r");
    output = await open(dst, "w");
    await output.writeFile(await input.readFile());
  } finally {
    await output?.close();
    await input?.close();
  }
}
```
Java, try-with-resources:
```java
/** Copies src to dst and returns the bytes copied; both streams close on every path. */
static long copy(Path src, Path dst) throws IOException {
    try (InputStream in = Files.newInputStream(src);
         OutputStream out = Files.newOutputStream(dst)) {
        return in.transferTo(out);
    }
}
```

## Translation table
| Concept | C / C++ / Zig | Rust / Go | Python / TypeScript / Java |
|---|---|---|---|
| Single cleanup path | `goto err`/`goto oom`; C++ RAII; Zig `defer` + `errdefer` | `?` + `Drop`; `defer` + named `err` | `with`/`ExitStack`; one `try/finally`; try-with-resources |
| Allocate before mutate | allocate all nodes, check, then link | build the new value, then swap it in | build, validate, then assign |
| `raxShow` dumper | `xxxShow(FILE*)` | `impl fmt::Debug`; `String()` / `%+v` | `__repr__` / `dump()`; `toString()`/`inspect`; `toString()` |
| Guide comment | `/* 3a: ... */` | `// 3a: ...` | `# 3a: ...`; `// 3a: ...` |
