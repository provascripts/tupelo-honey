//! Rust forms around a struct: derives, a packed repr, generics with a
//! lifetime and a where clause, field attributes and a zero-sized marker.

use core::marker::PhantomData;

/// Wire header: [len:1B][kind:1B][crc:4B]
#[derive(Debug, Clone, Copy)]
#[repr(C, packed)]
pub struct Header<'a, T: Copy>
where
    T: Default,
{
    #[doc(hidden)]
    pub len: u8,
    pub(crate) kind: u8,
    #[cfg_attr(feature = "serde", serde(rename = "crc32"))]
    crc: u32,
    _marker: PhantomData<&'a T>,
}

const _: () = assert!(core::mem::size_of::<Header<'static, u8>>() == 6);
