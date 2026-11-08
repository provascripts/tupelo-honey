# One cleanup path in Python

Python, one `ExitStack`:

```python
def merge(paths, out_path):
    """Concatenate the files in `paths` into `out_path`; every opened file is closed on all paths."""
    with contextlib.ExitStack() as stack:
        sources = [stack.enter_context(open(p, "rb")) for p in paths]
        out = stack.enter_context(open(out_path, "wb"))
        for src in sources:
            shutil.copyfileobj(src, out)
```
