# Pin the diagram: layout assertions in Zig

The script can check that a diagram names the fields in order with plausible widths; only the compiler knows padding and real offsets. Put the numbers of the diagram in an assertion next to the type:

```zig
// [iskey:1b][size:31b]
const Node = packed struct(u32) { iskey: u1, size: u31 };
comptime {
    std.debug.assert(@bitSizeOf(Node) == 32);
    std.debug.assert(@bitOffsetOf(Node, "size") == 1);
}
```
