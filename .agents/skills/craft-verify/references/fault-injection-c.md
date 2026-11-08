# Fault injection in C

`craft_oom_harness.h`, a complete header for C modules with pluggable allocators:

```c
/* craft_oom_harness.h -- deterministic allocation-failure injection for tests.
 *
 * Point the module's allocator macros here, for rax in its rax_malloc.h:
 *   #include "craft_oom_harness.h"
 *   #define rax_malloc craft_malloc
 *   #define rax_realloc craft_realloc
 *   #define rax_free craft_free
 * Define CRAFT_OOM_IMPL in exactly one .c file, before it includes this
 * header: that file holds the counters and the functions, every other file
 * sees declarations only, so the module and the test share one injector.
 * Then call craft_oom_arm(k) before an operation: its k-th allocation fails
 * once, every other one succeeds. Not thread-safe: tests drive one
 * operation at a time. */
#ifndef CRAFT_OOM_HARNESS_H
#define CRAFT_OOM_HARNESS_H
#include <stddef.h>

extern long craft_fail_at;      /* 1-based allocation to fail; 0 = never. */
extern long craft_alloc_count;  /* Allocations attempted since craft_oom_arm(). */
extern long craft_live;         /* Blocks currently allocated. */
extern long craft_faults;       /* Faults injected so far; 0 = the module never asked. */

void craft_oom_arm(long k);
void *craft_malloc(size_t size);
void *craft_realloc(void *ptr, size_t size);
void craft_free(void *ptr);

/* SQLite-style boundary marker, compiled out unless CRAFT_COVERAGE is set:
 * under coverage builds both outcomes of X must be observed at each site. */
#ifdef CRAFT_COVERAGE
extern unsigned int craft_coverage_counter;
#define testcase(X) do { if (X) craft_coverage_counter += (unsigned)__LINE__; } while (0)
#else
#define testcase(X) ((void)0)
#endif

#ifdef CRAFT_OOM_IMPL
#include <stdlib.h>

long craft_fail_at, craft_alloc_count, craft_live, craft_faults;
unsigned int craft_coverage_counter;

/* Arm the injector so that allocation number k, counted from now, fails once. */
void craft_oom_arm(long k) {
    craft_fail_at = k;
    craft_alloc_count = 0;
}

/* Return 1, consuming and counting the armed fault, if this allocation must fail. */
static int craft_should_fail(void) {
    if (craft_fail_at && ++craft_alloc_count == craft_fail_at) {
        craft_fail_at = 0;
        craft_faults++;
        return 1;
    }
    return 0;
}

void *craft_malloc(size_t size) {
    void *p;
    if (craft_should_fail()) return NULL;
    if ((p = malloc(size)) != NULL) craft_live++;
    return p;
}

/* Like realloc(): on failure 'ptr' stays valid and still owned by the caller.
 * Never pass size 0: C leaves realloc(ptr, 0) to the implementation (glibc
 * frees the block and returns NULL, others return a new block), so the live
 * count would drift. Free with craft_free() instead. */
void *craft_realloc(void *ptr, size_t size) {
    void *p;
    if (craft_should_fail()) return NULL;
    if ((p = realloc(ptr, size)) != NULL && ptr == NULL) craft_live++;
    return p;
}

void craft_free(void *ptr) {
    if (ptr != NULL) craft_live--;
    free(ptr);
}
#endif /* CRAFT_OOM_IMPL */

#endif
```

The driver loop, with rax's real API (`assert.h`, `errno.h` and `string.h` included):

```c
/* Sorted oracle keys; key j holds the value (void *)(j + 1). */
static const char *oracle[] = {"ANNIBALE", "ANNIENTARE"};

/* Assert that 't' holds exactly the first 'n' oracle keys: size, lookups, order. */
static void check_oracle(rax *t, size_t n) {
    raxIterator it;
    size_t i = 0, j;
    assert(raxSize(t) == n);
    for (j = 0; j < n; j++)
        assert(raxFind(t, (unsigned char *)oracle[j], strlen(oracle[j])) == (void *)(j + 1));
    raxStart(&it, t);
    raxSeek(&it, "^", NULL, 0);
    while (raxNext(&it)) {
        assert(i < n && it.key_len == strlen(oracle[i]) && memcmp(it.key, oracle[i], it.key_len) == 0);
        i++;
    }
    raxStop(&it);
    assert(i == n);
}

/* Fail the 1st, 2nd, ... allocation of one raxInsert() until it completes
 * without reaching the fault. After every injected failure the tree must
 * still mean what the oracle says. Its shape may differ, so leaks are
 * counted after raxFree(), never while the tree is alive. */
static void test_insert_oom(void) {
    long base = craft_live, k;
    for (k = 1; ; k++) {
        long faults = craft_faults;
        int added;
        rax *t = raxNew();
        raxInsert(t, (unsigned char *)oracle[0], strlen(oracle[0]), (void *)1, NULL);
        craft_oom_arm(k);
        added = raxInsert(t, (unsigned char *)oracle[1], strlen(oracle[1]), (void *)2, NULL);
        craft_oom_arm(0);
        if (craft_faults > faults) {
            assert(added == 0 && errno == ENOMEM);
            check_oracle(t, 1);
        } else {
            assert(added == 1);
            check_oracle(t, 2);
        }
        raxFree(t);
        assert(craft_live == base);
        if (craft_faults == faults) break;
    }
    assert(craft_faults > 0);   /* The injector reached the module at least once. */
}
```
