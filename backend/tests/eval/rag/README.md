# RAG 基线评测（政策问答）

评的是 `search_policy` 这条链路的**检索质量**：给一个口语化的问题，top-k 个 chunk 里有没有包含答案的那一段。
这是后面做混合检索（向量 + 关键词）、加相似度下限、改 chunk 策略之前的**对照基线**。

## 语料与评测集

- `corpus/`：两份官方文件——《江苏省 2025 年普通高校艺术类专业招生办法》（14 页）、《江苏省高等职业院校考试
  招生制度改革实施方案》（8 页，2019 年）。《男生女生十大热门专业分析》**不在**语料里：它不是官方文件，
  混进去会让回答看起来有官方出处。
- `questions.py`：42 题——term 22 / numeric 10 / paraphrase 6 / negative 4（库里没有答案）。
  - **证据短语标注**，不是"应命中第几个 chunk"：结果算命中 ⇔ 来自正确文档且正文含任一证据短语。
    所以以后改 `CHUNK_SIZE`、按标题切分，同一份评测集依然可比，不用重标。
  - `test_rag_harness.py` 每次都会校验证据短语真的出现在所声称的文档里，防止标注写错。

## 怎么跑

在 `backend/` 目录、激活 venv 之后：

```bash
python -m tests.eval.rag.run_baseline --retriever vector   # ★ 系统真实基线，需要 backend/.env 里的 GEMINI_API_KEY
python -m tests.eval.rag.run_baseline --retriever bm25     # 离线参照物，不需要密钥
python -m pytest tests/eval/rag -q                         # 评测框架自身的离线测试
```

`vector` 用项目真实的 chunker + 真实的 Gemini embedding（`gemini-embedding-001`，768 维，同样的
`RETRIEVAL_DOCUMENT` / `RETRIEVAL_QUERY`），相似度在内存里用 numpy 算余弦，**不读写数据库**——不会污染你本地的
`policy_documents` 表，向量按文本哈希缓存在 `.cache/`（已 gitignore），重复跑不重复花额度。
结果写入 `reports/baseline_<retriever>.json`，**跑完 vector 后请把这份报告提交进仓库**，它就是基线。

## 指标怎么读

| 指标 | 含义 |
|---|---|
| hit@1 / hit@3 / hit@5 | 前 k 个结果里有没有命中证据 |
| MRR | 第一个命中结果排名的倒数的平均，越接近 1 越好 |
| 文档对 | top-1 结果是否来自正确的文档 |
| 证据完整 | 证据短语是否完整落在某一个 chunk 里。<1 说明切分把证据切断了——这类失败是**切分问题**，不是检索问题 |
| 无答案区分度 AUC | 有答案问题的 top-1 相似度 vs 无答案问题的 top-1 相似度能否分开；0.5=分不开，1=完全分开。决定"加相似度下限"是否可行 |
| 阈值=负例最高分时仍通过的比例 | 把相似度下限设在最高的负例分数上，有多少有答案的问题还能通过（越高越好） |

## 局限（请别过度解读）

- 语料只有两份文件、~33 个 chunk，**检索太容易**——绝对数字会偏高，只适合比较"改动前 vs 改动后"的相对变化，
  不能当作线上真实水平。想要更有区分度，需要补更多官方文件（专项计划通知、各批次志愿填报规定等）。
- 38 个有答案的问题，一道题 ≈2.6 个百分点，几个百分点的差异在统计上不显著。
- `bm25-bigram` 是离线字符二元组 BM25，**不是系统的基线**，只是无需密钥的参照物，用来在做混合检索前先看看
  "关键词通道"在这批问题上大概什么水平。

## 切块与混合检索的对比实验

`run_baseline.py` 现在可以组合：`--chunking legacy|structured`（旧：逐页按段落累积；新：按「一、/（一）」标题
切小节、chunk 开头带章节面包屑、清掉页码标记）× `--retriever vector|bm25|hybrid`（hybrid = 向量 + 关键词，
RRF 融合，和线上 `policy_search.py` 同一套参数）。`--offline-cache` 让向量只读 `.cache`，不调 Gemini。

沙箱里离线可得的结果（关键词通道，仅换切块方式）：hit@1 0.816 → 0.868，hit@5 0.895 → 0.974，MRR 0.855 → 0.910。
向量和混合在新切块下的数字需要带真实 Key 跑一次（新切块的向量要重新算，约 33 个 chunk，花费可忽略）：

    python -m tests.eval.rag.run_baseline --retriever all --chunking structured

注意：这套题 22/38 是「术语类」，天然偏向关键词；语料只有 ~30 个 chunk，1 道题 = 2.6 个百分点，
差距在 3 个点以内都不要当作结论。融合分是 RRF 名次分，「负例分离度」只对 vector/bm25 有意义。
