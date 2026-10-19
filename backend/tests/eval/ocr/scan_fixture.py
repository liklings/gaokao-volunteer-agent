"""
用手头的官方政策 PDF"造"扫描件测试件。

真实扫描件没有标准答案，没法精确算识别率；这里反过来——拿一份有文字层的 PDF，把每一页渲染成图片、
再拼成一份**没有任何文字层**的纯图片 PDF，这份 PDF 对 policy_chunker 来说跟扫描件一模一样，
而原 PDF 的文字层就是 OCR 结果的标准答案，可以逐字比对（字准率 CER）。

两种变体：
- clean：只渲染成图片，没有任何劣化——测 OCR 的上限。
- degraded：模拟真实扫描的常见劣化——轻微倾斜、灰度、高斯噪点、轻微模糊、JPEG 压缩。
  参数是固定种子，每次生成的图完全一样，结果可复现。
注意 degraded 只是"像"扫描件，不等于真实扫描质量（真实的有折痕、阴影、印章压字等），所以这里测出来
的字准率应当被当作乐观估计。
"""
from __future__ import annotations

import io
import random
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image, ImageFilter


def _degrade(img: Image.Image, rng: random.Random) -> Image.Image:
    img = img.convert("L")
    img = img.rotate(rng.uniform(-0.6, 0.6), resample=Image.BICUBIC, fillcolor=255, expand=False)
    arr = np.asarray(img, dtype=np.float32)
    noise = np.random.default_rng(rng.randrange(1 << 30)).normal(0, 9, arr.shape)
    img = Image.fromarray(np.clip(arr + noise, 0, 255).astype(np.uint8))
    return img.filter(ImageFilter.GaussianBlur(radius=0.7))


def make_scanned_pdf(
    src: str | Path, out: str | Path, *, degrade: bool = False, dpi: int = 200,
    scanned_pages: set[int] | None = None, seed: int = 0,
) -> Path:
    """scanned_pages：1 起始的页码集合，只把这些页换成图片，其余页原样保留文字层（造"混合"PDF，
    测 OCR 只在需要的页触发）。None 表示全部页都转成图片。"""
    rng = random.Random(seed)
    with pymupdf.open(str(src)) as src_doc, pymupdf.open() as out_doc:
        for i, page in enumerate(src_doc, 1):
            if scanned_pages is not None and i not in scanned_pages:
                out_doc.insert_pdf(src_doc, from_page=i - 1, to_page=i - 1)
                continue
            pix = page.get_pixmap(dpi=dpi)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            buf = io.BytesIO()
            if degrade:
                _degrade(img, rng).save(buf, format="JPEG", quality=55)
            else:
                img.save(buf, format="PNG")
            new_page = out_doc.new_page(width=page.rect.width, height=page.rect.height)
            new_page.insert_image(new_page.rect, stream=buf.getvalue())
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        out_doc.save(str(out))
    return Path(out)
