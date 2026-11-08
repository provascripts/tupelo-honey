# Fault injection in Python

I/O faults in Python, same assertions:

```python
def test_put_survives_failed_rename(tmp_path):
    store = Store(tmp_path / "db")
    store.put(b"k", b"v1")
    with mock.patch("os.replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            store.put(b"k", b"v2")
    assert Store(tmp_path / "db").get(b"k") == b"v1"
```
