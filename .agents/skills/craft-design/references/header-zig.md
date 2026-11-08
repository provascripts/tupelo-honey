# Design comment in Zig

The same frame codec, `//!` at the top of the file:

```zig
//! Length-prefixed frame codec for the replication stream.
//!
//! Every frame is a fixed 5-byte header followed by the payload, so a reader
//! learns the payload size before touching it and never scans for a delimiter.
//!
//! Layout, little-endian: [len:4B][kind:1B][payload: len bytes]
const std = @import("std");

pub const Header = extern struct { len: u32 align(1), kind: u8 };

comptime {
    std.debug.assert(@sizeOf(Header) == 5);
}

/// Return the payload length of the frame at the start of `buf`, or
/// error.Incomplete if `buf` is shorter than the header.
pub fn payloadLen(buf: []const u8) !u32 {
    if (buf.len < @sizeOf(Header)) return error.Incomplete;
    return std.mem.readInt(u32, buf[0..4], .little);
}
```
