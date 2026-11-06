"""ctypes layouts: a nested Structure field, an array and bitfields.

Field names are strings in _fields_, so the parser reads the raw tuples; a
type it cannot size (a nested Structure, a POINTER) keeps its name only.
"""
import ctypes


class Point(ctypes.Structure):
    """[x:2B][y:2B]"""
    _pack_ = 1
    _fields_ = [("x", ctypes.c_int16), ("y", ctypes.c_int16)]


class Packet(ctypes.LittleEndianStructure):
    """Wire packet: [kind:1B][flags:3][spare:5][origin:4B][data:8B]"""
    _pack_ = 1
    _fields_ = [
        ("kind", ctypes.c_uint8),
        ("flags", ctypes.c_uint8, 3),
        ("spare", ctypes.c_uint8, 5),
        ("origin", Point),
        ("data", ctypes.c_uint8 * 8),
    ]


assert ctypes.sizeof(Point) == 4, "Point layout changed: update the diagram"
assert ctypes.sizeof(Packet) == 14, "Packet layout changed: update the diagram"
