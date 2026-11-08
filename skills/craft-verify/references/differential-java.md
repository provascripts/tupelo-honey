# Differential fuzzing in Java

Illustrative, not compiled: no toolchain for this language was available when the examples were last checked.

jqwik against a `HashMap` (`Trie` is the system under test):

```java
class TrieProperties {
    /** Every third key is removed, the others inserted, in the Trie and in a HashMap; their contents must match. */
    @Property
    void matchesHashMap(@ForAll List<@AlphaChars @StringLength(max = 8) String> keys) {
        Trie sut = new Trie();
        Map<String, Integer> oracle = new HashMap<>();
        for (int i = 0; i < keys.size(); i++) {
            String key = keys.get(i);
            if (i % 3 == 2) {
                assertEquals(oracle.remove(key) != null, sut.remove(key));
            } else {
                assertEquals(oracle.put(key, i) == null, sut.insert(key, i));
            }
        }
        assertEquals(oracle.size(), sut.size());
        oracle.forEach((key, value) -> assertEquals(value, sut.find(key)));
    }
}
```
