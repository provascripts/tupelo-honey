# Pin the diagram: layout assertions in C

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
