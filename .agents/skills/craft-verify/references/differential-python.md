# Differential fuzzing in Python

The same proof in Python with Hypothesis stateful testing (here `Trie` is the system under test):

```python
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from trie import Trie

KEYS = st.binary(max_size=8)


class TrieVsDict(RuleBasedStateMachine):
    """Every operation runs on the Trie and on a dict; any divergence is a bug."""

    def __init__(self):
        super().__init__()
        self.sut, self.oracle = Trie(), {}

    @rule(key=KEYS, value=st.integers())
    def insert(self, key, value):
        assert self.sut.insert(key, value) == (key not in self.oracle)
        self.oracle[key] = value

    @rule(key=KEYS)
    def remove(self, key):
        assert self.sut.remove(key) == (key in self.oracle)
        self.oracle.pop(key, None)

    @invariant()
    def same_contents(self):
        assert len(self.sut) == len(self.oracle)
        for key, value in self.oracle.items():
            assert self.sut.find(key) == value


TestTrieVsDict = TrieVsDict.TestCase
```
