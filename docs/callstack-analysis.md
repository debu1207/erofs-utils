# Compression Call Stack Analysis for mkfs.erofs

## Overview

This document describes the function call stack for the `lz4hc_compress_destsize` function in erofs-utils, and compares it across all supported compression algorithms.

---

## 1. Complete Call Stack

The full call chain from `main()` to the library-level compression call:

```
main()                                          [mkfs/main.c:1251]
  │
  ├─ erofs_mkfs_handle_inode()                  [lib/inode.c:1580]
  │   │  Determines whether to compress per file via erofs_file_is_compressible()
  │   │  Opens the source file, begins compression context
  │   │
  │   └─ erofs_begin_compressed_file()           [lib/compress.c]
  │       │  Creates z_erofs_compress_ictx (compression context)
  │       │
  │       └─ erofs_mkfs_go() → job queue (async)
  │           │  Submits work to single-threaded or multi-threaded queue
  │           │
  │           └─ erofs_mkfs_jobfn()              [lib/inode.c:1281]
  │               │  Dispatches based on job type (EROFS_MKFS_JOB_NDIR)
  │               │
  │               └─ erofs_mkfs_handle_nondirectory() [lib/inode.c:1232]
  │                   │  Handles symlinks, device nodes, regular files
  │                   │
  │                   └─ erofs_mkfs_job_write_file() [lib/inode.c:1197]
  │                       │  Calls compress or fallback to uncompressed
  │                       │
  │                       └─ erofs_write_compressed_file() [lib/compress.c:1725]
  │                           │  Allocates data buffer, initializes segment context
  │                           │
  │                           └─ z_erofs_compress_segment() [lib/compress.c:1107]
  │                               │  Reads file data into queue in chunks
  │                               │  Iterates until all data consumed
  │                               │
  │                               └─ z_erofs_compress_one() [lib/compress.c:743]
  │                                   │  Wraps compression decisions
  │                                   │
  │                                   └─ __z_erofs_compress_one() [lib/compress.c:556]
  │                                       │  Core per-extent compression logic
  │                                       │  Handles gain check, inline data,
  │                                       │  fragment packing, fallback to
  │                                       │  uncompressed
  │                                       │
  │                                       └─ erofs_compress_destsize() [lib/compressor.c:77]
  │                                           │  Generic dispatcher:
  │                                           │  c->alg->c->compress_destsize(c, ...)
  │                                           │
  │                                           └─ algorithm-specific function
  │                                              (e.g. lz4hc_compress_destsize)
  │                                                │
  │                                                └─ library call
  │                                                   (e.g. LZ4_compress_HC_destSize)
  │
  └─ erofs_commit_compressed_file()             [lib/compress.c:1178]
      │  Writes compression indexes, computes metadata
      │  Checks if compression actually saved space;
      │  if not, returns -ENOSPC to trigger fallback
      │
      └─ z_erofs_write_indexes()
          └─ erofs_convert_to_compacted_format()  [if needed]
```

---

## 2. The Generic Dispatcher

All algorithms converge at `lib/compressor.c:77-86`:

```c
int erofs_compress_destsize(const struct erofs_compress *c,
                            const void *src, unsigned int *srcsize,
                            void *dst, unsigned int dstsize)
{
    DBG_BUGON(!c->alg);
    if (!c->alg->c->compress_destsize)
        return -EOPNOTSUPP;

    return c->alg->c->compress_destsize(c, src, srcsize, dst, dstsize);
}
```

The `erofs_compressor` struct registered in `compressor.c` holds the function pointer:

```c
struct erofs_compressor {
    int default_level;
    int best_level;
    u32 default_dictsize;
    u32 max_dictsize;

    int (*init)(struct erofs_compress *c);
    int (*exit)(struct erofs_compress *c);
    void (*reset)(struct erofs_compress *c);
    int (*setlevel)(struct erofs_compress *c, int compression_level);
    int (*setdictsize)(struct erofs_compress *c, u32 dict_size);

    int (*compress_destsize)(const struct erofs_compress *c,
                             const void *src, unsigned int *srcsize,
                             void *dst, unsigned int dstsize);
};
```

---

## 3. Per-Algorithm Comparison

### 3.1 LZ4 (`lib/compressor_lz4.c`)

