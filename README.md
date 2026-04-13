# PDFium (Thread-Safe Fork)

Thread-safe PDFium for cross-document concurrency. Drop-in replacement — no code changes needed.

## Quick Start

Download the prebuilt binary for your platform from [Releases](../../releases), then replace your wrapper's bundled PDFium library:

**Python (pypdfium2):**
```bash
# Find your pypdfium2 install location
python -c "import pypdfium2_raw; print(pypdfium2_raw.__file__)"

# Linux
cp libpdfium.so /path/to/site-packages/pypdfium2_raw/pdfium.so

# macOS
cp libpdfium.dylib /path/to/site-packages/pypdfium2_raw/pdfium.dylib

# Windows
copy pdfium.dll \path\to\site-packages\pypdfium2_raw\pdfium.dll
```

**Rust (pdfium-render):**
```rust
let pdfium = Pdfium::new(
    Pdfium::bind_to_library("./libpdfium.so")
        .or_else(|_| Pdfium::bind_to_system_library())?
);
// thread_safe feature can now be disabled — PDFium itself is safe
```

**Go (go-pdfium):**
```bash
# Place libpdfium.so in your library path
export LD_LIBRARY_PATH=/path/to/libpdfium:$LD_LIBRARY_PATH
# Use single_threaded implementation instead of multi_threaded — no process isolation needed
```

**C/C++ (libvips, direct embedding):**
```bash
# Replace system or bundled libpdfium.so
cp libpdfium.so /usr/local/lib/
ldconfig
```

After replacing, all cross-document operations (open, load pages, render, close) are fully concurrent with no external locks needed.

## Thread Safety (Fork Modification)

Upstream PDFium explicitly states that none of its APIs are thread-safe. This is
by design — Chrome and Android isolate PDFium in sandboxed processes, so
thread-safety at the library level is unnecessary for those embedders.

