# One cleanup path in C

C, `goto` to a single teardown:

```c
/* Read the whole file at 'path' into a new NUL-terminated buffer and store
 * its size in '*len'. Returns NULL on any failure; on success the caller
 * owns the buffer and frees it. */
char *load_file(const char *path, size_t *len) {
    char *buf = NULL;
    FILE *fp = fopen(path, "rb");
    if (fp == NULL) goto err;
    if (fseek(fp, 0, SEEK_END) == -1) goto err;
    long size = ftell(fp);
    if (size < 0 || fseek(fp, 0, SEEK_SET) == -1) goto err;
    if ((buf = malloc((size_t)size + 1)) == NULL) goto err;
    if (fread(buf, 1, (size_t)size, fp) != (size_t)size) goto err;
    buf[size] = '\0';
    fclose(fp);
    *len = (size_t)size;
    return buf;

err:
    /* fp and buf start as NULL, so this is safe whichever step failed. */
    free(buf);
    if (fp) fclose(fp);
    return NULL;
}
```
