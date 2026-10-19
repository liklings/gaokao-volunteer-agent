"""
扫描版 PDF 的 OCR 兜底（app/services/ocr.py + policy_chunker._extract_pdf）。

用手头的官方 PDF "造"扫描件（tests/eval/ocr/scan_fixture.py）：转成纯图片 PDF、没有文字层，原 PDF 的
文字层当标准答案。需要真实 OCR 引擎的用例标了 @pytest.mark.ocr，默认不跑：
    pytest -m ocr tests/eval/ocr            # 用 auto：先 paddleocr，没有就 rapidocr
    OCR_ENGINE=paddleocr pytest -m ocr ...  # 指定引擎
完整的字准率/关键事实召回/端到端报告用 `python -m tests.eval.ocr.run_ocr_eval`。
"""
import logging
import os

os.environ.setdefault("JWT_SECRET", "eval-only")

import pytest

pymupdf = pytest.importorskip("pymupdf")
from pypdf import PdfReader  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services import ocr as ocr_module  # noqa: E402
from app.services.ocr import OcrUnavailableError, postprocess_ocr_text  # noqa: E402
from app.services.policy_chunker import extract_pages  # noqa: E402

from ..rag.corpus_utils import CORPUS_DIR, page_texts  # noqa: E402
from ..text_utils import levenshtein, norm_content  # noqa: E402
from .scan_fixture import make_scanned_pdf  # noqa: E402

VOC = CORPUS_DIR / "江苏省高等职业院校考试招生制度改革实施方案.pdf"


@pytest.fixture
def engine_env(monkeypatch):
    """让 Settings 用指定的 OCR 引擎；测完清掉 lru_cache，避免污染其他用例。"""
    def _set(engine: str, **extra):
        monkeypatch.setenv("OCR_ENGINE", engine)
        for k, v in extra.items():
            monkeypatch.setenv(k.upper(), str(v))
        get_settings.cache_clear()
    yield _set
    monkeypatch.undo()
    get_settings.cache_clear()


# ---------- 不需要 OCR 引擎的用例（默认就跑）----------

def test_generated_scan_fixture_has_no_text_layer(tmp_path):
    scanned = make_scanned_pdf(VOC, tmp_path / "scan.pdf", dpi=100)
    reader = PdfReader(str(scanned))
    assert len(reader.pages) == len(PdfReader(str(VOC)).pages)
    assert all(not (p.extract_text() or "").strip() for p in reader.pages), "造出来的扫描件不该有文字层"


def test_scanned_pages_are_skipped_with_warning_when_ocr_unavailable(tmp_path, engine_env, monkeypatch, caplog):
    """OCR 引擎没装时退回老行为（跳过扫描页，导入不崩），但必须打 warning——不再是完全静默。"""
    scanned = make_scanned_pdf(VOC, tmp_path / "scan.pdf", dpi=100)
    engine_env("paddleocr")

    def _unavailable(*_a, **_k):
        raise OcrUnavailableError("测试：引擎不可用")

    monkeypatch.setattr(ocr_module, "_get_engine", _unavailable)
    with caplog.at_level(logging.WARNING):
        pages = extract_pages(str(scanned))
    assert pages == []
    assert any("OCR 不可用" in r.message for r in caplog.records)


def test_text_pdf_never_touches_ocr(engine_env, monkeypatch):
    """有文字层的正常 PDF 不能触发 OCR（既慢又可能引入识别错误）。"""
    engine_env("paddleocr")

    def _boom(*_a, **_k):
        raise AssertionError("纯文字 PDF 不该调用 OCR")

    monkeypatch.setattr(ocr_module, "_get_engine", _boom)
    pages = extract_pages(str(VOC))
    assert pages and not any(p.is_ocr for p in pages)


