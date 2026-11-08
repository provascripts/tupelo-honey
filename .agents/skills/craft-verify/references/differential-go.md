# Differential fuzzing in Go

Illustrative, not compiled: no toolchain for this language was available when the examples were last checked.

`go test -fuzz` against a `map` (`Trie` is the system under test):

```go
// FuzzTrie replays the fuzzer's bytes as operations on the Trie and on a map;
// any divergence is a bug. Run with: go test -fuzz=FuzzTrie
func FuzzTrie(f *testing.F) {
	f.Add([]byte{0, 1, 2})
	f.Fuzz(func(t *testing.T, ops []byte) {
		sut, oracle := NewTrie(), map[byte]byte{}
		for i := 0; i+2 < len(ops); i += 3 {
			key, val := ops[i+1], ops[i+2]
			if ops[i]%2 == 0 {
				sut.Insert(key, val)
				oracle[key] = val
			} else {
				sut.Remove(key)
				delete(oracle, key)
			}
		}
		if sut.Len() != len(oracle) {
			t.Fatalf("size: trie %d, map %d", sut.Len(), len(oracle))
		}
		for k, v := range oracle {
			if got, ok := sut.Find(k); !ok || got != v {
				t.Fatalf("key %d: trie %d, %v, map %d", k, got, ok, v)
			}
		}
	})
}
```