```c
static int lz4_compress_destsize(const struct erofs_compress *c,
                                 const void *src, unsigned int *srcsize,
                                 void *dst, unsigned int dstsize)
{
    int srcSize = (int)*srcsize;
    int rc = LZ4_compress_destSize(src, dst, &srcSize, (int)dstsize);
    if (!rc)
        return -EFAULT;
    *srcsize = srcSize;
    return rc;
}
```

| Property | Value |
|----------|-------|
| `default_level` | N/A |
| `best_level` | N/A |
| `setlevel` | Not provided |
| `init` | No-op (returns 0) |
| Private state | Stateless |
| Strategy | Single-pass via `LZ4_compress_destSize()` |
| Library call | `LZ4_compress_destSize` |

### 3.2 LZ4HC (`lib/compressor_lz4hc.c`)

```c
static int lz4hc_compress_destsize(const struct erofs_compress *c,
                                   const void *src, unsigned int *srcsize,
                                   void *dst, unsigned int dstsize)
{
    int srcSize = (int)*srcsize;
    int rc = LZ4_compress_HC_destSize(c->private_data, src, dst,
                                      &srcSize, (int)dstsize,
                                      c->compression_level);
    if (!rc)
        return -EFAULT;
    *srcsize = srcSize;
    return rc;
}
```

| Property | Value |
|----------|-------|
| `default_level` | `LZ4HC_CLEVEL_DEFAULT` |
| `best_level` | `LZ4HC_CLEVEL_MAX` |
| `setlevel` | Yes; validates range, defaults to `LZ4HC_CLEVEL_DEFAULT` if negative |
| `init` | `LZ4_createStreamHC()` → `c->private_data` |
| Private state | `LZ4_streamHC_t*` (HC streaming context) |
| Strategy | Single-pass via `LZ4_compress_HC_destSize()` |
| Library call | `LZ4_compress_HC_destSize` |

### 3.3 LZMA (`lib/compressor_liblzma.c`)

```c
static int erofs_liblzma_compress_destsize(const struct erofs_compress *c,
                                           const void *src, unsigned int *srcsize,
                                           void *dst, unsigned int dstsize)
{
    struct erofs_liblzma_context *ctx = c->private_data;
    lzma_stream *strm = &ctx->strm;

    lzma_ret ret = lzma_microlzma_encoder(strm, &ctx->opt);
    if (ret != LZMA_OK)
        return -EFAULT;

    strm->next_in = src;
    strm->avail_in = *srcsize;
    strm->next_out = dst;
    strm->avail_out = dstsize;

    ret = lzma_code(strm, LZMA_FINISH);
    if (ret != LZMA_STREAM_END)
        return -EBADMSG;

    *srcsize = strm->total_in;
    return strm->total_out;
}
```

| Property | Value |
|----------|-------|
| `default_level` | `LZMA_PRESET_DEFAULT` |
| `best_level` | 109 |
| `setlevel` | Yes; supports `0-9` (normal) and `100-109` (extreme) |
| `init` | Allocates `erofs_liblzma_context` with LZMA filter config |
| Private state | `erofs_liblzma_context { lzma_stream, lzma_options_lzma, ... }` |
| Strategy | Single-pass: configures microlzma encoder, runs `lzma_code(LZMA_FINISH)` |
| Library call | `lzma_microlzma_encoder` + `lzma_code` |

### 3.4 kite_deflate (`lib/compressor_deflate.c`)

```c
static int deflate_compress_destsize(const struct erofs_compress *c,
                                     const void *src, unsigned int *srcsize,
                                     void *dst, unsigned int dstsize)
{
    int rc = kite_deflate_destsize(c->private_data, src, dst,
                                   srcsize, dstsize);
    if (rc <= 0)
        return -EFAULT;
    return rc;
}
```

| Property | Value |
|----------|-------|
| `default_level` | 1 |
| `best_level` | 9 |
| `setlevel` | Yes; validates range |
| `init` | `kite_deflate_init(level, dict_size)` → `c->private_data` |
| Private state | kite_deflate internal context |
| Strategy | Single-pass via `kite_deflate_destsize()` |
| Library call | `kite_deflate_destsize` (internal erofs implementation) |

### 3.5 libdeflate (`lib/compressor_libdeflate.c`)

