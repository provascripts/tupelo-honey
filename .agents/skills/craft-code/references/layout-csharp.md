# Pin the diagram: layout assertions in C#

The script can check that a diagram names the fields in order with plausible widths; only the compiler knows padding and real offsets. Put the numbers of the diagram in an assertion next to the type:

```csharp
/// <summary>Wire header: [len:1B][kind:1B][crc:4B]</summary>
[StructLayout(LayoutKind.Sequential, Pack = 1)]
struct Hdr { public byte Len; public byte Kind; public uint Crc; }

// In a test: Marshal knows the layout the runtime really uses.
Debug.Assert(Marshal.SizeOf<Hdr>() == 6);
Debug.Assert((int)Marshal.OffsetOf<Hdr>(nameof(Hdr.Crc)) == 2);
```
