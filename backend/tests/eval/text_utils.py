"""评测共用的文本归一化。

PDF 文字层经常在汉字、数字之间夹空格（"外语 3门"、"第 1小批"），所以"一段文字是否包含某个
证据短语"这类判断，一律先做归一化再比：NFKC（全角/半角统一）+ 去掉全部空白。
"""
from __future__ import annotations

import re
import unicodedata

_PAGE_MARKER = re.compile(r"[—─\-－一]\s*\d{1,3}\s*[—─\-－一]")
_NON_CONTENT = re.compile(r"[^\w]", re.UNICODE)


def norm(text: str) -> str:
    """证据匹配用：NFKC + 去空白（保留标点，标点本身也是证据的一部分，如 ×0.5）。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def norm_content(text: str) -> str:
    """OCR 字准率用：在 norm 基础上再去掉页码标记和所有标点符号，只比较"内容字符"（汉字/字母/数字）。
    标点（全半角逗号、引号、〔〕）和页码页眉的 OCR 差异不影响检索效果，混进去只会稀释指标。
    """
    t = unicodedata.normalize("NFKC", text)
    t = _PAGE_MARKER.sub("", t)
    return _NON_CONTENT.sub("", t)


def levenshtein(a: str, b: str) -> int:
    """纯 Python 编辑距离（两行滚动数组）。单页 ~500 字符，几十万次运算，够用。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]
