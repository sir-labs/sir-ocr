"""Generate public-domain acceptance PDFs; no user documents are copied."""
import argparse
from pathlib import Path
import pymupdf as fitz
p=argparse.ArgumentParser();p.add_argument('output',type=Path);p.add_argument('--pages',type=int,default=3)
a=p.parse_args();a.output.parent.mkdir(parents=True,exist_ok=True)
chart=fitz.open();page=chart.new_page(width=480,height=240)
page.insert_text((20,25),'Validation accuracy by epoch',fontsize=15)
for n,h in enumerate([40,80,110,150]):
    x=50+n*100
    page.draw_rect(fitz.Rect(x,210-h,x+45,210),color=(.1,.4,.3),fill=(.2,.6,.45))
    page.insert_text((x,230),str(n+1),fontsize=12)
chart_image=page.get_pixmap().tobytes('png')
d=fitz.open()
for n in range(1,a.pages+1):
    page=d.new_page()
    page.insert_text((50,60),f'SIR OCR ACCEPTANCE - PAGE {n:02d}',fontsize=21)
    page.insert_text((50,105),'Machine learning experiment report',fontsize=16)
    page.insert_text((50,140),f'Page marker: UNIQUE_PAGE_{n:02d}. Preserve numeric page order.',fontsize=12)
    page.insert_text((50,170),'This document is generated for public service acceptance tests.',fontsize=12)
    page.insert_text((50,200),'Loss function: L = (y - prediction)^2',fontsize=14)
    page.insert_image(fitz.Rect(50,250,530,490),stream=chart_image)
    page.insert_text((50,530),'Epoch    Accuracy    Loss',fontsize=14)
    for i in range(4):
        page.insert_text((50,560+i*25),f'{i+1}             {0.6+i*.1:.1f}               {0.8-i*.2:.1f}',fontsize=12)
    page.insert_text((50,720),'End of page. OCR output must include this sentence.',fontsize=12)
d.save(a.output)
print(f'Generated {a.pages} pages: {a.output}')
