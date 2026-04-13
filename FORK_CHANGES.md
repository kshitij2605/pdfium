# PDFium Fork Changes — Thread-Safety for Cross-Document Concurrency

## Why This Fork Exists

Google's PDFium C library is **not thread-safe**. When multiple threads process separate PDF documents concurrently — even with completely separate `FPDF_DOCUMENT` handles — the library crashes with segfaults, double-frees, and heap corruption.

This is by design: Chrome and Android isolate PDFium in sandboxed processes. But language bindings (Python's pypdfium2, Go's go-pdfium, Rust's pdfium-render, .NET's PdfiumViewer, C's libvips) run PDFium in-process. Every one of these projects works around the limitation with global locks, multiprocessing, or WASM isolation.

This fork adds ~200 lines of C++ to make PDFium **fully thread-safe for cross-document concurrency** — different `FPDF_DOCUMENT` instances can be used from different threads simultaneously with no external locks.

## Root Causes

Through systematic isolation testing and C++ source auditing, we identified multiple layers of unprotected shared mutable state:

1. **FreeType face operations** — `CFX_Face` wraps `FT_Face` but had no per-face synchronization. Shared font faces are accessed by multiple threads during rendering.
2. **Glyph caches** — `CFX_GlyphCache` instances are shared across threads but their mutable maps (bitmap, path, width) had no synchronization.
3. **Global font singletons** — `CPDF_FontGlobals`, `CFX_FontCache`, `CFX_FontMgr` have shared maps accessed during font lookup.
4. **Font mapper** — `CFX_FontMapper::FindSubstFont()` has shared mutable state and a recursive fallback path.
5. **Non-atomic reference counting** — `Retainable::ref_count_` was a plain `uintptr_t`. When multiple threads hold `RetainPtr<CFX_Face>` to the same cached face, concurrent `Retain()`/`Release()` corrupted the counter causing double-frees.
6. **Unprotected Observable** — `CFX_FontMgr::face_map_` uses `ObservedPtr<FontDesc>` which calls `AddObserver`/`RemoveObserver`. Concurrent operations corrupted the unprotected `std::set`.
7. **Shared static parser state** — `CPDF_SyntaxParser::s_CurrentRecursionDepth` is a `static int` shared across all threads, causing incorrect depth tracking.
8. **Global error state** — `g_last_error` was a plain global variable clobbered by concurrent threads.

## Changes (4 commits)

### Commit 1: `48d9b94fa` — Global font singleton mutexes

Added `std::mutex` to three global font singletons:

| Class | File | Protected State |
|---|---|---|
| `CPDF_FontGlobals` | `core/fpdfapi/font/cpdf_fontglobals.{h,cpp}` | `cmaps_`, `stock_map_`, `cid2unicode_maps_` |
| `CFX_FontCache` | `core/fxge/cfx_fontcache.{h,cpp}` | `glyph_cache_map_`, `ext_glyph_cache_map_` |
| `CFX_FontMgr` | `core/fxge/cfx_fontmgr.{h,cpp}` | `face_map_`, `ttc_face_map_` |

### Commit 2: `d7a8d2a18` — GlyphCache mutex + thread-local error state

| Change | File |
|---|---|
| `mutable std::mutex mutex_` on `CFX_GlyphCache` | `core/fxge/cfx_glyphcache.{h,cpp}` |
| `thread_local` on `g_last_error` | `core/fxcrt/fx_system.cpp` |

### Commit 3: `81f9875b1` — Comprehensive per-face thread safety

| Change | File(s) |
|---|---|
| Per-face `recursive_mutex` on `CFX_Face` — locks every FreeType-calling method | `core/fxge/cfx_face.{h,cpp}` |
| FT_Library face lifecycle mutex (protects `FT_New_Memory_Face` / `FT_Done_Face`) | `core/fxge/freetype/fx_freetype.{h,cpp}` |
| `recursive_mutex` on `CFX_FontMapper::FindSubstFont()` | `core/fxge/cfx_fontmapper.{h,cpp}` |
| Face lock usage in `CFX_Font::GetGlyphBBox` / `GetPsName` | `core/fxge/cfx_font.cpp` |
| Face lock usage in `CPDF_SimpleFont::LoadCharMetrics` | `core/fpdfapi/font/cpdf_simplefont.cpp` |
| `thread_local` on `g_CurrentRecursionDepth` | `core/fpdfapi/render/cpdf_renderstatus.cpp` |

### Commit 4: `6781cbb56` — Full cross-document thread safety

| Change | File(s) |
|---|---|
| Atomic `ref_count_`: `std::atomic<uintptr_t>`, `fetch_add(relaxed)` / `fetch_sub(acq_rel)` | `core/fxcrt/retain_ptr.h` |
| Thread-safe `Observable`: mutex on `observers_` set, swap-before-iterate in `NotifyObservers()` | `core/fxcrt/observed_ptr.{h,cpp}` |
| `thread_local` on `s_CurrentRecursionDepth` | `core/fpdfapi/parser/cpdf_syntax_parser.{h,cpp}` |

## Thread-Safety Model

### Fully safe (no external locks needed)

| Operation | Mechanism |
|---|---|
| `FPDF_LoadDocument` | Independent CPDF_Document; thread-local parser depth |
| `FPDF_LoadPage` | Per-face mutex + atomic refcount + thread-safe Observable |
| `FPDF_RenderPageBitmap` | Per-face mutex + GlyphCache mutex + FontMapper mutex |
| `FPDF_ClosePage` | Face lifecycle mutex + atomic refcount + thread-safe Observable |
| `FPDF_CloseDocument` | Face lifecycle mutex + atomic refcount + thread-safe Observable |

### NOT safe (architectural limitation)

- `FPDF_InitLibrary()` / `FPDF_DestroyLibrary()` — call once from a single thread
- Concurrent access to the **same** `FPDF_DOCUMENT` from multiple threads — `CPDF_Document` internal state (page tree, object cache, cross-reference table) has unprotected shared mutable state that would require a massive refactor

## Stress Test Results

All tests use separate `FPDF_DOCUMENT` instances per thread (cross-document concurrency).

| Test | Docs | Pages/doc | Workers | Runs | Result |
|---|---|---|---|---|---|
| Basic rendering | 4 | 1 | 4 | 1 | PASSED |
| Scaling | 8 | 3 | 8 | 1 | PASSED |
| Stress | 16 | 3 | 8 | 3 | PASSED |
| Heavy lifecycle | 16 | 5 | 16 | 3 | PASSED |
| Max concurrency | 16 | 5 | 32 | 3 | PASSED |

Same-document concurrent pages (1 doc, 4 pages, 4 workers): **CRASH (SIGSEGV)** — confirms the architectural limitation.

Test scripts are in `tests/`.

## Build

```bash
# PDFium requires depot_tools and gclient sync (see upstream README)

# Configure (out/Shared/args.gn):
#   is_component_build = true
#   pdf_is_standalone = true
#   is_debug = false

# Build
PATH="$HOME/depot_tools:$PATH" ninja -C out/Shared pdfium

# Link into a single self-contained .so
find out/Shared/obj -name '*.o' > /tmp/pdfium_objects.txt
g++ -shared -o out/Shared/libpdfium_single.so @/tmp/pdfium_objects.txt \
    -Wl,--allow-multiple-definition -lpthread -ldl -lm
```

Note: `is_component_build = true` is required for proper `FPDF_EXPORT` symbol visibility. The re-link step produces a single `.so` with no runtime dependencies on other PDFium component libraries.

## Install

```bash
# Replace pypdfium2's bundled library (example for Python)
cp out/Shared/libpdfium_single.so \
   ~/.local/lib/python3.10/site-packages/pypdfium2_raw/pdfium.so

# No LD_LIBRARY_PATH needed — single self-contained .so
```

This fork is based on an older PDFium version than pypdfium2's bundled binary. Two symbols (`FPDFCatalog_GetLanguage`, `FPDF_StructElement_GetExpansion`) are absent but unused in rendering paths. pypdfium2's reference bindings handle this gracefully with `if hasattr()` guards.

## Performance Impact

Uncontended mutex acquisition is ~25ns on modern x86 — negligible compared to the milliseconds spent in actual rendering. Atomic reference counting uses relaxed/acq_rel ordering, which compiles to plain increments on x86. For single-threaded workloads there is no measurable overhead.
