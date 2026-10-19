# 分层测评（Layered Evaluation）—— 第一阶段：代码自动化断言

对应三段流水线「代码自动化断言 → 历史回测 → LLM-as-Judge」的第一步。
这一层只测能用程序化断言判定对错、不需要真实/模拟多轮对话、不需要另
起一个模型当裁判的部分——工具层的硬约束零容忍、以及冲稳保梯度的结构
自洽性。历史回测和 LLM-as-Judge 建立在"硬约束没崩"这个前提上，所以
放在后面做。

## 目录结构

- `fixtures.py`：不落库的 `School`/`AdmissionGroup`/`AdmissionRecord`/
  `ProfileIn` 构造函数，复用 `tests/test_rules_engine.py` 里验证过的思路
  （`evaluate_group()` 只读属性，不需要真的存进数据库）。
- `scenarios.py`：Layer 2（硬约束零容忍）场景库，声明式的 dict 列表，往
  后扩充到 300~500 条时直接往列表里追加即可，不用改测试代码本身。
- `test_layer2_hard_constraints.py`：跑 `scenarios.py` 里的每一条场景，
  断言 `must_fail` 绝不出现、`must_pass` 一定出现，外加一个打印"硬约束
  违规率"汇总的测试。
- `test_layer3_tier_structure.py`：冲稳保梯度的结构自洽性 + 高分段（全省
  前500名）位次占比分档的边界行为回归保护。
- `test_layer4_discovery_boundary.py`：信息差发现（discoveries）该出现/
  不该出现的边界场景。
- `backtest/`：Layer 5 历史回测（核心逻辑 `core.py` 纯函数 + 合成数据自检；`run_backtest.py` 连本机数据库只读运行）。
- `rag/`：政策文件 RAG 检索基线测评（42 题，证据短语标注，hit@k/MRR/
  证据完整率/负例分离度），详见 `rag/README.md`。
- `ocr/`：扫描件 OCR 回退测评（用原文件造纯图片 PDF，以原文字层为标准答案
  算字准率/关键事实召回/端到端检索），详见下方"RAG 与 OCR 测评"。

跑法和其余测试一样：`cd backend && python -m pytest tests/eval -v`。

## 跟需求文档的指标对照，目前覆盖到什么程度

| 层级 | 需求文档指标 | 本阶段覆盖情况 |
|---|---|---|
| 1. 意图与参数提取 | Slot F1-Score > 98% | **流程已跑通**——`slots/`：200 条用例（手写种子 + 参数化生成：单轮/推断类别/多轮/纠偏/可选开关/闲聊不该幻觉/较难表达），逐轮调用真实 Profile Agent，按生产合并规则得到最终槽位后算 micro-F1 与分槽位 P/R/F1，另附"必填不齐时是否追问"代理指标。需 Gemini Key，见下方"Slot F1"一节。规模仍不足以作为正式 98% 判定（目标 300–500 条）。 |
| 2. 硬约束遵从度 | 违规率 0%，绝对红线 | **已覆盖**第一批 12 条种子场景（选科冲突、地域极端含"招生目标省份≠实际所在地"、中外合作/专项计划默认排除+同意后放行、军警类无开关、预算、科类）。 |
| 3. 冲稳保梯度合理性 | 梯度覆盖率 > 95% | **已覆盖**——口径已确认为代码实际的固定权重 **保底:稳妥:冲刺 = 3:4:3**（不再用 20/60/20）；结构自洽性、3:4:3 名额精确性、候选池悬殊/某档不足时的名额再分配、高分段边界行为均已编码成断言。 |
| 4. 信息差发现能力 | ~~召回率 ≥ 2所~~（已取消） | 只覆盖边界行为："保底档不该被当成发现""稳妥/冲刺档冷门城市对口校该被发现"。"≥2所"数量下限按产品决定**不做**。 |
| 5. 真实录取回测 | 模拟录取成功率（红线由产品于 2026-10 定为 **≥95%**，原需求 99.5%） | **脚本已就绪**——`backtest/`：只给规则引擎看 T 年之前的数据，用 T 年真实最低位次判卷；输出三档校准表、保底档录取率、整份方案不滑档率。需在本机连库运行，见下方"历史回测"。 |

## 口径事项（第1条已确认，其余待确认）

1. ~~冲20%/稳60%/保20%~~ → **已确认（口径定为 3:4:3）**：Layer 3 以
   `candidate_curation._CATEGORY_TARGET_WEIGHTS = {"baowen": 3, "wen": 4,
   "chong": 3}` 为准，衡量"精选 10 所里三档各占几个名额"。位次差占比
   分档阈值（决定候选属于哪一档）与 3:4:3（决定每档展示几个）互不冲突。
   对应断言见 `test_layer3_tier_structure.py` 末尾（权重常量钉住、三档
   充足时精确 3/4/3、候选池规模悬殊不受影响、某档不足时余量按权重流向
   其余档且总数仍为 10）。
