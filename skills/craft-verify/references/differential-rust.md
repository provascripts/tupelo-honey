# Differential fuzzing in Rust

`proptest` against a `HashMap` (`Trie` is the system under test):

```rust
use proptest::prelude::*;
use std::collections::HashMap;

proptest! {
    /// Every operation runs on the Trie and on a HashMap; any divergence is a bug.
    #[test]
    fn trie_matches_hashmap(ops in prop::collection::vec((any::<bool>(), any::<[u8; 2]>(), any::<u32>()), 0..1000)) {
        let (mut sut, mut oracle) = (Trie::new(), HashMap::new());
        for (insert, key, value) in ops {
            if insert {
                prop_assert_eq!(sut.insert(&key, value), oracle.insert(key, value));
            } else {
                prop_assert_eq!(sut.remove(&key), oracle.remove(&key));
            }
        }
        prop_assert_eq!(sut.len(), oracle.len());
    }
}
```
