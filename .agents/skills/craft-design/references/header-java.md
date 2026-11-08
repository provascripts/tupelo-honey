# Design comment in Java

Illustrative, not compiled: no toolchain for this language was available when the examples were last checked.

The same frame codec, in `package-info.java` and beside the reader:

```java
/**
 * Length-prefixed frame codec for the replication stream.
 *
 * <p>Every frame is a fixed 5-byte header followed by the payload, so a reader
 * learns the payload size before touching it and never scans for a delimiter.
 *
 * <p>Layout, little-endian: {@code [len:4B][kind:1B][payload: len bytes]}
 *
 * <p>Invariant: {@code len <= MAX_FRAME} is checked before allocating.
 */
package org.example.frame;
```
```java
/** Returns the payload length of the frame at the buffer's position; sets the buffer little-endian, moves no position. */
static long payloadLen(ByteBuffer buf) {
    return Integer.toUnsignedLong(buf.order(ByteOrder.LITTLE_ENDIAN).getInt(buf.position()));
}
```
