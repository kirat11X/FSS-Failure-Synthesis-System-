#!/usr/bin/env python3
"""
Extract plain text and structured sections from all reference papers and specs
in W/ and the workspace root.
"""
import os
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
import pypdf

OUTPUT_DIR = Path("extracted_specs")
OUTPUT_DIR.mkdir(exist_ok=True)

def extract_odt(odt_path: Path) -> str:
    with zipfile.ZipFile(odt_path) as z:
        content_xml = z.read("content.xml")
    root = ET.fromstring(content_xml)
    # namespaces in ODT
    namespaces = {
        'text': 'urn:oasis:names:tc:opendocument:xmlns:text:1.0',
        'table': 'urn:oasis:names:tc:opendocument:xmlns:table:1.0'
    }
    paragraphs = []
    for elem in root.iter():
        if elem.tag.endswith('}p') or elem.tag.endswith('}h'):
            text = "".join(elem.itertext()).strip()
            if text:
                paragraphs.append(text)
    return "\n\n".join(paragraphs)

def extract_pdf(pdf_path: Path) -> str:
    reader = pypdf.PdfReader(str(pdf_path))
    pages = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        pages.append(f"--- [Page {i+1}] ---\n{text}")
    return "\n\n".join(pages)

def main():
    papers = [
        ("research_fss_neon.odt", Path("research_fss_neon.odt")),
        ("FSS_Research_and_Plan.pdf", Path("FSS_Research_and_Plan.pdf")),
        ("Minor_Project_Failure_Synthesis_System.pdf", Path("Minor Project_ Failure Synthesis System.pdf")),
        ("python_side.pdf", Path("python side.pdf")),
        ("Software_Engineering_Lab_Project_Submission.pdf", Path("Software Engineering Lab Project Submission.pdf")),
        ("FSS_RESEARCH_PAPER.md", Path("FSS_RESEARCH_PAPER.md")),
        ("FSS_PROJECT.md", Path("FSS_PROJECT.md")),
        ("IRL_SIGNIFICANCE.md", Path("IRL_SIGNIFICANCE.md")),
    ]

    for name, path in papers:
        if not path.exists():
            print(f"Skipping {name}: not found at {path}")
            continue
        print(f"Extracting {name}...")
        out_txt = OUTPUT_DIR / f"{Path(name).stem}.txt"
        if path.suffix.lower() == ".odt":
            text = extract_odt(path)
        elif path.suffix.lower() == ".pdf":
            text = extract_pdf(path)
        elif path.suffix.lower() == ".md":
            text = path.read_text(encoding="utf-8")
        else:
            text = path.read_text(encoding="utf-8", errors="ignore")
        out_txt.write_text(text, encoding="utf-8")
        print(f"Saved {len(text)} chars to {out_txt}")

if __name__ == "__main__":
    main()
