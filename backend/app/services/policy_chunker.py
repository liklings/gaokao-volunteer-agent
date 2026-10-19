"""
把一份政策文档（.docx / .pdf）读出正文、切成一个个适合做向量检索的文本片段。

切分策略：按自然段落累积，累积到接近 CHUNK_SIZE 字符数就切一刀，切的时候保留
CHUNK_OVERLAP 个字符的重叠——纯按段落切会导致"一个完整的规则说明恰好被切成两半、
检索时只命中半句话"，重叠一部分能缓解这个问题（不是完美方案，但不需要引入分词器/
语义切分模型这类更重的依赖，够用）。不按"整篇文档一个 chunk"是因为政策原文经常
几千字，一个 chunk 太大会稀释向量的语义、检索时召回率下降；也不按"每句话一个
chunk"，太碎会丢失上下文，一句话经常要配合前后文才能准确回答用户的问题。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import docx
from pypdf import PdfReader

from app.services.ocr import OcrUnavailableError, ocr_image

logger = logging.getLogger(__name__)

CHUNK_SIZE = 500       # 每个 chunk 的目标字符数（中文语境下，没有用 token 数是因为
                        # 不想为了精确算 token 数额外引入分词依赖，字符数是够用的近似）
CHUNK_OVERLAP = 80      # 相邻 chunk 之间重叠的字符数
MIN_SECTION_CHARS = 80  # 标题切分时，正文不足这么多字的小节并入下一节，避免出现只有一两句话的碎片 chunk

# 页码标记：政策 PDF 每页首/末常有「—4—」「─ 7 ─」这类整行页码，混进正文会污染 chunk 和关键词检索
_PAGE_MARKER = re.compile(r"^\s*[—─–\-]{1,3}\s*\d{1,3}\s*[—─–\-]{1,3}\s*$")
# 公文常见的两级标题：「一、」「第三章」是一级，「（一）」是二级。故意不识别「1.」「（1）」：
# 正文里的编号列表很常见，当标题会把一段话切碎。
_H1 = re.compile(r"^\s*(?:[一二三四五六七八九十]+、|第[一二三四五六七八九十百]+[章节])")
_H2 = re.compile(r"^\s*（[一二三四五六七八九十]+）")


@dataclass
class RawPage:
    """从文件里提取出来的一"页"文字——docx 没有天然页码概念，整份文档当一"页"；
    pdf 按实际页数拆开，方便后面引用能标注到具体页码。
    """
    page_number: int | None
    text: str
    is_ocr: bool = False  # 这一页的文字是 OCR 识别出来的（扫描件），不是 PDF 自带的文字层


@dataclass
class Chunk:
    chunk_index: int
    content: str
    page_number: int | None
    is_ocr: bool = False  # 目前只在内存里带着（评测/日志用），还没有落库——需要时再加 policy_chunks.is_ocr 列


class UnsupportedDocumentTypeError(ValueError):
    pass


def extract_pages(file_path: str) -> list[RawPage]:
    suffix = Path(file_path).suffix.lower()
    if suffix == ".docx":
        return _extract_docx(file_path)
    if suffix == ".pdf":
        return _extract_pdf(file_path)
    raise UnsupportedDocumentTypeError(
        f"不支持的文档类型：{suffix}（目前只支持 .docx / .pdf，"
        f".doc 是旧的二进制格式，读取前需要先转换成 .docx，例如用 Word/LibreOffice 另存为）"
    )


def _extract_docx(file_path: str) -> list[RawPage]:
    with open(file_path, "rb") as fh:
        header = fh.read(8)
    if header.startswith(b"\xd0\xcf\x11\xe0"):
        # 扩展名是 .docx、实际是旧版 Word 二进制格式（OLE2）——政策文件从网站下载后常见。
        raise UnsupportedDocumentTypeError(
            f"{Path(file_path).name} 扩展名是 .docx，但实际是旧版 .doc 二进制格式，python-docx 读不了；"
            f"请先用 Word/WPS/LibreOffice 另存为真正的 .docx（命令行：soffice --headless --convert-to docx 文件名）再导入"
        )
    document = docx.Document(file_path)
    paragraphs = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    text = "\n\n".join(paragraphs)
    return [RawPage(page_number=None, text=text)] if text else []


def _extract_pdf(file_path: str) -> list[RawPage]:
    reader = PdfReader(file_path)
    pages = []
    ocr_unavailable_logged = False
    for i, page in enumerate(reader.pages):
        text = (page.extract_text() or "").strip()
        is_ocr = False
        if len(text) < _ocr_min_chars() and _page_has_images(page):
            # 文字层几乎是空的：多半是扫描图片页，尝试 OCR 兜底。OCR 不可用时退回老行为
            # （跳过这页），但打一条 warning，不再是完全静默。
            try:
                ocr_text = _ocr_pdf_page(file_path, i).strip()
            except OcrUnavailableError as exc:
                if not ocr_unavailable_logged:
                    logger.warning("%s 第 %d 页没有文字层，但 OCR 不可用，跳过扫描页：%s", file_path, i + 1, exc)
                    ocr_unavailable_logged = True
            else:
                if len(ocr_text) > len(text):  # OCR 没比原来多识别出东西就不替换（比如本来就是一页很短的真文字）
                    text, is_ocr = ocr_text, True
        if text:
            pages.append(RawPage(page_number=i + 1, text=text, is_ocr=is_ocr))
    return pages


def _page_has_images(page) -> bool:
    """没有文字层、也没有任何图片的页是真正的空白页，不值得花几秒钟跑 OCR；扫描页一定带着整页图片。
    pypdf 解析个别特殊编码的图片会抛异常，这时保守地当成"有图片"，宁可多 OCR 一次也不漏扫描页。"""
    try:
        return len(page.images) > 0
    except Exception:  # noqa: BLE001
        return True


def _ocr_min_chars() -> int:
    from app.config import get_settings  # 惰性导入：纯文字 PDF/docx 的路径不依赖 Settings

    return get_settings().ocr_min_page_chars


def _ocr_pdf_page(file_path: str, page_index: int) -> str:
    """把 PDF 的第 page_index 页（0 起始）渲染成图片再 OCR。依赖 pymupdf 和 OCR 引擎，缺哪个抛
    OcrUnavailableError。"""
    from app.config import get_settings

    settings = get_settings()
    try:
        import pymupdf  # type: ignore
    except ImportError as exc:
        raise OcrUnavailableError("未安装 pymupdf（pip install -r requirements-ocr.txt）") from exc
    with pymupdf.open(file_path) as doc:
        png = doc[page_index].get_pixmap(dpi=settings.ocr_render_dpi).tobytes("png")
    return ocr_image(png, settings.ocr_engine)


def _split_into_paragraphs(text: str) -> list[str]:
    # pdf 提取出来的文字经常没有干净的段落分隔（换行符插得很随意，可能是每行一断，
    # 不一定对应真正的段落边界），这里先按连续空行分段，分不出多段的话整段当一段，
    # 后面按字符数切分的逻辑本身就能兜底处理"一大段没有任何分隔符"的情况。
    parts = [p.strip() for p in text.split("\n\n") if p.strip()]
    return parts if parts else ([text.strip()] if text.strip() else [])


def _chunk_page_text(text: str, size: int = CHUNK_SIZE) -> list[str]:
    paragraphs = _split_into_paragraphs(text)
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= size:
            current = candidate
            continue
        # 加上这一段就超了：先把攒到目前为止的内容切成一个 chunk。
        if current:
            chunks.append(current)
            # 重叠：把上一个 chunk 末尾的一部分字符带到下一个 chunk 开头，
            # 缓解"关键句子恰好卡在切分点上"的问题。
            overlap_text = current[-CHUNK_OVERLAP:] if len(current) > CHUNK_OVERLAP else current
            current = f"{overlap_text}\n\n{para}"
        else:
            current = para
        # 单个段落本身就超过 CHUNK_SIZE（比如一大段没有换行的正文）：按字符数硬切，
        # 不再等下一段落到来，避免一个 chunk 无限增长。
        while len(current) > size:
            chunks.append(current[:size])
            current = current[size - CHUNK_OVERLAP :]
    if current:
        chunks.append(current)
    return chunks


_SENTENCE_PUNCT = re.compile(r"[，。；：！？,;:!?]")


def _heading_level(line: str) -> int:
    """1 / 2 = 一级 / 二级标题，0 = 不是标题。

    真标题短且不含句内标点（「（一）省统考」）；以「（一）」开头的整句话（「（二）综合素质评价围绕……，
    建立……」）是带编号的正文段落，PDF 里还常被折行成半句，必须当正文——误判成标题会把一个段落
    从中间劈开。"""
    text = line.strip()
    if len(text) > 24:
        return 0
    m = _H1.match(text) or _H2.match(text)
    if not m or _SENTENCE_PUNCT.search(text[m.end():]):
        return 0
    return 1 if _H1.match(text) else 2


@dataclass
class _Section:
    path: list[str]          # 标题路径，如 ["四、专业考试", "（一）省统考"]
    lines: list[str]         # 该节正文行（不含标题行本身）
    page_number: int | None  # 该节第一行所在页
    is_ocr: bool = False


def _split_sections(pages: list[RawPage]) -> list[_Section]:
    """把整份文档（跨页连续）按标题切成小节。页码标记行在这里被丢弃。"""
    sections: list[_Section] = []
    path: list[str] = []
    current = _Section(path=[], lines=[], page_number=None)
    for page in pages:
        for raw_line in page.text.splitlines():
            line = raw_line.strip()
            if not line or _PAGE_MARKER.match(line):
                continue
            level = _heading_level(line)
            if level:
                if current.lines or current.path:
                    sections.append(current)
                path = path[: level - 1] + [line]
                current = _Section(path=list(path), lines=[], page_number=page.page_number, is_ocr=page.is_ocr)
            else:
                if current.page_number is None and not current.lines:
                    current.page_number, current.is_ocr = page.page_number, page.is_ocr
                current.lines.append(line)
    if current.lines or current.path:
        sections.append(current)
    return sections


def _common_prefix(paths: list[list[str]]) -> list[str]:
    out: list[str] = []
    for items in zip(*paths):
        if len(set(items)) != 1:
            break
        out.append(items[0])
    return out


def _build_structured_chunks(pages: list[RawPage]) -> list[Chunk]:
    sections = _split_sections(pages)
    chunks: list[Chunk] = []
    carry: list[_Section] = []  # 正文太短的小节，连同标题并入下一节

    def emit(group: list[_Section]) -> None:
        # 面包屑 = 这一组小节共同的标题路径（四、专业考试 > （一）省统考）。它同时参与向量化和关键词
        # 检索——答案段落的正文里常常没有出现这些词。合并的小节各自的标题行留在正文里，面包屑不去
        # 指向其中某一个，避免"面包屑说是（三），正文却从（一）开始"。
        prefix_items = _common_prefix([g.path for g in group]) if len(group) > 1 else list(group[0].path)
        lines: list[str] = []
        for g in group:
            if len(group) > 1 and g.path and g.path[-1] not in prefix_items:
                lines.append(g.path[-1])
            lines.extend(g.lines)
        body = "\n".join(lines)
        if not body.strip():
            return
        prefix = " > ".join(prefix_items)
        budget = max(CHUNK_SIZE - len(prefix) - 1, 100)
        first = group[0]
        for piece in _chunk_page_text(body, budget):
            content = f"{prefix}\n{piece}" if prefix else piece
            chunks.append(Chunk(
                chunk_index=len(chunks), content=content, page_number=first.page_number,
                is_ocr=any(g.is_ocr for g in group),
            ))

    for i, sec in enumerate(sections):
        nxt = sections[i + 1] if i + 1 < len(sections) else None
        if not sec.lines and nxt is not None and len(nxt.path) > len(sec.path) and nxt.path[: len(sec.path)] == sec.path:
            continue  # 没有正文的父标题（「四、专业考试」后面直接是「（一）省统考」）：它已经在子节的面包屑里了
        if len("\n".join(sec.lines)) < MIN_SECTION_CHARS and nxt is not None:
            carry.append(sec)
            continue
        emit(carry + [sec])
        carry = []
    if carry:
        emit(carry)
    return chunks


def build_chunks(pages: list[RawPage], structured: bool = True) -> list[Chunk]:
    """structured=True：文档里检测到标题时按标题切小节（带面包屑、清页码标记）；没有任何标题
    的文档（或 structured=False）走原来的"逐页按段落累积"逻辑，行为不变。"""
    if structured and any(_heading_level(ln) for p in pages for ln in p.text.splitlines()):
        return _build_structured_chunks(pages)
    return _build_chunks_legacy(pages)


def _build_chunks_legacy(pages: list[RawPage]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for page in pages:
        for text in _chunk_page_text(page.text):
            chunks.append(Chunk(
                chunk_index=len(chunks), content=text, page_number=page.page_number, is_ocr=page.is_ocr,
            ))
    return chunks


def chunk_document(file_path: str, structured: bool = True) -> list[Chunk]:
    """一步到位：读文件 → 提取正文（按页）→ 切分成 chunk 列表，chunk_index 全局
    连续编号（跨页也不重置），方便后面按顺序展示/去重。
    """
    pages = extract_pages(file_path)
    return build_chunks(pages, structured=structured)
