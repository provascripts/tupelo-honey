# One cleanup path in Rust

Rust, `?` plus `Drop`:

```rust
/// Copy `src` to `dst`, returning the bytes written. Both files close when
/// they go out of scope; on failure the partial `dst` is removed.
fn copy_file(src: &Path, dst: &Path) -> io::Result<u64> {
    let mut input = File::open(src)?;
    let mut output = File::create(dst)?;
    let result = io::copy(&mut input, &mut output);
    // Why: close before removing, Windows cannot delete an open file.
    drop(output);
    if result.is_err() {
        let _ = fs::remove_file(dst);
    }
    result
}
```
