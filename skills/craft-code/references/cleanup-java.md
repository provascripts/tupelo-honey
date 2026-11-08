# One cleanup path in Java

Illustrative, not compiled: no toolchain for this language was available when the examples were last checked.

Java, try-with-resources:

```java
/** Copies src to dst and returns the bytes copied; both streams close on every path. */
static long copy(Path src, Path dst) throws IOException {
    try (InputStream in = Files.newInputStream(src);
         OutputStream out = Files.newOutputStream(dst)) {
        return in.transferTo(out);
    }
}
```
