# Pin the diagram: layout assertions in Python

The script can check that a diagram names the fields in order with plausible widths; only the compiler knows padding and real offsets. Put the numbers of the diagram in an assertion next to the type:

```python
class Hdr(ctypes.LittleEndianStructure):
    """Wire header: [len:1B][flags:3][rest:5]"""
    _pack_ = 1
    _fields_ = [("len", ctypes.c_uint8), ("flags", ctypes.c_uint8, 3), ("rest", ctypes.c_uint8, 5)]


assert ctypes.sizeof(Hdr) == 2, "Hdr layout changed: update the diagram"
```
