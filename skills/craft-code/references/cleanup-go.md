# One cleanup path in Go

Illustrative, not compiled: no toolchain for this language was available when the examples were last checked.

Go, named result plus one deferred closure:

```go
// CopyFile copies src to dst. Both files are always closed; on failure dst
// is removed, so a partial copy never survives.
func CopyFile(dst, src string) (err error) {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	defer func() {
		if cerr := out.Close(); err == nil {
			err = cerr
		}
		if err != nil {
			os.Remove(dst)
		}
	}()
	_, err = io.Copy(out, in)
	return err
}
```
