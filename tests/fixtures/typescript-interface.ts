// TypeScript interfaces have no memory layout, so their cell-like comments
// are never checked; debt markers inside literals are never debt either.

// [kind:1B][len:2B][crc:4B]
export interface Frame<T extends object = {}> {
  readonly len: number;
  kind?: number;
  crc: number;
  meta?: T;
}

export const samples = ["text // TODO: keep", `line // FIXME ${1}`, /a \/\/ shortcut: \d+/g];
