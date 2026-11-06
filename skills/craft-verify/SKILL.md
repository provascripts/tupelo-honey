---
name: craft-verify
description: >-
  Use when writing tests for data structures, parsers, allocators, caches,
  storage engines, codecs or state machines, or when code handles allocation,
  I/O, timeout or partial-write failures. Applies the rax-test.c / SQLite
  protocol: differential fuzzing against a trivially correct oracle, iterative
  fault injection (fail the k-th allocation or I/O call, check state and leaks),
  and testcase() boundary markers for MC/DC-style coverage, in any language.
license: MIT
---

# craft-verify: three orthogonal proofs, generated not typed

**Bulk is generated, never typed.** Do not hand-write 30 happy-path tests. Write one small harness (< 150 LOC) that generates the cases, seeded and replayable (print the seed on failure), and give it the three proofs below.

## Proof 1: differential fuzzing against a trivial oracle
Run the optimized structure (SUT) next to an obviously correct model (a hash table, `dict`, `Map`, a sorted array, a brute-force scan). Apply 10^4 to 10^6 seeded random operations; after each one compare return values, and at the end compare sizes and full contents. Bias the generator toward the encodings that are special (empty keys, NULL values, shared prefixes, maximum sizes).

From antirez/rax, `rax-test.c` (BSD-2-Clause; verbatim, trimmed with `[...]`):
```c
/* Perform a fuzz test, returns 0 on success, 1 on error. */
int fuzzTest(int keymode, size_t count, double addprob, double remprob) {
    hashtable *ht = htNew();
    rax *rax = raxNew();
    [...]
    /* Perform random operations on both the dictionaries. */
    for (size_t i = 0; i < count; i++) {
        [...]
        /* Insert element. */
        if ((double)rc4rand()/RAND_MAX < addprob) {
            keylen = int2key((char*)key,sizeof(key),i,keymode);
            void *val = (void*)(unsigned long)rc4rand();
            /* Stress NULL values more often, they use a special encoding. */
            if (!(rc4rand() % 100)) val = NULL;
            int retval1 = htAdd(ht,key,keylen,val);
            int retval2 = raxInsert(rax,key,keylen,val,NULL);
            if (retval1 != retval2) {
                printf("Fuzz: key insertion reported mismatching value in HT/RAX\n");
                return 1;
            }
        }
        [...]
    }

    /* Check that count matches. */
    if (ht->numele != raxSize(rax)) {
        [...]
```
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

## Proof 2: iterative fault injection
Make the 1st, then the 2nd, ..., then the k-th allocation (or write, fsync, socket call) of one operation fail, until the operation completes without reaching the fault. After every injected failure assert three things: a clean error (`NULL` + `ENOMEM`, `Err`, an exception), the pre-existing state intact (check it with the `*Show`/invariant checker or the oracle), and zero leaked blocks. SQLite runs this for every allocation site. rax's `rax_oom_malloc.h` instead fails a random 1 in 20 calls: keep that for soak runs, use the deterministic countdown in the suite, because only it is reproducible.

