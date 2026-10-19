"""
扫描件 OCR：把一页图片转成文字，给 policy_chunker 在 PDF 页面提取不出文字时兜底用。

背景：policy_chunker._extract_pdf 之前直接用 pypdf 读文字层，遇到扫描版 PDF
（整页是一张图片、没有文字层，比如省考试院盖章扫描的通知）会提取出空字符串，
这一页被静默跳过——导入不报错，但这份文档的内容其实有一部分永远进不了向量库。

引擎（settings.ocr_engine）：
- "paddleocr"：PaddleOCR 3.x（paddlepaddle + paddleocr）。中文识别效果好，首次运行会联网下载
  检测/识别模型。依赖较重，装在 requirements-ocr.txt 里，不是主依赖。
- "rapidocr"：RapidOCR（onnxruntime），模型是 PaddleOCR 系列（PP-OCR）导出的 ONNX，
  权重直接打在 pip 包里、不需要联网下载模型，依赖轻很多。适合没法联网下载 paddle 模型的环境
  （比如受限的沙盒/CI），识别质量略低于 paddleocr 最新版的服务端模型。
- "auto"：先试 paddleocr，不可用再退回 rapidocr。

两个引擎都是惰性导入、进程内单例：没装对应依赖时只有真的需要 OCR 才会报
OcrUnavailableError，不影响不涉及扫描件的正常导入流程。
"""
from __future__ import annotations

import logging
import re
import threading

logger = logging.getLogger(__name__)

SUPPORTED_ENGINES = ("paddleocr", "rapidocr", "auto")


class OcrUnavailableError(RuntimeError):
    """请求的 OCR 引擎没装/加载失败。调用方（policy_chunker）据此决定是跳过这一页还是报错。"""


_lock = threading.Lock()
_engines: dict[str, object] = {}


def _load_paddleocr():
    try:
        from paddleocr import PaddleOCR  # type: ignore
    except ImportError as exc:
        raise OcrUnavailableError("未安装 paddleocr（pip install -r requirements-ocr.txt）") from exc
    # 整页文档图：不需要文档方向分类/反扭曲/行方向分类，关掉能明显提速。
    return PaddleOCR(
        lang="ch",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
    )


def _load_rapidocr():
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
    except ImportError as exc:
        raise OcrUnavailableError("未安装 rapidocr-onnxruntime（pip install -r requirements-ocr.txt）") from exc
    return RapidOCR()


_LOADERS = {"paddleocr": _load_paddleocr, "rapidocr": _load_rapidocr}


def _get_engine(name: str):
    with _lock:
        if name not in _engines:
            try:
                _engines[name] = _LOADERS[name]()
            except OcrUnavailableError:
                raise
            except Exception as exc:  # noqa: BLE001 - 模型下载失败/依赖冲突等，统一当成"这个引擎现在用不了"
                raise OcrUnavailableError(f"{name} 加载失败（{type(exc).__name__}）：{exc}") from exc
        return _engines[name]


def _run_paddleocr(engine, png_bytes: bytes) -> str:
    import cv2  # type: ignore  # paddleocr 的依赖，装了 paddleocr 就一定有
    import numpy as np

    image = cv2.imdecode(np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    lines: list[str] = []
    for res in engine.predict(image):
        lines.extend(res["rec_texts"])
    return "\n".join(lines)


def _run_rapidocr(engine, png_bytes: bytes) -> str:
    result, _elapsed = engine(png_bytes)
    # result 是 [[box, text, confidence], ...]，没识别出任何文字时为 None
    return "\n".join(item[1] for item in (result or []))


_RUNNERS = {"paddleocr": _run_paddleocr, "rapidocr": _run_rapidocr}


def resolve_engine_name(engine: str) -> str:
    """把 "auto" 解析成实际可用的引擎名；指定具体引擎时原样返回（不可用时由 ocr_image 抛错）。"""
    if engine not in SUPPORTED_ENGINES:
        raise ValueError(f"不支持的 OCR 引擎 {engine!r}，可选：{SUPPORTED_ENGINES}")
    if engine != "auto":
        return engine
    for candidate in ("paddleocr", "rapidocr"):
        try:
            _get_engine(candidate)
            return candidate
        except OcrUnavailableError:
            continue
    raise OcrUnavailableError("paddleocr 和 rapidocr 都不可用（pip install -r requirements-ocr.txt）")


def is_available(engine: str = "auto") -> bool:
    try:
        _get_engine(resolve_engine_name(engine))
        return True
    except OcrUnavailableError:
        return False


# 公文文号形如"苏教考函〔2025〕5号"，年份外面是六角括号〔〕。OCR 引擎普遍把〔〕认成【】或［］
# （评测里真实撞到：两份文件的发文字号都被识别成了 苏招委【2019】3号 / 苏教考函［2025］5号），
# 导致按文号检索（"苏招委〔2019〕3号"）原样匹配不上。只在"汉字 + 括号包着的四位年份 + 数字 + 号"这个
# 公文文号的确切形态下纠正，不碰其他地方的方括号，误伤面很小。
_DOC_NO_BRACKETS = re.compile(r"([\u4e00-\u9fff])[【\[［]\s*(\d{4})\s*[】\]］]\s*(\d+)\s*号")


def postprocess_ocr_text(text: str) -> str:
    return _DOC_NO_BRACKETS.sub(r"\1〔\2〕\3号", text)


def ocr_image(png_bytes: bytes, engine: str = "auto") -> str:
    """识别一张页面图片（PNG/JPEG 字节），返回按阅读顺序拼接的文字，行与行之间用换行符。"""
    name = resolve_engine_name(engine)
    return postprocess_ocr_text(_RUNNERS[name](_get_engine(name), png_bytes))