2. ~~信息差召回率 ≥2所~~：已决定不做，不再作为指标。
3. **两个需求文档里提到、但当前系统完全没有对应机制的场景**（如实标
   注出来，而不是假装写了能通过的测试）：
   - "特殊排除 Case"里的"不读软件工程（高学费）"——这是**排除**某个
     专业关键词，但 `ProfileIn.major_keyword` 目前只支持**包含**匹配
     （软匹配、不参与硬性排除），没有"明确排除某个专业"的硬性字段。
     如果这确实是产品需要的能力，需要先在 `ProfileIn`/`evaluate_group`
     里加一个硬性排除字段，再回来补这条 case，不能靠现有字段硬凑。
   - "压线段（特控线/本科线上下5分）Case"——系统目前的数据模型里没有
     "批次线/特控线"这个概念（`School`/`AdmissionGroup` 都没有对应字段），
     只有具体专业组的历年 `min_rank`。这条 case 想测的"精准锁定保底
     二本/公办院校、绝不滑档"，本质上会退化成"这个位次附近，min_rank
     明显更靠后的候选是不是被正确分类成了保底"——已经被 Layer 3 的结构
     自洽性测试间接覆盖，如果需要专门针对"批次线"本身的判断逻辑，需要
     先确认要不要真的建这个数据字段。

## 下一步

1. **扩充 Layer 2 场景到覆盖需求文档全部 4 类边界场景**：当前"多轮纠偏
   与补全"（首轮只说"我考了610分，能去哪"，测试 Agent 该不该追问）不
   能用纯函数测，需要接 Orchestrator 真实/模拟对话——属于 Layer 1，规划
   在下一阶段做。
2. **Layer 1（Slot F1）**：复用 `tests/test_advisor.py` 里已经在用的
   `trace.steps[].tool_result` 结构，写一批"用户原话 → 期望的硬约束参数"
   的 case，用真实（或 mock 掉 Gemini 调用、只测 Profile Agent 抽槽逻辑
   的）对话跑一遍，比较抽出来的槽位和期望槽位的 F1。
3. ~~Layer 5（历史回测）~~：脚本已完成，见"历史回测"一节，等本机跑出第一份报告。
4. ~~LLM-as-Judge~~：流程已跑通，见下方“LLM-as-Judge”一节。

## RAG 与 OCR 测评

语料为两份政策 PDF（艺术类招生办法、高职招考改革方案；热门专业那份已排除）。

- RAG 基线：`python -m tests.eval.rag.run_baseline --retriever vector`
  （需要 backend/.env 里真实的 GEMINI_API_KEY）。沙箱里只跑了离线 BM25
  参考线（`rag/reports/baseline_bm25-bigram.json`），**向量基线需在本机跑后提交**。
  语料很小（约 33 个 chunk、38 个正例），指标波动大，只适合看"改动前后的相对变化"。
- OCR：`pip install -r requirements-ocr.txt`，然后
  `OCR_ENGINE=paddleocr python -m tests.eval.ocr.run_ocr_eval`。
  沙箱网络拦截 PaddleOCR 模型下载，这里用 RapidOCR（同为 PP-OCR 系列 ONNX）验证，
  **PaddleOCR 路径本身未实测**。RapidOCR 结果：字准率 99.1%~99.9%；
  关键事实严格召回 85%~87%，丢失的几乎都是 ÷ × + 等符号被误识（忽略符号后艺术类 96%）。
  文号括号 `〔〕`→`【】` 已用后处理修复。降质变体较温和，不能据此宣称抗劣质扫描。
- 带 `ocr` 标记的测试默认不跑：`OCR_ENGINE=rapidocr python -m pytest -m ocr tests/eval/ocr`。
- 后续：`is_ocr` 尚未入库；混合检索（关键词通道 + RRF）、相似度阈值、标题感知切块待做，做完用同一套题对比基线。

## 历史回测（Layer 5）

    cd backend && python -m tests.eval.backtest.run_backtest                 # 2025/2024/2023 各一遍
    python -m tests.eval.backtest.run_backtest --no-rank-scale               # 对照：不做跨年位次折算
    python -m tests.eval.backtest.run_backtest --years 2025 --n-ranks 60     # 只测一年、更密的位次网格
    python -m tests.eval.backtest.run_backtest --sweep                       # 扫 历史口径(latest/min2/min3) × 保底阈值(0.2~0.5)
    python -m tests.eval.backtest.run_backtest --history-strategy min3 --baowen-ratio 0.3

