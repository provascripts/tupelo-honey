"""Debt markers inside literals are text, never debt; a comment that titles a
block of statements is a Guide comment, never Trivial."""

MESSAGE = "literal text # TODO: keep this string unchanged"
BANNER = """multi-line literal
 # FIXME this line lives inside a string
 /* shortcut: and so does this one */
"""
RAW = r"C:\path # TODO: raw strings too"
FORMATTED = f"{MESSAGE} # FIXME: and f-strings"


def free(buffer):
    """Empty `buffer` in place."""
    buffer.clear()


def free_client(client):
    """Release the buffers a client owns; the block below has the Guide comment shape."""
    # Free the query buffer.
    free(client.query_buf)
    free(client.pending_query_buf)
    client.query_buf = None

    return client
