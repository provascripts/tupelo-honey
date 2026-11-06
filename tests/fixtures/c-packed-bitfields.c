/* Exact C layouts: a packed sds-style header and raxNode-style bitfields.
 * Every cell names a field in declaration order with its fixed width, and
 * the assertions below each type pin what its diagram claims. */
#include <stddef.h>
#include <stdint.h>

/* sdshdr8, one allocation, header right before the string:
 *   [len:1B][alloc:1B][flags:1B][buf: len bytes]
 */
struct __attribute__ ((__packed__)) sdshdr8 {
    uint8_t len;
    uint8_t alloc;
    unsigned char flags;
    char buf[];
};
_Static_assert(offsetof(struct sdshdr8, buf) == 3, "diagram: 3-byte header");

typedef struct raxNode {
    uint32_t iskey:1;
    uint32_t isnull:1;
    uint32_t iscompr:1;
    uint32_t size:29;
    /* [header:32b][data:size bytes] */
    unsigned char data[];
} raxNode;
_Static_assert(sizeof(raxNode) == 4, "diagram: 32-bit header, data follows");
