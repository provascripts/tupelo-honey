//! A Zig packed struct with a backing integer and a method: functions and
//! declarations inside a struct carry no storage.
const std = @import("std");

// [iskey:1b][size:31b]
const Node = packed struct(u32) {
    iskey: u1,
    size: u31,

    pub fn isEmpty(self: Node) bool {
        return self.size == 0;
    }
};

comptime {
    std.debug.assert(@bitSizeOf(Node) == 32);
    std.debug.assert(@bitOffsetOf(Node, "size") == 1);
}
