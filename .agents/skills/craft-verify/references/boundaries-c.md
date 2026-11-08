# Boundary values in C

Illustrative, not compiled: a quoted excerpt and a fragment of a larger function.

SQLite's macro (`sqliteInt.h`, public domain; verbatim, trimmed):

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
