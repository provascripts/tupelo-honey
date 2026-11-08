# One cleanup path in Zig

Zig, `defer` plus `errdefer` (compiled on Zig 0.17.1; earlier releases open files through `std.fs` and take no `io`):

```zig
/// Read the whole file at `path`. The caller owns the returned slice and
/// frees it with `allocator.free`.
fn loadFile(io: std.Io, allocator: std.mem.Allocator, path: []const u8) ![]u8 {
    const file = try std.Io.Dir.cwd().openFile(io, path, .{});
    defer file.close(io);
    const buf = try allocator.alloc(u8, @intCast(try file.length(io)));
    errdefer allocator.free(buf);
    _ = try file.readPositionalAll(io, buf, 0);
    return buf;
}
```
