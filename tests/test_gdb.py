"""Minimal concurrent test for GDB debugging."""
import faulthandler
faulthandler.enable()
import pypdfium2 as pdfium
from concurrent.futures import ThreadPoolExecutor
import threading

PDF_PATH = '/home/mac/25gitlab/marker_preprocessor/pdf/pdfsmall.pdf'

def work(i):
    for _ in range(5):
        doc = pdfium.PdfDocument(PDF_PATH)
        for pi in range(len(doc)):
            page = doc[pi]
            bmp = page.render(scale=1.0)
            img = bmp.to_pil()
            page.close()
        doc.close()
    return i

print(f'Starting 4 workers...', flush=True)
with ThreadPoolExecutor(max_workers=4) as ex:
    futs = [ex.submit(work, i) for i in range(4)]
    for f in futs:
        print(f'  Worker {f.result()} done', flush=True)
print('Done', flush=True)
