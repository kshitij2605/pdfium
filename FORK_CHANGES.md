# PDFium Fork Changes — Thread-Safety for Concurrent Rendering

## Why This Fork Exists

Google's PDFium C library is **not thread-safe** for concurrent page rendering. When multiple threads render PDF pages simultaneously, the library crashes with segfaults due to unprotected shared mutable state in glyph caching and FreeType face access.

This fork adds minimal mutex protection to enable safe concurrent rendering from multiple threads within a single process. The changes follow the same `std::mutex` + `std::lock_guard` pattern already used by three other classes in the codebase (CPDF_FontGlobals, CFX_FontCache, CFX_FontMgr).

## Changes

### 1. CFX_GlyphCache — Mutex-protect glyph/path/width caches

**Files:** `core/fxge/cfx_glyphcache.h`, `core/fxge/cfx_glyphcache.cpp`

**Problem:** `CFX_GlyphCache` instances are shared across threads (returned from the already-mutex-protected `CFX_FontCache`), but the cache's own mutable maps had no synchronization:

```cpp
std::map<ByteString, SizeGlyphCache> size_map_;
std::map<PathMapKey, std::unique_ptr<CFX_Path>> path_map_;
std::map<WidthMapKey, int> width_map_;
```

When two threads render pages using the same font, they share a `CFX_GlyphCache` instance and race on these maps. The cache's methods also call `FT_Load_Glyph` / `FT_Render_Glyph` which modify FreeType's per-face glyph slot — also not thread-safe.

**Fix:** Added `mutable std::mutex mutex_` to the class and `std::lock_guard<std::mutex> lock(mutex_)` at the entry of each public method:

- `LoadGlyphBitmap()` — bitmap cache + FreeType rendering
- `LoadGlyphPath()` — path cache + FreeType glyph loading
- `GetGlyphWidth()` — width cache + FreeType glyph loading
- `GetDeviceCache()` — Skia typeface creation (PDF_USE_SKIA only)

The per-instance mutex serializes all glyph operations on a given font face, which is exactly what FreeType requires (per-face serialization). Different fonts/faces use different `CFX_GlyphCache` instances and don't contend.

### 2. g_last_error — Make thread-local

**File:** `core/fxcrt/fx_system.cpp`

**Problem:** `g_last_error` is a plain global variable used by `FXSYS_SetLastError` / `FXSYS_GetLastError` on non-Windows platforms. Concurrent threads clobber each other's error codes.

**Fix:** Changed from `uint32_t g_last_error = 0` to `thread_local uint32_t g_last_error = 0`.

### 3. Previously applied changes (not in this commit)

These were applied in earlier commits to the fork:

| Class | File | What was protected |
|---|---|---|
| `CPDF_FontGlobals` | `core/fpdfapi/font/cpdf_fontglobals.cpp` | `stock_map_`, `cmaps_`, `cid2unicode_maps_` |
| `CFX_FontCache` | `core/fxge/cfx_fontcache.h/.cpp` | `glyph_cache_map_`, `ext_glyph_cache_map_` |
| `CFX_FontMgr` | `core/fxge/cfx_fontmgr.h/.cpp` | `face_map_`, `ttc_face_map_` |

## Complete Diff

```diff
diff --git a/core/fxcrt/fx_system.cpp b/core/fxcrt/fx_system.cpp
--- a/core/fxcrt/fx_system.cpp
+++ b/core/fxcrt/fx_system.cpp
@@ -17,7 +17,7 @@
 namespace {

 #if !BUILDFLAG(IS_WIN)
-uint32_t g_last_error = 0;
+thread_local uint32_t g_last_error = 0;
 #endif

diff --git a/core/fxge/cfx_glyphcache.h b/core/fxge/cfx_glyphcache.h
--- a/core/fxge/cfx_glyphcache.h
+++ b/core/fxge/cfx_glyphcache.h
@@ -9,6 +9,7 @@

 #include <map>
 #include <memory>
+#include <mutex>
 #include <tuple>

@@ -84,6 +85,7 @@
                                      int anti_alias);
   RetainPtr<CFX_Face> const face_;
+  mutable std::mutex mutex_;
   std::map<ByteString, SizeGlyphCache> size_map_;

diff --git a/core/fxge/cfx_glyphcache.cpp b/core/fxge/cfx_glyphcache.cpp
--- a/core/fxge/cfx_glyphcache.cpp
+++ b/core/fxge/cfx_glyphcache.cpp
@@ -8,6 +8,7 @@
 #include <initializer_list>
 #include <memory>
+#include <mutex>
 #include <utility>

 // Added std::lock_guard<std::mutex> lock(mutex_) to:
 // - LoadGlyphPath()     (protects path_map_)
 // - LoadGlyphBitmap()   (protects size_map_ via LookUpGlyphBitmap)
 // - GetGlyphWidth()     (protects width_map_)
 // - GetDeviceCache()    (protects typeface_, Skia only)
```

## Thread-Safety Constraints

After these patches, the following operations are safe for concurrent use:

| Operation | Thread-safe? | Notes |
|---|---|---|
| `FPDF_RenderPageBitmap` | Yes | CFX_GlyphCache mutex serializes per-face |
| `FPDF_LoadDocument` | Partially | Fails at 8+ concurrent opens; best to serialize |
| `FPDF_LoadPage` | No | Must be serialized (global parser state) |
| `FPDF_ClosePage` / `FPDF_CloseDocument` | No | Must be serialized |

**Recommended usage pattern:**

```
1. Serialize: Open document + load all pages
2. Concurrent: Render pages (the expensive part — now thread-safe)
3. Serialize: Close pages + close document
```

## Benchmark Results

**Test PDF:** 70-page Japanese corporate report, 200 DPI rendering
**Hardware:** 48-core CPU, 93 GiB RAM

| Scenario | Pages | Time | Throughput |
|---|---|---|---|
| Serial rendering (baseline) | 30 | 4.96s | 6 pages/sec |
| 8-thread concurrent rendering | 30 | 1.00s | 30 pages/sec |
| 4 concurrent PDFs x 30 pages | 120 | 1.82s | 66 pages/sec |
| 5 concurrent PDFs x 70 pages | 350 | 3.50s | 100 pages/sec |
| Stress: 10 rounds x 5 PDFs x 70 pages | 3,500 | 37.7s | 93 pages/sec |

All stress tests completed with **zero crashes**.

## Build

```bash
PATH="$HOME/depot_tools:$PATH" ninja -C out/Release pdfium
```

Output: `out/Release/libpdfium.so` (plus shared library dependencies in the same directory).

## Install

```bash
# Replace pypdfium2's bundled library
cp out/Release/libpdfium.so \
   ~/.local/lib/python3.10/site-packages/pypdfium2_raw/pdfium.so

# Set LD_LIBRARY_PATH for shared library dependencies at runtime
export LD_LIBRARY_PATH=/path/to/pdfium/out/Release
```

Note: This fork is based on an older PDFium version than pypdfium2's bundled binary. Two symbols (`FPDFCatalog_GetLanguage`, `FPDF_StructElement_GetExpansion`) are absent but unused in rendering paths.
