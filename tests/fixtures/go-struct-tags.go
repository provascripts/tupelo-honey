// Package wire shows Go struct tags, an embedded field and a load-time
// layout check: tags are string literals, so they never read as types.
package wire

import (
	"sync"
	"unsafe"
)

// Header in memory: [Len:1B][Flags:1B][pad:2B][Crc:4B][Mutex:8B]
type Header struct {
	Len   uint8  `json:"len" binary:"1"`
	Flags uint8  `json:"flags"`
	Crc   uint32 `json:"crc"`
	sync.Mutex
}

func init() {
	if unsafe.Sizeof(Header{}) != 16 || unsafe.Offsetof(Header{}.Crc) != 4 {
		panic("Header layout changed: update the diagram")
	}
}
