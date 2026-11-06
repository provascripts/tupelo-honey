// C# sequential layout with field attributes, an auto-property, a constant,
// a computed property, a method and a fixed buffer: only Len, Size and Name
// have storage, in this order.
using System.Runtime.InteropServices;

// [Len:1B][Size:4B][Name:16B]
[StructLayout(LayoutKind.Sequential, Pack = 1)]
public unsafe struct Header {
    [MarshalAs(UnmanagedType.U1)] public byte Len;
    public int Size { get; set; }
    public const int MaxName = 16;
    public int Twice => Size * 2;
    public void Reset() { Len = 0; Size = 0; }
    public fixed byte Name[16];
}

public static class HeaderLayout {
    public static void Check() {
        System.Diagnostics.Debug.Assert(Marshal.SizeOf<Header>() == 21);
    }
}
