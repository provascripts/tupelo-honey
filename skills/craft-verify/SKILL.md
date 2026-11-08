---
name: craft-verify
description: >-
  Use when writing tests for data structures, parsers, allocators, caches,
  storage engines, codecs or state machines, or when code handles allocation,
  I/O, timeout or partial-write failures. Applies the rax-test.c / SQLite
  protocol: differential fuzzing against a trivially correct oracle, iterative
  fault injection (fail the k-th allocation or I/O call, check state and leaks),
  testcase() boundary markers for MC/DC-style coverage, and threshold crossings
  of dual representations, in any language.
license: MIT
---

# craft-verify: four orthogonal proofs, generated not typed

**Bulk is generated, never typed.** Do not hand-write 30 happy-path tests. Write one small harness (< 150 LOC) that generates the cases, seeded and replayable (print the seed on failure), and give it the proofs below.

## Proof 1: differential fuzzing against a trivial oracle
Run the optimized structure (SUT) next to an obviously correct model (a hash table, `dict`, `Map`, a sorted array, a brute-force scan). Apply 10^4 to 10^6 seeded random operations; after each one compare return values, and at the end compare sizes and full contents. Bias the generator toward the encodings that are special (empty keys, NULL values, shared prefixes, maximum sizes).

From antirez/rax, `rax-test.c` (BSD-3-Clause, see LICENSE; verbatim, trimmed with `[...]`):
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

## Proof 2: iterative fault injection
Make the 1st, then the 2nd, ..., then the k-th allocation (or write, fsync, socket call) of one operation fail, until the operation completes without reaching the fault. After every injected failure assert a clean error (`NULL` + `ENOMEM`, `Err`, an exception) and the logical state intact against the oracle: size, a lookup of every key, ordered iteration. Compare what the structure means, not its shape: rax's `oom:` path leaves a split node uncompressed, which is correct and still frees everything later. Count leaks after teardown (free the structure, then the live-block count must equal the one taken before it was created), and assert that at least one fault fired: a harness that never reaches the module passes every other check. SQLite runs this for every allocation site. rax's `rax_oom_malloc.h` instead fails a random 1 in 20 calls: keep that for soak runs, use the deterministic countdown in the suite, because only it is reproducible.

## Proof 3: boundary values with testcase()
Mark both sides of every limit in the code, then make the suite hit each side: `testcase( j == 0 );`, `testcase( len == RAX_NODE_MAX_SIZE );`.

## Proof 4: both sides of a dual representation
A structure with a small and a large form (`references/encodings.md` in craft-design) gets a differential run in each form against the oracle and a workload that stops at threshold-1, threshold and threshold+1. Answers stay the same across the conversion, and the conversion never goes back: emptying a converted structure leaves it in the large form.

## Language references
In `references/`: `differential-<lang>.md` (Proof 1; cpp, go, java, python, rust, typescript), `fault-injection-<lang>.md` (Proof 2; c, python, zig; `fault-injection-c.md` is a complete OOM harness and its driver, ready to compile), `boundaries-c.md` (Proof 3). **Read only the file for the language in use.**

## Translation table
| Proof | C / C++ / Zig | Rust / Go | Python / TypeScript / Java |
|---|---|---|---|
| Differential fuzzing | `rax-test.c` loop vs hash table; libFuzzer `LLVMFuzzerTestOneInput` | `proptest!` or `cargo fuzz` `fuzz_target!` vs `HashMap`; `func FuzzX(f *testing.F)` vs `map` | Hypothesis `RuleBasedStateMachine` vs `dict`; fast-check `fc.commands` + `fc.modelRun` vs `Map`; jqwik `@Property` vs `HashMap` |
| Fault injection, k = 1..N | `craft_oom_harness.h`; Zig `std.testing.checkAllAllocationFailures`, `FailingAllocator` | allocator or I/O trait returning `Err` at call k (process OOM aborts in Rust: test `try_reserve` paths); `testing/iotest` readers, fake `io.Writer` | `mock.patch(..., side_effect=OSError)`; stubbed `fs` throwing on call k; Mockito `doThrow` |
| Boundaries | `testcase(X)` | table tests at limit-1, limit, limit+1 | `pytest.mark.parametrize`; `test.each`; JUnit `@ParameterizedTest` |
