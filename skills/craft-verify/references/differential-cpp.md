# Differential fuzzing in C++

A libFuzzer target against a `std::map` (`Trie` is the system under test):

```cpp
// Each 3 bytes of input are one operation on the Trie and on a std::map; any
// divergence is a bug. Build with -fsanitize=fuzzer,address.
extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t *data, std::size_t size) {
    Trie sut;
    std::map<std::uint8_t, std::uint8_t> oracle;
    for (std::size_t i = 0; i + 2 < size; i += 3) {
        std::uint8_t key = data[i + 1], value = data[i + 2];
        if (data[i] % 2 == 0) {
            sut.insert(key, value);
            oracle[key] = value;
        } else {
            sut.remove(key);
            oracle.erase(key);
        }
    }
    assert(sut.size() == oracle.size());
    for (auto [key, value] : oracle) assert(sut.find(key) == value);
    return 0;
}
```
