"""关键词通道（BM25 + RRF）的单元测试，纯函数，不需要数据库。"""
from app.services.bm25 import Bm25Index, char_bigrams, normalize_for_search, rrf_fuse


def test_normalize_strips_spaces_punct_page_markers_and_fullwidth():
    assert normalize_for_search("外语 3 门，共３門") == "外语3门共3門"
    assert normalize_for_search("—4—\n正文") == "正文"
    assert normalize_for_search("─ 7 ─正文") == "正文"


def test_bigrams_ignore_pdf_inserted_spaces():
    # PDF 文字层常在汉字数字间夹空格；有没有空格必须切出同样的二元组
    assert char_bigrams("外语 3 门科目") == char_bigrams("外语3门科目")


def test_bm25_ranks_exact_term_first_and_zero_for_no_overlap():
    docs = ["省统考设音乐类舞蹈类和书法类", "录取批次设置为本科提前批次和专科批次", "今天天气不错"]
    idx = Bm25Index(docs)
    s = idx.scores("省统考分哪几类")
    assert s.index(max(s)) == 0
    assert idx.scores("zzzz") == [0.0, 0.0, 0.0]


def test_rrf_fuses_by_rank_not_score():
    # 通道A: 0>1>2；通道B: 1>2。1 在两个通道里名次都靠前，应排第一（0 只在 A 里第一名）
    fused = rrf_fuse([[0, 1, 2], [1, 2]], k=60)
    assert fused[0][0] == 1
    assert {i for i, _ in fused} == {0, 1, 2}


def test_rrf_single_channel_preserves_order():
    assert [i for i, _ in rrf_fuse([[5, 3, 9], []])] == [5, 3, 9]
