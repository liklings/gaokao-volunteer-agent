from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

from ..text_utils import norm

CORPUS_DIR = Path(__file__).parent / "corpus"


def corpus_files() -> list[Path]:
    return sorted(CORPUS_DIR.glob("*.pdf"))


def page_texts(pdf_path: Path) -> list[str]:
    """pypdf 提取的每页文字（原样，未归一化）。"""
    return [(p.extract_text() or "") for p in PdfReader(str(pdf_path)).pages]


def evidence_pages(doc_name: str, evidence: list[str]) -> set[int]:
    """任意一个证据短语出现在该文档的哪些页（1 起始，归一化后匹配）。找不到返回空集合。"""
    pages = page_texts(CORPUS_DIR / doc_name)
    found: set[int] = set()
    for i, text in enumerate(pages, 1):
        nt = norm(text)
        if any(norm(e) in nt for e in evidence):
            found.add(i)
    return found
