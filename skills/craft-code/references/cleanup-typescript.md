# One cleanup path in TypeScript

TypeScript, handles start `undefined`, one `finally` (the shape of C's `goto err`):

```ts
/** Copy src to dst; both handles are closed on every path, errors included. */
export async function copyFile(src: string, dst: string): Promise<void> {
  let input: FileHandle | undefined;
  let output: FileHandle | undefined;
  try {
    input = await open(src, "r");
    output = await open(dst, "w");
    await output.writeFile(await input.readFile());
  } finally {
    await output?.close();
    await input?.close();
  }
}
```