However, language bindings (Python's pypdfium2, Go's go-pdfium, Rust's
pdfium-render, .NET's PdfiumViewer, etc.) run PDFium in-process. In these
environments, processing multiple PDF documents concurrently — even with
completely separate `FPDF_DOCUMENT` handles — crashes due to unsynchronized
access to global mutable state throughout the library.

### What was changed

This fork adds thread-safety for **cross-document concurrency** — different
`FPDF_DOCUMENT` instances can be fully used from different threads with no
external locks. Four commits, ~200 lines of C++:

#### Commit 1: Global font singleton mutexes

| Class | File | Protected State |
|---|---|---|
| `CPDF_FontGlobals` | `core/fpdfapi/font/cpdf_fontglobals.{h,cpp}` | `cmaps_`, `stock_map_`, `cid2unicode_maps_` |
| `CFX_FontCache` | `core/fxge/cfx_fontcache.{h,cpp}` | `glyph_cache_map_`, `ext_glyph_cache_map_` |
| `CFX_FontMgr` | `core/fxge/cfx_fontmgr.{h,cpp}` | `face_map_`, `ttc_face_map_` |

#### Commit 2: GlyphCache mutex + thread-local error state

| Class | File | Protected State |
|---|---|---|
| `CFX_GlyphCache` | `core/fxge/cfx_glyphcache.{h,cpp}` | `size_map_`, `path_map_`, `width_map_`, `typeface_` |

Also made `g_last_error` in `core/fxcrt/fx_system.cpp` `thread_local`.

#### Commit 3: Comprehensive per-face thread safety

- **Per-face `recursive_mutex`** on `CFX_Face` — locks every method that calls
  FreeType functions (`RenderGlyph`, `LoadGlyphPath`, `GetGlyphWidth`, etc.).
  Recursive because some methods call other locked methods internally.
- **FT_Library face lifecycle mutex** — global mutex protecting `FT_New_Memory_Face`
  and `FT_Done_Face`, which modify the parent `FT_Library`.
- **`CFX_FontMapper` `recursive_mutex`** — protects `FindSubstFont()` which has
  shared mutable state and a recursive fallback path.
- **`CFX_Font` / `CPDF_SimpleFont` face lock usage** — protects direct
  `FT_Face` access via `GetRec()` in `GetGlyphBBox`, `GetPsName`, `LoadCharMetrics`.
- **Thread-local `g_CurrentRecursionDepth`** in `cpdf_renderstatus.cpp`.

#### Commit 4: Full cross-document thread safety

- **Atomic reference counting** — changed `Retainable::ref_count_` from
  `uintptr_t` to `std::atomic<uintptr_t>` with `fetch_add(relaxed)` /
  `fetch_sub(acq_rel)`. Fixes double-free when multiple threads hold
  `RetainPtr<CFX_Face>` to the same cached face.
- **Thread-safe `Observable`** — added mutex to `Observable::observers_` set.
  `NotifyObservers()` swaps to a local copy to avoid holding the lock during
  callbacks. Fixes corruption in `CFX_FontMgr::face_map_` which uses `ObservedPtr`.
- **Thread-local parser recursion depth** — changed
  `CPDF_SyntaxParser::s_CurrentRecursionDepth` from `static int` to
  `static thread_local int`. Fixes incorrect depth tracking when multiple
  threads parse PDFs concurrently.

### What is safe (cross-document)

All operations on separate `FPDF_DOCUMENT` instances are fully thread-safe
with **no external locks required**:

| Operation | Safe? | Mechanism |
|---|---|---|
| `FPDF_LoadDocument` | Yes | Independent CPDF_Document; thread-local parser depth |
| `FPDF_LoadPage` | Yes | Per-face mutex + atomic refcount + thread-safe Observable |
| `FPDF_RenderPageBitmap` | Yes | Per-face mutex + GlyphCache mutex + FontMapper mutex |
| `FPDF_ClosePage` | Yes | Face lifecycle mutex + atomic refcount + thread-safe Observable |
| `FPDF_CloseDocument` | Yes | Face lifecycle mutex + atomic refcount + thread-safe Observable |

### What is NOT safe

- `FPDF_InitLibrary()` / `FPDF_DestroyLibrary()` — call from a single thread
  before/after all other activity
- Concurrent access to the **same** `FPDF_DOCUMENT` or `FPDF_PAGE` handle from
  multiple threads — `CPDF_Document` internal state (page tree, object cache,
  cross-reference table) has unprotected shared mutable state that would require
  a massive refactor to make thread-safe

### Recommended usage pattern

```python
# No locks needed — just use separate documents per thread
from concurrent.futures import ThreadPoolExecutor

def process_pdf(pdf_path):
    doc = open_document(pdf_path)
    for page in doc:
        render(page)    # fully concurrent across documents
        close(page)
    close(doc)

with ThreadPoolExecutor(max_workers=8) as pool:
    pool.map(process_pdf, pdf_paths)
```

### Stress test results

All tests use separate `FPDF_DOCUMENT` instances per thread.

| Test | Docs | Pages/doc | Workers | Runs | Result |
|---|---|---|---|---|---|
| Basic rendering | 4 | 1 | 4 | 1 | PASSED |
| Scaling | 8 | 3 | 8 | 1 | PASSED |
| Stress | 16 | 3 | 8 | 3 | PASSED |
| Heavy lifecycle | 16 | 5 | 16 | 3 | PASSED |
| Max concurrency | 16 | 5 | 32 | 3 | PASSED |

Same-document concurrent pages: CRASH (SIGSEGV) — confirms the architectural
limitation described above.

### Build

```bash
# PDFium requires depot_tools and a gclient sync (see upstream instructions below)

# Configure: component build required for proper FPDF_EXPORT symbols
# In out/Shared/args.gn:
#   is_component_build = true
#   pdf_is_standalone = true
#   is_debug = false

# Build
PATH="$HOME/depot_tools:$PATH" ninja -C out/Shared pdfium

# Link all .o files into a single self-contained .so
find out/Shared/obj -name '*.o' > /tmp/pdfium_objects.txt
g++ -shared -o out/Shared/libpdfium_single.so @/tmp/pdfium_objects.txt \
    -Wl,--allow-multiple-definition -lpthread -ldl -lm

# Install (example: replace pypdfium2's bundled library)
cp out/Shared/libpdfium_single.so \
   ~/.local/lib/python3.10/site-packages/pypdfium2_raw/pdfium.so
```

### Performance impact

Uncontended mutex acquisition is ~25ns on modern x86 — negligible compared to
the milliseconds spent in actual rendering. Atomic reference counting uses
relaxed/acq_rel ordering, which compiles to plain increments on x86. For
single-threaded workloads there is no measurable overhead.

---

## Prerequisites

PDFium uses the same build tooling as Chromium. See the platform-specific
Chromium build instructions to get started, but replace Chromium's
"Get the code" instructions with [PDFium's](#get-the-code).

*   [Chromium Linux build instructions](https://chromium.googlesource.com/chromium/src/+/main/docs/linux/build_instructions.md)
*   [Chromium Mac build instructions](https://chromium.googlesource.com/chromium/src/+/main/docs/mac_build_instructions.md)
*   [Chromium Windows build instructions](https://chromium.googlesource.com/chromium/src/+/main/docs/windows_build_instructions.md)

### CPU Architectures supported

The default architecture for Windows, Linux, and Mac is "`x64`". On Windows,
"`x86`" is also supported. GN parameter "`target_cpu = "x86"`" can be used to
override the default value. If you specify Android build, the default CPU
architecture will be "`arm`".

It is expected that there are still some places lurking in the code which will
not function properly on big-endian architectures. Bugs and/or patches are
welcome, however providing this support is **not** a priority at this time.

### Compilers supported

PDFium aims to be compliant with the [Chromium policy](https://chromium.googlesource.com/chromium/src/+/main/docs/toolchain_support.md#existing-toolchain-support).

Currently this means Clang. Former MSVC users should consider using clang-cl
if needed. Community-contributed patches for gcc will be allowed. No MSVC
patches will be taken.

#### Google employees

Run: `download_from_google_storage --config` and follow the
authentication instructions. **Note that you must authenticate with your
@google.com credentials**. Enter "0" if asked for a project-id.

Once you've done this, the toolchain will be installed automatically for
you in the [Generate the build files](#generate-the-build-files) step below.

The toolchain will be in `depot_tools\win_toolchain\vs_files\<hash>`, and
windbg can be found in
`depot_tools\win_toolchain\vs_files\<hash>\win_sdk\Debuggers`.

If you want the IDE for debugging and editing, you will need to install
it separately, but this is optional and not needed for building PDFium.

## Get the code

The name of the top-level directory does not matter. In the following example,
the directory name is "repo". This directory must not have been used before by
`gclient config` as each directory can only house a single gclient
configuration.

```
mkdir repo
cd repo
gclient config --unmanaged https://pdfium.googlesource.com/pdfium.git
gclient sync
cd pdfium
```

On Linux, additional build dependencies need to be installed by running the
following from the `pdfium` directory.

```
./build/install-build-deps.sh
```

## Generate the build files

PDFium uses GN to generate the build files and [Ninja](https://ninja-build.org/)
to execute the build files.  Both of these are included with the
depot\_tools checkout.

### Selecting build configuration

PDFium may be built either with or without JavaScript support, and with
or without XFA forms support.  Both of these features are enabled by
default. Also note that the XFA feature requires JavaScript.

Configuration is done by executing `gn args <directory>` to configure the build.
This will launch an editor in which you can set the following arguments.
By convention, `<directory>` should be named `out/foo`, and some tools / test
support code only works if one follows this convention.
A typical `<directory>` name is `out/Debug`.

```
use_remoteexec = false # Approved users only.  Do necessary setup & authentication first.
is_debug = true  # Enable debugging features.

# Set true to enable experimental Skia backend.
pdf_use_skia = false

# Set true to enable experimental Fontations backend.
pdf_enable_fontations = false

pdf_enable_xfa = true  # Set false to remove XFA support (implies JS support).
pdf_enable_v8 = true  # Set false to remove Javascript support.
pdf_is_standalone = true  # Set for a non-embedded build.
is_component_build = false # Disable component build (Though it should work)
```

For test applications like `pdfium_test` to build, one must set
`pdf_is_standalone = true`.

By default, the entire project builds with C++20.

By default, PDFium expects to build with a clang compiler that provides
additional chrome plugins. To build against a vanilla one lacking these,
one must set
`clang_use_chrome_plugins = false`.

When complete the arguments will be stored in `<directory>/args.gn`, and
GN will automatically use the new arguments to generate build files.
Should your files fail to generate, please double-check that you have set
use\_sysroot as indicated above.

## Building the code

You can build the standalone test program by running:
`ninja -C <directory> pdfium_test`
You can build the entire product (which includes a few unit tests) by running:
`ninja -C <directory> pdfium_all`

## Running the standalone test program

The pdfium\_test program supports reading, parsing, and rasterizing the pages of
a .pdf file to .ppm or .png output image files (Windows supports two other
formats). For example: `<directory>/pdfium_test --ppm path/to/myfile.pdf`. Note
that this will write output images to `path/to/myfile.pdf.<n>.ppm`.
Run `pdfium_test --help` to see all the options.

## Testing

There are currently several test suites that can be run:

 * pdfium\_unittests
 * pdfium\_embeddertests
 * testing/tools/run\_corpus\_tests.py
 * testing/tools/run\_javascript\_tests.py
 * testing/tools/run\_pixel\_tests.py

It is possible the tests in the `testing` directory can fail due to font
differences on the various platforms. These tests are reliable on the bots. If
you see failures, it can be a good idea to run the tests on the tip-of-tree
checkout to see if the same failures appear.

### Pixel Tests

If your change affects rendering, a pixel test should be added. Simply add a
`.in` or `.pdf` file in `testing/resources/pixel` and the pixel runner will
pick it up at the next run.

Make sure that your test case doesn't have any copyright issues. It should also
be a minimal test case focusing on the bug that renders the same way in many
PDF viewers. Try to avoid binary data in streams by using the `ASCIIHexDecode`
simply because it makes the PDF more readable in a text editor.

To try out your new test, you can call the `run_pixel_tests.py` script:

```bash
$ ./testing/tools/run_pixel_tests.py your_new_file.in
```

To generate the expected image, you can use the `make_expected.sh` script:

```bash
$ ./testing/tools/make_expected.sh your_new_file.pdf
```

Please make sure to have `optipng` installed which optimized the file size of
the resulting png.

### `.in` files

`.in` files are PDF template files. PDF files contain many byte offsets that
have to be kept correct or the file won't be valid. The template makes this
easier by replacing the byte offsets with certain keywords.

This saves space and also allows an easy way to reduce the test case to the
essentials as you can simply remove everything that is not necessary.

A simple example can be found [here](https://pdfium.googlesource.com/pdfium/+/refs/heads/main/testing/resources/rectangles.in).

To transform this into a PDF, you can use the `fixup_pdf_template.py` tool:

```bash
$ ./testing/tools/fixup_pdf_template.py your_file.in
```

This will create a `your_file.pdf` in the same directory as `your_file.in`.

There is no official style guide for the .in file, but a consistent style is
preferred simply to help with readability. If possible, object numbers should
be consecutive and `/Type` and `/SubType` should be on top of a dictionary to
make object identification easier.

## Embedding PDFium in your own projects

The public/ directory contains header files for the APIs available for use by
embedders of PDFium. The PDFium project endeavors to keep these as stable as
possible.

Outside of the public/ directory, code may change at any time, and embedders
should not directly call these routines.

## Code Coverage

Code coverage reports for PDFium can be generated in Linux development
environments. Details can be found [here](/docs/code-coverage.md).

Chromium provides code coverage reports for PDFium
[here](https://chromium-coverage.appspot.com/). PDFium is located in
`third_party/pdfium` in Chromium's source code.
This includes code coverage from PDFium's fuzzers.

## Waterfall

The current health of the source tree can be found
[here](https://ci.chromium.org/p/pdfium/g/main/console).

## Community

There are several mailing lists that are setup:

 * [PDFium](https://groups.google.com/forum/#!forum/pdfium)
 * [PDFium Reviews](https://groups.google.com/forum/#!forum/pdfium-reviews)
 * [PDFium Bugs](https://groups.google.com/forum/#!forum/pdfium-bugs)

Note, the Reviews and Bugs lists are typically read-only.

## Bugs

PDFium uses this [bug tracker](https://crbug.com/pdfium/new), but for security
bugs, please use
[Chromium's security bug template](https://crbug.com/new?component=1586257&noWizard=True&template=1922342).

## Contributing code

See the [CONTRIBUTING](CONTRIBUTING.md) document for more information on
contributing to the PDFium project.
