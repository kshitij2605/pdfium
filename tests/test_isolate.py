"""Isolate which operation is unsafe: load, render, or both."""
import faulthandler
faulthandler.enable()
import pypdfium2 as pdfium
from concurrent.futures import ThreadPoolExecutor
import threading
import time

PDF_PATH = '/home/mac/25gitlab/marker_preprocessor/pdf/pdfsmall.pdf'

# ---- Test 1: Serial load, concurrent render ----
def test_serial_load_concurrent_render():
    print("Test 1: Serial load, concurrent render...", flush=True)
    docs = []
    for i in range(8):
        doc = pdfium.PdfDocument(PDF_PATH)
        pages = [doc[pi] for pi in range(len(doc))]
        docs.append((doc, pages))

    def render_work(doc_pages):
        doc, pages = doc_pages
        for _ in range(3):
            for page in pages:
                bmp = page.render(scale=1.0)
                img = bmp.to_pil()
        return True

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(render_work, dp) for dp in docs]
        for f in futs:
            f.result()

    for doc, pages in docs:
        for p in pages:
            p.close()
        doc.close()

    print("  PASSED", flush=True)


# ---- Test 2: Concurrent load only (no render) ----
def test_concurrent_load_only():
    print("Test 2: Concurrent load only (no render)...", flush=True)

    def load_work(i):
        for _ in range(10):
            doc = pdfium.PdfDocument(PDF_PATH)
            for pi in range(len(doc)):
                page = doc[pi]
                page.close()
            doc.close()
        return True

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(load_work, i) for i in range(8)]
        for f in futs:
            f.result()

    print("  PASSED", flush=True)


# ---- Test 3: Concurrent load + render ----
def test_concurrent_load_and_render():
    print("Test 3: Concurrent load + render...", flush=True)

    def full_work(i):
        for _ in range(3):
            doc = pdfium.PdfDocument(PDF_PATH)
            for pi in range(len(doc)):
                page = doc[pi]
                bmp = page.render(scale=1.0)
                img = bmp.to_pil()
                page.close()
            doc.close()
        return True

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(full_work, i) for i in range(8)]
        for f in futs:
            f.result()

    print("  PASSED", flush=True)


if __name__ == "__main__":
    print(f"pdfium: {pdfium.PDFIUM_INFO}", flush=True)

    test_serial_load_concurrent_render()
    test_concurrent_load_only()
    test_concurrent_load_and_render()

    print("\nAll tests passed!", flush=True)