def test_blank_page_without_images_does_not_trigger_ocr(tmp_path, engine_env, monkeypatch):
    from pypdf import PdfWriter

    engine_env("paddleocr")
    blank = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with open(blank, "wb") as f:
        writer.write(f)

    def _boom(*_a, **_k):
        raise AssertionError("真正的空白页（没文字也没图片）不该调用 OCR")

    monkeypatch.setattr(ocr_module, "_get_engine", _boom)
    assert extract_pages(str(blank)) == []


def test_engine_load_failure_is_reported_as_unavailable_not_crash(monkeypatch):
    """比如 paddleocr 装了但联网下载模型失败——应该是 OcrUnavailableError（调用方据此跳过+告警），
    而不是一个五花八门的底层异常把整个导入流程炸掉。"""
    monkeypatch.setattr(ocr_module, "_engines", {})
    monkeypatch.setitem(ocr_module._LOADERS, "paddleocr", lambda: (_ for _ in ()).throw(RuntimeError("download failed")))
    with pytest.raises(OcrUnavailableError, match="download failed"):
        ocr_module._get_engine("paddleocr")


@pytest.mark.parametrize("raw, expected", [
    ("苏招委【2019】3号", "苏招委〔2019〕3号"),
    ("苏教考函［2025］5号", "苏教考函〔2025〕5号"),
    ("苏教考函[2025]5号", "苏教考函〔2025〕5号"),
    ("国发【2014】35 号", "国发〔2014〕35号"),
    ("苏招委〔2019〕3号", "苏招委〔2019〕3号"),          # 已经正确的不动
    ("见附件[1]和[2024]年度数据", "见附件[1]和[2024]年度数据"),  # 不是文号形态，不误伤
])
def test_postprocess_fixes_document_number_brackets_only(raw, expected):
    assert postprocess_ocr_text(raw) == expected


# ---------- 需要真实 OCR 引擎的用例（pytest -m ocr）----------

def _require_engine(engine_env):
    engine_env(os.environ.get("OCR_ENGINE", "auto"), ocr_render_dpi=150)
    if not ocr_module.is_available(get_settings().ocr_engine):
        pytest.skip("没有可用的 OCR 引擎（pip install -r requirements-ocr.txt）")


@pytest.mark.ocr
def test_ocr_runs_only_on_scanned_pages_of_a_mixed_pdf(tmp_path, engine_env):
    _require_engine(engine_env)
    mixed = make_scanned_pdf(VOC, tmp_path / "mixed.pdf", dpi=150, scanned_pages={3})
    pages = {p.page_number: p for p in extract_pages(str(mixed))}
    assert pages[3].is_ocr and len(pages[3].text) > 100, "第3页是图片，应该走 OCR 并识别出正文"
    assert not pages[2].is_ocr and not pages[4].is_ocr, "有文字层的页不该走 OCR"


@pytest.mark.ocr
def test_clean_scan_char_accuracy_is_high(tmp_path, engine_env):
    _require_engine(engine_env)
    scanned = make_scanned_pdf(VOC, tmp_path / "scan.pdf", dpi=150, scanned_pages={2, 3, 4})
    truth = page_texts(VOC)
    pages = {p.page_number: p for p in extract_pages(str(scanned))}
    edits = total = 0
    for i in (2, 3, 4):
        gt = norm_content(truth[i - 1])
        edits += levenshtein(norm_content(pages[i].text), gt)
        total += len(gt)
    accuracy = 1 - edits / total
    assert accuracy >= 0.95, f"干净扫描件的字准率只有 {accuracy:.2%}"


@pytest.mark.ocr
def test_document_number_survives_ocr_after_bracket_fix(tmp_path, engine_env):
    """真实撞到的问题：OCR 把 〔2019〕 识别成 【2019】，按文号检索原样匹配不上。"""
    _require_engine(engine_env)
    scanned = make_scanned_pdf(VOC, tmp_path / "scan.pdf", dpi=150, scanned_pages={1})
    page1 = next(p for p in extract_pages(str(scanned)) if p.page_number == 1)
    assert "苏招委〔2019〕3号" in page1.text.replace(" ", "")