```c
static int libdeflate_compress_destsize(const struct erofs_compress *c,
                                        const void *src, unsigned int *srcsize,
                                        void *dst, unsigned int dstsize)
{
    // Binary search: finds largest input that fits in dstsize
    size_t l = 0;          // largest input that fits so far
    size_t l_csize = 0;
    size_t r = *srcsize + 1; // smallest input that doesn't fit so far
    size_t m;

    // ... buffer management ...

    for (;;) {
        csize = libdeflate_deflate_compress(ctx->strm, src, m,
                                            ctx->fitblk_buffer, dstsize + 9);
        if (csize > 0 && csize <= dstsize) {
            memcpy(dst, ctx->fitblk_buffer, csize);
            l = m;  l_csize = csize;
            // estimate next m based on ratio
            m = (dstsize * m) / csize;
        } else {
            r = m;
            m = (l + r) / 2;  // binary search
        }
    }
    *srcsize = l;
    return l_csize;
}
```

| Property | Value |
|----------|-------|
| `default_level` | 1 |
| `best_level` | 12 |
| `setlevel` | Yes; validates range |
| `init` | `libdeflate_alloc_compressor(level)` → `c->private_data` context |
| Private state | `erofs_libdeflate_context { strm, fitblk_buffer, last_uncompressed_size, ... }` |
| Strategy | **Binary search** — iterates to find largest input fitting in `dstsize` |
| Library call | `libdeflate_deflate_compress` (called multiple times per extent) |

### 3.6 libzstd (`lib/compressor_libzstd.c`)

```c
static int libzstd_compress_destsize(const struct erofs_compress *c,
                                     const void *src, unsigned int *srcsize,
                                     void *dst, unsigned int dstsize)
{
    // Binary search: finds largest input that fits in dstsize
    size_t l = 0;
    size_t l_csize = 0;
    size_t r = *srcsize + 1;
    size_t m;

    // ... buffer management ...

    for (;;) {
        csize = ZSTD_compress2(ctx->cctx, ctx->fitblk_buffer,
                               dstsize + 32, src, m);
        if (csize > 0 && csize <= dstsize) {
            memcpy(dst, ctx->fitblk_buffer, csize);
            l = m;  l_csize = csize;
            m = (dstsize * m) / csize;
        } else {
            r = m;
            m = (l + r) / 2;
        }
    }
    *srcsize = l;
    return l_csize;
}
```

| Property | Value |
|----------|-------|
| `default_level` | `ZSTD_CLEVEL_DEFAULT` |
| `best_level` | 22 |
| `setlevel` | Yes; validates range |
| `init` | `ZSTD_createCCtx()` → `c->private_data` context, configures level |
| Private state | `erofs_libzstd_context { cctx, fitblk_buffer, fitblk_bufsiz }` |
| Strategy | **Binary search** — iterates to find largest input fitting in `dstsize` |
| Library call | `ZSTD_compress2` (called multiple times per extent) |

---

## 4. Summary Table

| Aspect | LZ4 | LZ4HC | LZMA | kite_deflate | libdeflate | libzstd |
|--------|-----|-------|------|-------------|------------|---------|
| **Dispatch** | Same path via `c->alg->c->compress_destsize()` | Same | Same | Same | Same | Same |
| **Streaming state** | None | `LZ4_streamHC_t*` | `lzma_stream` | kite context | `libdeflate_compressor*` + buffer | `ZSTD_CCtx*` + buffer |
| **Input sizing** | Single-pass (`LZ4_compress_destSize`) | Single-pass (`LZ4_compress_HC_destSize`) | Single-pass (`lzma_code`) | Single-pass (`kite_deflate_destsize`) | **Binary search** | **Binary search** |
| **`setlevel`** | No | Yes | Yes | Yes | Yes | Yes |
| **`setdictsize`** | No | No | Yes | Yes | No | No |
| **Post-compression gain check** | Same for all: `ret * compress_threshold / 100 >= e->length` at `lib/compress.c:609` | | | | | |

### The call stack convergence point is always `lib/compressor.c:85`:

```
__z_erofs_compress_one()
  └─ erofs_compress_destsize()        ← ALL algorithms converge here
       └─ c->alg->c->compress_destsize()
            └─ algorithm-specific function
                 └─ native library call
```

Everything above `erofs_compress_destsize()` is **identical** for all algorithms — same segment loop, same extent logic, same gain check, same fallback to uncompressed.
