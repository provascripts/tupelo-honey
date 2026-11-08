# Pin the diagram: layout assertions in Go

Illustrative, not compiled: no toolchain for this language was available when the examples were last checked.

The script can check that a diagram names the fields in order with plausible widths; only the compiler knows padding and real offsets. Put the numbers of the diagram in an assertion next to the type:

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
