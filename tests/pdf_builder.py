"""Tiny, dependency-free PDFs laid out like the first page of a paper."""


def _escape(text):
    return text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')


def make_pdf(runs, info=None, pages=1, padding=0):
    """runs: (text, size, x, y[, rotated]) on page 1; info: document /Info entries."""
    content = []
    for run in runs:
        text, size, x, y = run[:4]
        matrix = f'0 1 -1 0 {x} {y}' if len(run) > 4 and run[4] else f'1 0 0 1 {x} {y}'
        content.append(f'BT /F1 {size} Tf {matrix} Tm ({_escape(text)}) Tj ET')
    streams = ['\n'.join(content)] + [f'BT /F1 10 Tf 1 0 0 1 72 720 Tm (Page {n} body text.) Tj ET' for n in range(2, pages+1)]
    objects = ['<< /Type /Catalog /Pages 2 0 R >>', None, '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    kids = []
    for stream in streams:
        data = stream.encode('latin-1')
        objects.append(f'<< /Length {len(data)} >>\nstream\n'+stream+'\nendstream')
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {len(objects)} 0 R >>')
        kids.append(f'{len(objects)} 0 R')
    objects[1] = f'<< /Type /Pages /Kids [{" ".join(kids)}] /Count {len(kids)} >>'
    trailer_info = ''
    if info:
        objects.append('<< '+' '.join(f'/{key} ({_escape(value)})' for key, value in info.items())+' >>')
        trailer_info = f' /Info {len(objects)} 0 R'
    output = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
    if padding: output.extend(b'%'+b'x'*padding+b'\n')
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f'{number} 0 obj\n{obj}\nendobj\n'.encode('latin-1'))
    xref = len(output)
    output.extend(f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode())
    for offset in offsets: output.extend(f'{offset:010d} 00000 n \n'.encode())
    output.extend(f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R{trailer_info} >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return bytes(output)


TITLE = 'Calibrating Uncertainty in Small Image Classifiers'


def paper_pdf(title=TITLE, arxiv=None, doi_line=None, info=None, **extra):
    runs = [(title, 17, 72, 700),
            ('Ada Researcher and Grace Example', 11, 72, 670),
            ('Abstract', 12, 72, 620),
            ('We calibrate the confidence of small image classifiers with a held-out set.', 10, 72, 600),
            ('1 Introduction', 12, 72, 560),
            ('Classifiers are often overconfident.', 10, 72, 540)]
    if arxiv: runs.append((f'arXiv:{arxiv}  [cs.LG]  2 Jan 2024', 20, 30, 250, True))
    if doi_line: runs.append((doi_line, 8, 72, 80))
    return make_pdf(runs, info, **extra)
