/* cursor.c -- a bounded path cursor over a prefix tree.
 *
 * Hand-written fixture, no code copied from rax or sds: the comment shapes an
 * expert writes that a lexical checker must never block. A label on a
 * declaration (field, enum member, macro, initializer slot, prototype)
 * documents a name; a comment that explains a condition in its own words is
 * at most a review signal; an operator titles a comparison case exactly. */
#include <stddef.h>
#include <string.h>

#define CURSOR_MAX_DEPTH 32 /* Max cursor depth. */

/* Seek direction. */
enum seekOp {
    SEEK_FIRST = 0, /* Seek first. */
    SEEK_LAST       /* Seek last. */
};

typedef struct cursorPath {
    size_t path_depth;                  /* Path depth. */
    int path_truncated;                 /* Path truncated. */
    void *path_nodes[CURSOR_MAX_DEPTH]; /* Path nodes. */
} cursorPath;

/* Compare two keys of `len` bytes; return <0, 0 or >0 like memcmp(). */
int cursorKeyCompare(const void *a, const void *b, size_t len);

/* Return the number of nodes on the path, or 0 when the descent stopped
 * early because the path did not fit in CURSOR_MAX_DEPTH slots. The
 * caller must then restart the seek from the root. */
size_t cursorDepth(const cursorPath *path) {
    if (path == NULL) return 0;
    /* Return failure on truncated path info. */
    if (path->path_truncated) return 0;

    return path->path_depth;
}

/* Compare callbacks of a cursor, by slot. */
static int (*const cursorCompare[])(const void *, const void *, size_t) = {
    cursorKeyCompare, /* key compare */
    NULL              /* value compare: values are opaque pointers */
};

/* Return 1 when a key that compares as `cmp` against the bound satisfies the
 * seek operator given by `lt`, `gt` and `eq` (">=" sets gt and eq), else 0.
 * Pure function: no side effects, no allocation. */
int cursorMatch(int cmp, int lt, int gt, int eq) {
    /* Handle == */
    if (lt == 0 && gt == 0) return cmp == 0;

    /* Handle >, >=, <, <= */
    if (cmp == 0) return eq;
    if (lt) return cmp < 0;
    return cmp > 0;
}

/* Return the comparison callback of slot `slot` (0: keys, 1: values), or
 * NULL when the slot compares nothing. */
int (*cursorComparator(int slot))(const void *, const void *, size_t) {
    if (slot < 0 || slot > 1) return NULL;
    return cursorCompare[slot];
}
