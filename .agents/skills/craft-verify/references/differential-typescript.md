# Differential fuzzing in TypeScript

fast-check model-based commands against a `Map` (`Trie` is the system under test):

```ts
import fc from "fast-check";

/** One random insert, applied to the Trie and checked against the Map. */
class Insert implements fc.Command<Map<string, number>, Trie> {
  constructor(readonly key: string, readonly value: number) {}
  check = () => true;
  run(model: Map<string, number>, real: Trie): void {
    expect(real.insert(this.key, this.value)).toBe(!model.has(this.key));
    model.set(this.key, this.value);
    expect(real.size).toBe(model.size);
  }
  toString = () => `insert(${this.key})`;
}

/** One random remove, applied to the Trie and checked against the Map. */
class Remove implements fc.Command<Map<string, number>, Trie> {
  constructor(readonly key: string) {}
  check = () => true;
  run(model: Map<string, number>, real: Trie): void {
    expect(real.remove(this.key)).toBe(model.delete(this.key));
    expect(real.size).toBe(model.size);
  }
  toString = () => `remove(${this.key})`;
}

test("the Trie matches a Map", () => {
  const key = fc.string({ maxLength: 8 });
  fc.assert(fc.property(fc.commands([
    fc.tuple(key, fc.integer()).map(([k, v]) => new Insert(k, v)),
    key.map((k) => new Remove(k)),
  ]), (cmds) => fc.modelRun(() => ({ model: new Map<string, number>(), real: new Trie() }), cmds)));
});
```