`craft_oom_harness.h`, a complete header for C modules with pluggable allocators:
```c
/* craft_oom_harness.h -- deterministic allocation-failure injection for tests.
 *
 * Point the module's allocator macros here, for rax:
 *   #define rax_malloc craft_malloc
 *   #define rax_realloc craft_realloc
 *   #define rax_free craft_free
 * then call craft_oom_arm(k) before an operation: its k-th allocation fails
 * once, every other one succeeds. craft_live counts live blocks for the
 * leak check. Not thread-safe: tests drive one operation at a time. */
#ifndef CRAFT_OOM_HARNESS_H
#define CRAFT_OOM_HARNESS_H
#include <stdlib.h>

static long craft_fail_at = 0;      /* 1-based allocation to fail; 0 = never. */
static long craft_alloc_count = 0;  /* Allocations attempted since craft_oom_arm(). */
static long craft_live = 0;         /* Blocks currently allocated. */

/* Arm the injector so that allocation number k, counted from now, fails once. */
static void craft_oom_arm(long k) {
    craft_fail_at = k;
    craft_alloc_count = 0;
}

/* Return 1, consuming the armed fault, if this allocation must fail. */
static int craft_should_fail(void) {
    if (craft_fail_at && ++craft_alloc_count == craft_fail_at) {
        craft_fail_at = 0;
        return 1;
    }
    return 0;
}

static void *craft_malloc(size_t size) {
    void *p;
    if (craft_should_fail()) return NULL;
    if ((p = malloc(size)) != NULL) craft_live++;
    return p;
}

/* Like realloc(): on failure 'ptr' stays valid and still owned by the caller. */
static void *craft_realloc(void *ptr, size_t size) {
    void *p;
    if (craft_should_fail()) return NULL;
    if ((p = realloc(ptr, size)) != NULL && ptr == NULL) craft_live++;
    return p;
}

static void craft_free(void *ptr) {
    if (ptr != NULL) craft_live--;
    free(ptr);
}

/* SQLite-style boundary marker, compiled out unless CRAFT_COVERAGE is set:
 * under coverage builds both outcomes of X must be observed at each site. */
#ifdef CRAFT_COVERAGE
extern unsigned int craft_coverage_counter;
#define testcase(X) do { if (X) craft_coverage_counter += (unsigned)__LINE__; } while (0)
#else
#define testcase(X) ((void)0)
#endif

#endif
```
The driver loop, with rax's real API:
```c
/* Fail the 1st, 2nd, ... allocation of one raxInsert() until it completes
 * without reaching the fault. Every injected failure must leave the tree
 * unchanged and leak-free. */
static void test_insert_oom(void) {
    for (long k = 1; ; k++) {
        rax *t = raxNew();
        raxInsert(t, (unsigned char *)"ANNIBALE", 8, NULL, NULL);
        long live = craft_live;
        craft_oom_arm(k);
        int added = raxInsert(t, (unsigned char *)"ANNIENTARE", 10, NULL, NULL);
        int faulted = craft_fail_at == 0;   /* the armed fault fired */
        craft_oom_arm(0);
        if (faulted) {
            assert(added == 0 && errno == ENOMEM);
            assert(raxSize(t) == 1);
            assert(raxFind(t, (unsigned char *)"ANNIBALE", 8) != raxNotFound);
            assert(craft_live == live);
        }
        raxFree(t);
        if (!faulted) {
            assert(added == 1);
            break;
        }
    }
}
```
I/O faults in Python, same three assertions:
```python
def test_put_survives_failed_rename(tmp_path):
    store = Store(tmp_path / "db")
    store.put(b"k", b"v1")
    with mock.patch("os.replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            store.put(b"k", b"v2")
    assert Store(tmp_path / "db").get(b"k") == b"v1"
```

## Proof 3: boundary values with testcase()
Mark both sides of every limit in the code, then make the suite hit each side. SQLite's macro (`sqliteInt.h`, public domain; verbatim, trimmed):
```c
** The testcase() macro is used to aid in coverage testing.  When
** doing coverage testing, the condition inside the argument to
** testcase() must be evaluated both true and false in order to
** get full branch coverage.  The testcase() macro is inserted
** to help ensure adequate test coverage in places where simple
** condition/decision coverage is inadequate.  For example, testcase()
** can be used to make sure boundary values are tested.
[...]
# define testcase(X)  if( X ){ sqlite3CoverageCounter += (unsigned)__LINE__; }
```
In a split routine like rax's:
```c
    testcase( j == 0 );              /* split at the first byte: step 3a */
    testcase( j == h->size - 1 );    /* split at the last byte: empty postfix, step 4b */
    testcase( len == RAX_NODE_MAX_SIZE );
```

## Translation table
| Proof | C / C++ / Zig | Rust / Go | Python / TypeScript / Java |
|---|---|---|---|
| Differential fuzzing | `rax-test.c` loop vs hash table; libFuzzer `LLVMFuzzerTestOneInput` | `proptest!` or `cargo fuzz` `fuzz_target!` vs `HashMap`; `func FuzzX(f *testing.F)` vs `map` | Hypothesis `RuleBasedStateMachine` vs `dict`; fast-check `fc.commands` + `fc.modelRun` vs `Map`; jqwik `@Property` vs `HashMap` |
| Fault injection, k = 1..N | `craft_oom_harness.h`; Zig `std.testing.checkAllAllocationFailures`, `FailingAllocator` | allocator or I/O trait returning `Err` at call k (process OOM aborts in Rust: test `try_reserve` paths); `testing/iotest` readers, fake `io.Writer` | `mock.patch(..., side_effect=OSError)`; stubbed `fs` throwing on call k; Mockito `doThrow` |
| Boundaries | `testcase(X)` | table tests at limit-1, limit, limit+1 | `pytest.mark.parametrize`; `test.each`; JUnit `@ParameterizedTest` |
