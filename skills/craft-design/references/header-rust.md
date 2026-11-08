# Design comment in Rust

A length-prefixed codec header, `//!` at the top of `lib.rs`:

```rust
//! Length-prefixed frame codec for the replication stream.
//!
//! Every frame is a fixed 5-byte header followed by the payload, so a reader
//! learns the payload size before touching it and never scans for a
//! delimiter. Rejected: newline-delimited JSON, which needs escaping, a
//! stateful parser and an unbounded read to find where a frame ends.
//!
//! Layout, little-endian:
//!   0..4   len      u32, payload bytes, header excluded
//!   4      kind     u8, 1 = data, 2 = ack, anything else is rejected
//!   5..n   payload  len bytes
//!
//! Invariant: `len <= MAX_FRAME` is checked before allocating, so a corrupt
//! or hostile header can never make us reserve 4 GiB.
//!
//! Known limitations: no internal buffering. A partial header returns
//! `Error::Incomplete` and the caller reads more bytes and retries.
```
