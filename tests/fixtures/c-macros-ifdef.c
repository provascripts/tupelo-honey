/* Graceful degradation: members the struct parser cannot read with
 * certainty (a macro-typed field, an #ifdef, an anonymous union) make the
 * struct inexact, so only the order and fixed widths it read are checked. */
#include <stdint.h>
#include <sys/queue.h>

/* Cache entry on LP64, DEBUG build:
 *   [len:4B][link:16B][stamp:8B][dbg:4B][kind:4B][u:8B]
 */
struct entry {
    uint32_t len;
    LIST_ENTRY(entry) link;
    __attribute__((aligned(8))) uint64_t stamp;
#ifdef DEBUG
    uint32_t dbg;
#endif
    uint32_t kind;
    union {
        uint64_t i;
        double d;
    } u;
};
_Static_assert(sizeof(struct entry) >= 40, "diagram: at least 40 bytes on LP64");

/* Tagged value: [tag:1B][pad:7B][i:8B]; i and d share their 8 bytes. */
struct tagged {
    uint8_t tag;
    union {
        int64_t i;
        double d;
    };
};
_Static_assert(sizeof(struct tagged) == 16, "diagram: tag, pad, 8-byte union");
