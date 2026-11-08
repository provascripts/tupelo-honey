# Fault injection in Zig

`std.testing.checkAllAllocationFailures` fails the 1st, 2nd, ... allocation of the function and checks for leaks:

```zig
const std = @import("std");

/// The operation under test; every allocation goes through `allocator`.
fn putTwice(allocator: std.mem.Allocator) !void {
    var map = std.StringHashMap(u32).init(allocator);
    defer map.deinit();
    try map.put("a", 1);
    try map.put("b", 2);
    try std.testing.expectEqual(@as(u32, 2), map.count());
}

test "putTwice survives every allocation failure" {
    try std.testing.checkAllAllocationFailures(std.testing.allocator, putTwice, .{});
}
```