做法：对每个验证年 T，只保留 year < T 的历史记录，用生产里同一套 `evaluate_group → 冲稳保分档 → curate(3:4:3)`
给一批虚拟考生（位次 500～10 万对数等距、物理类/历史类）出方案，再用 T 年真实最低位次判卷
（考生位次 ≤ 该组 T 年最低位次 = 录取）。输出：

- **三档校准表**：冲/稳/保各自的实际录取率。理想是单调（保 ≥ 稳 ≥ 冲），保底接近 100%；
- **保底档录取率**与**整份方案不滑档率**，对照 Pass Line 95%（`core.PASS_LINE_BAOWEN`；原需求 99.5% 在仅 3 年数据、单组年际波动大的条件下达不到，2025 年验证约 95–99%）；
- 保底档按位次段拆分（高分段最敏感）和失败样例（便于追查是哪类专业组、哪个位次段出的问题）。

局限（报告解读时要记住）：只看「位次 ≤ 当年最低位次」，不模拟平行志愿先后顺序、当年扩招缩招、专业组合并改名；
T 年没有录取记录的推荐项不判卷、单独计数；一分一段表缺某年时跨年折算会自动退化（与线上一致）。
`test_backtest_core.py` 用合成数据验证这把尺子本身：不偷看 T 年答案、稳定世界保底 100%、竞争突然加剧时能报警。


## Slot F1（Layer 1）

```
python -m tests.eval.slots.run_slot_eval            # 真实调 Gemini；响应缓存在 slots/.cache，重跑不重复调用
python -m tests.eval.slots.run_slot_eval --no-cache # 改了 prompt/模型后用这个
python -m pytest -q tests/eval/slots                # 评分器/运行器自检，不需要 Key
```

口径见 `slots/metrics.py` 顶部：只比最终槽位；标准答案里没写的槽位应为 null，凭空填值记 FP；值错记 1 FP + 1 FN；
year 不计分；等价判断宽松但有界（省份后缀、类别简写、选科集合、专业/城市包含关系）。调用失败的用例按漏检计，不会被略过。
报告写入 `slots/reports/slot_f1.json`。

用例来源：`cases.py` 手写（seed/hard/hand-*）+ `gen_cases.py` 固定种子参数化生成（标准答案由生成参数得出，不靠人工抄写）。
生成用例只覆盖"槽位组合 × 表达方式"的广度，句式多样性有限；真实口语/错别字/冲突输入要靠手写补充和线上对话回流。

人工复核：`python -m tests.eval.slots.export_review` 导出 `reports/review_sheet.csv`（按 tag 分层抽检 ≥20%，用 Excel 打开），
复核重点是推断出来的槽位（如由选科推断类别）和被否定/改口的槽位；发现标准答案有误就改用例，不要改评分器去迁就。

局限：200 条仍低于 300–500 的目标；未人工复核前，F1 数字只能当参考。真实运行约 288 次 Gemini 调用（有缓存，已跑过的不重复），
免费档按每分钟 12 次约需 20–25 分钟。


## LLM-as-Judge

只评代码断言测不了的模糊层面。两类被评对象：**Profile Agent 的追问回复**（追问得当、简洁、不编造、语气）和
**检索推荐回复**（有据可依、信息差讲清价值、如实转述位次说明、结构清晰、实用）。能用代码判断的不交给 LLM：
推荐回复另有确定性检查（不得出现概率/把握措辞、信息差学校必须被提到、回复里的学校必须出现在工具结果里）。

```
python -m tests.eval.judge.run_judge --target calibration   # 先校准评审员，过关才能信后面的分数
python -m tests.eval.judge.run_judge --target profile       # 默认抽 80 轮；与 Slot F1 共用 Agent 响应缓存
python -m tests.eval.judge.run_judge --target advisor       # 真实跑 8 个推荐场景（需本机库里的江苏数据），结束后清理评测数据
python -m pytest -q tests/eval/judge                        # 流程自检，不需要 Key
```

要点：评审员只能依据背景事实（不许用自己的知识圆谎）、先写理由再打分、温度 0、明确不因更长更热情加分；
解析失败/缺维度/越界分数记为 error，不补默认分。**评审员本身要先过校准**：用 17 个人工标注的好/坏回复检验，
一致率 ≥85% 且误放行（坏回复被判合格）为 0，才认为评审员可信；校准集是人工构造的单一缺陷样本，难度偏低，
一致率高只说明能抓明显缺陷。建议用与被评模型不同的评审模型（环境变量 `JUDGE_MODEL`）以免自我偏好。

合格线（均为建议值，产品可调）：分数 ≥4 记合格；Profile 回复各维度合格率 ≥90%；推荐回复各维度 ≥80%（只有 8 个场景，仅作参考）；
确定性检查要求全部通过。评审响应缓存在 `judge/.cache`，改了评分维度或 prompt 后要删掉缓存。
