# Design comment in Go

Illustrative, not compiled: no toolchain for this language was available when the examples were last checked.

The same frame codec, in `doc.go` and beside the reader:

```go
// Package frame is a length-prefixed codec for the replication stream.
//
// Every frame is a fixed 5-byte header followed by the payload, so a reader
// learns the payload size before touching it and never scans for a delimiter.
//
// Layout, little-endian: [len:4B][kind:1B][payload: len bytes]
//
// Invariant: len <= MaxFrame is checked before allocating.
package frame
```
```go
// PayloadLen returns the payload length of the frame at the start of buf,
// or ErrIncomplete if buf is shorter than the header.
func PayloadLen(buf []byte) (uint32, error) {
	if len(buf) < 5 {
		return 0, ErrIncomplete
	}
	return binary.LittleEndian.Uint32(buf), nil
}
```
