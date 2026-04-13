"""
Concurrent PDFium thread-safety test.
Tests concurrent FPDF_LoadDocument + FPDF_LoadPage + FPDF_RenderPageBitmap + FPDF_ClosePage + FPDF_CloseDocument.
"""
import sys
import os
import time
import threading
import traceback
import faulthandler
from concurrent.futures import ThreadPoolExecutor, as_completed

faulthandler.enable()

import pypdfium2 as pdfium

PDF_PATH = "/home/mac/25gitlab/marker_preprocessor/pdf/pdfsmall.pdf"

def process_pdf(worker_id: int, pdf_path: str, num_iterations: int = 3):
    """Open, render all pages, close — repeated num_iterations times."""
    errors = []
    for iteration in range(num_iterations):
        try:
            doc = pdfium.PdfDocument(pdf_path)
            n_pages = len(doc)
            for page_idx in range(n_pages):
                page = doc[page_idx]
                bitmap = page.render(scale=1.0)
                pil_image = bitmap.to_pil()
                _ = pil_image.size
                page.close()
            doc.close()
        except Exception as e:
            errors.append(f"Worker {worker_id}, iter {iteration}: {type(e).__name__}: {e}")
            traceback.print_exc()
    return errors


def run_test(num_workers: int, iterations_per_worker: int = 5):
    print(f"\n--- {num_workers} workers, {iterations_per_worker} iters ---", flush=True)

    start = time.time()
    all_errors = []

    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {
            executor.submit(process_pdf, i, PDF_PATH, iterations_per_worker): i
            for i in range(num_workers)
        }
        for future in as_completed(futures):
            worker_id = futures[future]
            try:
                errors = future.result()
                if errors:
                    all_errors.extend(errors)
            except Exception as e:
                all_errors.append(f"Worker {worker_id}: CRASHED: {e}")

    elapsed = time.time() - start
    status = "FAIL" if all_errors else "OK"
    print(f"  {status} ({elapsed:.1f}s, {len(all_errors)} errors)", flush=True)
    return len(all_errors) == 0


def main():
    if not os.path.exists(PDF_PATH):
        print(f"PDF not found: {PDF_PATH}")
        sys.exit(1)

    print(f"PDF: {PDF_PATH}")
    print(f"pdfium: {pdfium.PDFIUM_INFO}", flush=True)

    # Warm up
    doc = pdfium.PdfDocument(PDF_PATH)
    print(f"Pages: {len(doc)}", flush=True)
    doc.close()

    for n_workers in [2, 4, 8]:
        run_test(n_workers, iterations_per_worker=3)

    print("\nDone.", flush=True)


if __name__ == "__main__":
    main()
