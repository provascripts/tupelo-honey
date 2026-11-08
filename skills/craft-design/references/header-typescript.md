# Design comment in TypeScript

The same frame codec, at the top of `index.ts`:

```ts
/**
 * Length-prefixed frame codec for the replication stream.
 *
 * Every frame is a fixed 5-byte header followed by the payload, so a reader
 * learns the payload size before touching it and never scans for a delimiter.
 *
 * Layout, little-endian: [len:4B][kind:1B][payload: len bytes]
 *
 * Invariant: len <= MAX_FRAME is checked before allocating.
 */
export const HEADER_SIZE = 5;

/** Return the payload length of the frame at offset 0 of `view`; throws RangeError if `view` is too short. */
export function payloadLen(view: DataView): number {
  return view.getUint32(0, true);
}
```
