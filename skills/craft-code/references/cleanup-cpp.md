# One cleanup path in C++

C++, RAII: the owner's destructor is the teardown block:

```cpp
// Read the whole file at `path`. The unique_ptr closes the file on every
// path, exceptions included, so there is no teardown to keep in sync.
// Throws std::system_error if it cannot open or read the file.
std::string load_file(const char *path) {
    std::unique_ptr<std::FILE, int (*)(std::FILE *)> fp(std::fopen(path, "rb"), &std::fclose);
    if (!fp) throw std::system_error(errno, std::generic_category(), path);
    std::string buf;
    char chunk[4096];
    while (std::size_t n = std::fread(chunk, 1, sizeof chunk, fp.get())) buf.append(chunk, n);
    if (std::ferror(fp.get())) throw std::system_error(EIO, std::generic_category(), path);
    return buf;
}
```
