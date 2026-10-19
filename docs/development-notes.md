# 开发笔记（原 README，保留了各里程碑的详细验证步骤）

> 主文档见根目录 [README](../README.md)。以下为开发过程中的详细记录，部分命令基于早期里程碑，仅供参考。

# 志愿领航 Agent

高考志愿对话式规划 Agent。这是从纯前端原型往"可落地产品"过渡的项目，采用前后端分离架构。

## 快速开始（含数据导入）

> 仓库附带江苏省真实招生数据（作者自行收集整理，约 4.7 MB，仅供学习研究，详见 [data/DATA.md](data/DATA.md)）。**不构成志愿填报建议，请以官方发布为准。**

```bash
# 1. 建库（需要 PostgreSQL + pgvector 扩展）
createdb -U postgres gaokao_agent

# 2. 配置环境变量：复制 backend/.env.example 为 backend/.env，填入自己的 GEMINI_API_KEY 和 JWT_SECRET
cd backend && cp .env.example .env

# 3. 建表
alembic upgrade head

# 4. 导入数据（院校 / 专业组 / 历年录取 / 一分一段）和政策向量库
psql -U postgres -d gaokao_agent -f ../data/jiangsu_admission_data.sql
psql -U postgres -d gaokao_agent -f ../data/policy_vectors.sql

# 5. 修正自增序列（数据里带有显式 id）
psql -U postgres -d gaokao_agent -c "select setval(pg_get_serial_sequence('admission_records','id'), (select max(id) from admission_records)); select setval(pg_get_serial_sequence('score_rank_rows','id'), (select max(id) from score_rank_rows)); select setval(pg_get_serial_sequence('policy_documents','id'), (select max(id) from policy_documents)); select setval(pg_get_serial_sequence('policy_chunks','id'), (select max(id) from policy_chunks));"
```

政策 RAG 只提供向量库（`data/policy_vectors.sql`），不随仓库发布政策原文文件；向量由 `gemini-embedding-001`（768 维）生成，检索时的查询向量需要你自己的 Embedding Key。RAG 评测集依赖的政策原文需自行准备，详见 `backend/tests/eval/README.md`。评测怎么跑见 `backend/tests/eval/README.md`。

## 当前进度：里程碑6 —— 前端拆分成独立工程，接上真实后端

**里程碑1-5**（骨架连通 + 数据库模型/种子数据 + 资格过滤打分接口 + 对话接入Gemini + 方案版本/导出接口）已完成，见下方说明。

**里程碑6新增：**
- `frontend/` 从空目录变成真正的独立静态前端工程：`index.html` + `src/{config.js, api.js, main.js, styles.css}`，原生 ES module，没有构建步骤，不需要 npm
- 对话、候选查询、方案确认/导出全部改成调用真实后端接口，不再是浏览器里写死的数组或直连 Anthropic API
- `profile_id` 存在浏览器 `localStorage`，刷新页面、重新打开都能接着上次的对话继续（服务器端 `profiles`/`chat_messages` 表本来就持久化了完整状态）
- `frontend/prototype-reference.html` 保留最早那版单文件原型，仅供参考对照，不再维护

**已实测的部分**（不需要真实 Gemini Key 也能测）：
- 静态资源全部能正常访问（`index.html`/`src/*.js`/`src/styles.css` 返回 200）
- `main.js` 里引用的所有 DOM id 和 `index.html` 里实际存在的 id 逐一核对过，没有漏写或拼错的
- `src/api.js` 用 Node 直接 import 后对真实后端发请求测过：健康检查、`/api/candidates/search`（真实拿到10个候选）、`/api/plans` 确认+读取+导出（全部返回预期数据）、以及两条错误路径（profile 不存在→404、没配置 Gemini Key→503）都正确抛出、消息清晰
- CORS 单独测过：用 `Origin: http://localhost:8080` 发预检请求给后端，确认 `access-control-allow-origin` 正确放行——这是浏览器会强制检查、但 curl 默认不会检查的一环，容易在"看起来能用"之后才在真实浏览器里炸掉，所以单独验证了一遍

**没测过、需要你在真实浏览器里验证的部分**：完整的点击交互（输入框打字、勾选候选卡片、点确认方案这些 DOM 事件绑定是否符合直觉）——这部分我只能做到"代码逻辑正确、ID全部对得上"，没法在这个沙箱里真的用鼠标点一遍。跑起来后建议按下面"完整走一遍界面"的步骤自己点一次。

## 完整走一遍界面（需要真实 Gemini Key）

```bash
# 后端（一个终端）
cd backend
source .venv/bin/activate
uvicorn app.main:app --reload

# 前端（另一个终端）
cd frontend
python3 -m http.server 8080
```
浏览器打开 http://localhost:8080 ，点"示例：江苏物理类考生"按钮把示例消息填进输入框，发送，等 Agent 回复、条件收集齐后点"查询候选"，勾选几个候选卡片，点"确认方案"，最后点"导出方案"应该会打开一个文本文件。哪一步卡住了把浏览器控制台（F12 → Console/Network）里的报错发我。

## 有真实 API Key 后，怎么验证整个流程跑通了

按顺序做（假设你已经按上面"方式二"把服务起来了，`.env` 里填好了真实 `GEMINI_API_KEY`）：

```bash
cd backend
source .venv/bin/activate
python scripts/smoke_test_full_flow.py
```

这个脚本会依次做三件事，每一步都打印结果，方便你看是哪一步出问题：
1. 健康检查——确认数据库连通
2. 用一句包含全部必填信息的话调用 `/api/chat`（真实调用 Gemini），打印 Agent 回复、当前 profile、`missing`/`ready`
3. 如果 `ready=true`，拿这份条件去调 `/api/candidates/search`，打印前 5 个候选的学校名和参考概率

**预期结果**：三步都成功，第三步打印出类似
```
东南大学         pct= 42 冲刺
南京邮电大学       pct= 71 稳妥
...
```
这样的候选列表，就说明"对话 → 收集条件 → 查候选"这条完整链路真的跑通了。

**常见问题排查**：
- 第一步就失败 → 数据库没起来或没跑 `alembic upgrade head`
- 第二步 HTTP 503 → `.env` 里没填 `GEMINI_API_KEY` 或密钥没生效（改完 `.env` 记得重启 `uvicorn`）
- 第二步 HTTP 502 或格式解析失败 → 把打印出来的原始报错发给我，调整请求格式
- 第二步成功但 `ready` 一直是 `false` → 说明模型没能从这句话里提取出全部必填字段，可以把 `scripts/smoke_test_full_flow.py` 里 `MESSAGES` 那句话改得更明确，或者多发几轮消息
- 第三步返回 `results` 数量是 0 → 检查种子数据是否真的导入过（`python -m app.services.seed_loader`）

想测试"分多轮对话逐步补充信息，Agent 是否会正确追问缺失项"这条路径，把脚本里 `MESSAGES` 列表拆成几条依次发送的短消息即可，脚本会自动复用同一个 `profile_id` 继续对话。

**还没做的**：方案版本/导出接口、前端拆分成独立工程并接上这几个后端接口（现在前端仍是之前那个单文件 HTML 原型，还没有改成调用这些新接口）。

---

## 方式一：Docker（推荐，环境统一）

前提：本机装好 Docker 和 Docker Compose。

先设置 `JWT_SECRET`（必填，没有默认值，不设置的话 `docker compose` 会直接报错拒绝启动）：
```bash
# 在项目根目录建一个 .env 文件（docker compose 会自动读取），或者直接 export
echo "JWT_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" > .env
```

```bash
# 在项目根目录
docker compose up --build
```

起来之后：
- 前端：http://localhost:8080
- 后端：http://localhost:8000
- 接口文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/api/health
- 数据库：`localhost:5432`，用户名/密码 `postgres`/`postgres`，库名 `gaokao_agent`

首次启动后，建表 + 导入种子数据（容器已经在跑，另开一个终端执行）：
```bash
docker compose exec backend alembic upgrade head
docker compose exec backend python -m app.services.seed_loader
```

停止：`docker compose down`（加 `-v` 会连数据库数据卷一起删掉，谨慎使用）。

---

## 方式二：本地直接跑（不用 Docker）

前提：本机装好 Python 3.11+ 和 PostgreSQL。

```bash
# 1. 创建数据库（只需执行一次）
psql -U postgres -c "CREATE DATABASE gaokao_agent;"

# 2. 进入后端目录，建虚拟环境，装依赖
cd backend
python3 -m venv .venv
source .venv/bin/activate        # Windows 用 .venv\Scripts\activate
pip install -r requirements.txt

# 3. 配置环境变量
cp .env.example .env
# 打开 .env：
# - 确认 DATABASE_URL 里的用户名/密码/端口和你本地 Postgres 一致
# - JWT_SECRET 必填，用 python -c "import secrets; print(secrets.token_urlsafe(32))" 生成一个填进去
# - 有真实 Gemini Key 的话填到 GEMINI_API_KEY

# 4. 建表
alembic upgrade head

# 5. 导入种子数据（江苏物理类，10所学校/10个专业组/30条年度记录）
python -m app.services.seed_loader

# 6. 启动
uvicorn app.main:app --reload
```

启动后同样访问 http://localhost:8000/docs 和 http://localhost:8000/api/health。

---

## 验证这一步是否真的跑通了

```bash
curl http://localhost:8000/api/health
```

数据库连通时应该返回：
```json
{"status":"ok","app":"志愿领航 Agent Backend","environment":"local","database":"connected","database_error":null}
```

如果把数据库停掉再请求一次，会看到 `status` 变成 `"degraded"`，`database` 变成 `"unreachable"`，并带上具体的连接错误信息——这是刻意设计的，用来证明这个健康检查是真的在查数据库，不是写死的假状态。

试一下资格过滤 + 打分接口——**这个接口现在需要登录**（见下面"认证"一节），
先注册拿到 token：
```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","password":"testpass123"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -X POST http://localhost:8000/api/candidates/search \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"province":"江苏","track":"物理类","subjects":["物理","化学"],"score":637,"rank":5200,"major_keyword":"计算机"}'
```
返回里 `results` 是通过全部硬性规则（省份/科类/选科/预算）的候选，`audit` 是全部候选（含未通过的），每条都带 `rules` 数组说明每条规则通过与否、原因。

---

## 认证

`/api/chat`、`/api/candidates/search`、`/api/plans` 这几个接口都要求登录。流程：

```bash
# 注册（同时返回 token，不用再单独登录一次）
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"你的邮箱","password":"至少8位的密码"}'

# 之后登录换 token
curl -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"你的邮箱","password":"你的密码"}'
```

拿到 `access_token` 后，后续请求都带上 `Authorization: Bearer <access_token>`。token 7天过期。

数据按用户隔离：一个用户创建的 `profile`/`plan`，用别的账号的 token 去访问会返回 404（不区分"不存在"和"存在但不是你的"，避免信息泄露）。

**当前没做、但知道存在的缺口**（如果要正式对外开放注册，建议先补）：
- 注册/登录接口没有限流，理论上可以被脚本批量调用刷注册表或暴力破解密码
- 不验证邮箱真实性——任何格式正确的字符串都能注册成功，没有发确认邮件这一步

---

## MCP 客户端 与 Skill 知识库

Agent 现在接入了两个"目录常驻、内容/能力按需加载"的机制，取代了之前 `web_search` 工具"尚未接入真实数据源"的占位实现：

**MCP 客户端**（`app/services/mcp_client.py`）：Agent 作为 [MCP](https://modelcontextprotocol.io)（Model Context Protocol）client，通过标准 stdio 协议连接外部 MCP server，`web_search` 工具的真实执行转发给已连接的 MCP server。默认配置（`backend/mcp_servers.json`）只接了仓库自带的一个离线参考实现（`app/mcp_servers/policy_search_server.py`，一个内置小数据集的 `FastMCP` server，覆盖招生计划变化、新专业报考、冲学校/冲专业权衡、就业前景参考这几类常见问题），不依赖任何外部网络——这是因为当前开发沙箱本身网络受限（一般搜索引擎/API 都不通，之前调 Gemini 自带的 `google_search` grounding 功能也踩过免费层额度是 0 的坑），离线参考实现能在任何环境下稳定测试、稳定跑通。**要换成真实联网搜索**，只需要改 `mcp_servers.json` 里的 `command`/`args`/`env`（换成某个真实提供 MCP 协议的搜索服务），不用改一行 `tools.py`/`mcp_client.py` 的代码，`env` 支持 `${VAR_NAME}` 语法从进程环境变量里插值 API Key，密钥不进配置文件明文。

**Skill 知识库**（`app/services/skills.py`）：把专项计划、平行志愿投档规则、征集志愿、专业调剂这几类分领域的政策知识，从系统提示词里搬出来，做成独立的 markdown 文档（`app/skills/*.md`），系统提示词里只常驻一份很短的目录（id+标题+一句话摘要，见 `app/skills/manifest.json`），模型判断这一轮问题涉及某个领域时，自己调用 `load_skill` 工具按需加载完整正文——不管这一轮问不问得到，都不会把这几千字的政策说明全部塞进每一次 Gemini 调用里。新增一个政策领域，只需要加一个 markdown 文档 + `manifest.json` 加一行，不用改提示词代码。

两者都已经写了自动化测试：
- `tests/test_mcp_client.py`——真实端到端连接（子进程 + stdio 协议握手），覆盖正常调用、未知工具、配置文件缺失、server 被禁用、重复 `start()` 这几种场景
- `tests/test_skills.py`——目录读取、正文加载、未知 skill_id 的失败信息
- `tests/test_react_loop_mcp_and_skills.py`——在真实的 ReAct 循环状态机里（不是直接调用 `execute_tool`）验证这两个工具能被正确调度、真实拿到数据、正确写回对话历史
- `tests/test_tools_real_data.py` 里 `web_search` 相关用例——同样是真实调用（不是脚本模拟返回值）

**已知的范围之外**：这套 MCP 连接目前没有"某个 server 连接失败/调用超时后自动降级到备用数据源"这类容错策略，一个 server 连不上时对应的工具（比如 `web_search`）就会返回明确的失败信息，交给上层 Agent 决定怎么回应用户，不会让整个请求崩溃，但也不会自动换一个数据源重试。

---

## 导入真实数据（一分一段表 / 历年录取位次 / 选科限制 / 招生计划）

`seed_data/jiangsu_physics_seed.json` 只是示例数据（10所学校，方便本地跑通全流程用），要换成真实数据，用下面两个导入脚本，不需要手动改 JSON、也不需要先把 Excel 转格式。两个脚本都是幂等的（重复导入同一份文件是 upsert，不会产生重复行），列名支持常见的措辞变体自动识别，识别不了会把"需要哪个字段"和"表里实际有哪些列"都打印出来，不会是一个模糊的报错。

**一分一段表**（`app/services/score_rank_loader.py`）——存进新增的 `score_rank_rows` 表，这是一张独立的全省维度表，不属于任何学校，回答"某年某科类某分数，全省排第几名"：

```bash
python -m app.services.score_rank_loader \
    --file ~/Downloads/江苏2026物理类一分一段表.xlsx \
    --province 江苏 --year 2026 --track 物理类
```

表里默认识别"分数/总分"、"本段人数/该分人数"、"累计人数/位次"这几类常见表头，如果你的文件列名不在这个别名列表里，去文件顶部的 `_COLUMN_ALIASES` 加一行就行。导入之后可以用 `app/services/rank_lookup.py` 里的 `lookup_rank_for_score()`/`lookup_score_for_rank()` 做分数↔位次的换算（比如把去年某个专业组的 `min_rank` 换算成今年大概对应的分数）。

这个脚本额外处理两种官方表常见的情况（对照真实江苏一分一段表验证过）：最高分那一档经常是合并区间，比如"683-750"（顶尖分数段人数太少不逐分公布），会按区间下限存一行（683分及以上考生查分数会命中这一行，跟原表定义一致，不是编造）；如果表里把物理类/历史类放在同一张表用一列科类区分（而不是每个科类单独一个文件），会自动只导入跟 `--track` 参数匹配的行，避免另一个科类的数据被错误地当成当前科类导入进去。

**历年录取位次 / 选科限制 / 2026招生计划**（`app/services/admission_data_loader.py`）——复用 `seed_loader.py` 里同一套 upsert 逻辑，落进 `schools`/`admission_groups`/`admission_records` 这几张表，跟示例数据是完全一样的表结构：

```bash
python -m app.services.admission_data_loader \
    --file ~/Downloads/江苏2026招生计划.xlsx \
    --province 江苏 --year 2026
```

需要的列：学校名称、城市、科类、选考科目要求（比如"物理+化学"，会自动拆成列表）、计划数是必填；院校代号/专业组代号建议提供（用来生成稳定的内部 id，没有的话会退化成用名称生成一个短哈希，能用但可读性差）；最低分/最低位次允许留空——**2026年这种只有计划、还没有分数线的数据可以直接导入**，留空的字段会落成 `None`，`rules_engine`/`recommendation_guard` 都已经处理了这种"数据不完整"的情况，不会误判成有把握的候选，也不会报错崩溃。

**"专家版"合并大表（推荐，如果你手上有这一份）**（`app/services/admission_master_loader.py`）——像"江苏-2026-专家版数据（23-26）"这种把 2026招生计划 + 2025/2024/2023三年历史录取数据 + 院校基础信息合并在一张表里的文件，专门写了一个独立的加载器（不是上面通用的 `admission_data_loader.py`，因为这种表结构特殊：三行合并表头、每行是"专业"粒度而不是"专业组"粒度）：

```bash
python -m app.services.admission_master_loader \
    --file ~/Downloads/江苏-2026-专家版数据.xlsx
```

省份默认从文件"生源地"列自动识别，不用手动传；表头默认在文件第3行（`--header-row 2`，可覆盖）。这个加载器会把同一专业组下的多个专业行自动聚合成一条专业组记录：2026年用文件自带的"专业组计划人数"，2025年优先用文件自带的"专业组最低分/位次"，2024/2023年因为文件没有专业组级别字段，改用"组内所有专业最低分/位次取最小值"来近似（平行志愿下专业组投档线基本等于组内最低录取分，方法合理但不是官方投档线原文，跑完会在输出的 notes 里提醒你核实关键数据）。已经用真实文件的前300行跑过一次冒烟测试（不落库，纯验证解析逻辑），零跳过零报错。

**建议的操作顺序**：先拿一个只有几行数据的小样本文件跑一遍，确认列名都识别对了、生成的 id 符合预期，脚本打印的"跳过了几行、原因是什么"也过一遍确认没有意外的整批跳过，再用完整文件导入。三个脚本目前都写了自动化测试（`tests/test_score_rank_loader.py`、`tests/test_admission_data_loader.py`、`tests/test_admission_master_loader.py`、`tests/test_rank_lookup.py`），验证的是导入逻辑本身（用构造出来的数据 + 真实写出的 `.xlsx` 文件测），没有也没法验证你真实数据文件的具体列名措辞对不对得上——这一步只能你在自己机器上用真实文件跑一遍确认。

---

## 政策原文向量检索（RAG，pgvector + Gemini Embedding）

除了结构化的招生数据（学校/专业组/分数线），Agent 还需要能查询官方政策文件的原文（报名办法、专项计划通知、录取实施办法这类文档），而且回答时要能标注"这句话出自哪份文件第几页"，不能凭自己的知识编造。这一层是独立于对话模型的一套向量检索子系统：

**架构上刻意做了两件事解耦**：

1. **向量存储 vs 对话模型**：向量检索这一层（`embeddings.py`/`policy_chunker.py`/`policy_ingest.py`/`policy_search.py`）完全不依赖对话用的是哪个模型——哪天把生成最终回复的模型从 Gemini 换成 Qwen 之类的，这一层的代码和数据库表一行都不用改，`search_policy` 工具照样能用。
2. **但是向量本身绑定着生成它的 embedding 模型**：`policy_chunks` 表里存的每一条向量，都是特定 embedding 模型（`app/config.py` 里的 `policy_embedding_model`，当前是 `gemini-embedding-001`）在特定维度（`policy_embedding_dimensions`，当前 768）下产出的。如果哪天连 embedding 模型本身也要换掉（不是对话模型，是专门算向量这个模型），不同模型产出的向量不在同一个语义空间里，没法混着比相似度，必须把所有政策文档重新导入一遍，而且 `Vector(768)` 这个列的维度定义也要跟着改。这两件事的边界不一样，实现前已经跟需求方确认过。

**用到的组件**：
- `pgvector` Postgres 扩展做向量存储和余弦相似度检索（`<=>` 算子 + HNSW 索引），不引入额外的独立向量数据库服务。
- Gemini Embedding API（`:embedContent`/`:batchEmbedContents`，注意这是跟对话用的 `:generateContent`完全不同的接口）把文本转成向量，索引时用 `taskType=RETRIEVAL_DOCUMENT`，检索时用 `taskType=RETRIEVAL_QUERY`（Gemini 官方推荐的检索场景用法，两种 taskType 编出来的向量不完全一样）。

**在你自己的机器上装 pgvector**（先装扩展再跑 `alembic upgrade head`，迁移里会自动 `CREATE EXTENSION IF NOT EXISTS vector`，但扩展本体需要你先装好）：

```bash
# macOS，本地原生 Postgres 是用 Homebrew 装的：
brew install pgvector
# 如果 brew 装的 Postgres 版本较新且 pgvector 没有对应的预编译包，改用源码编译：
#   git clone --branch v0.7.4 https://github.com/pgvector/pgvector.git
#   cd pgvector && make && make install

# Postgres.app 用户：pgvector 需要单独装，参考 pgvector 官方 README 里
# "Postgres.app" 这一节的说明（不同大版本步骤不同，这里不重复列）

# Docker 部署（docker-compose.yml 用的 postgres 镜像）：换成带 pgvector 的镜像，
# 比如 pgvector/pgvector:pg16，或者在 Dockerfile 里基于官方镜像加一层编译安装

# 装好扩展本体之后：
cd backend && alembic upgrade head
```

**导入政策文档**：

```bash
# 导入整个目录（递归找 .docx/.pdf）：
python -m app.services.policy_ingest --dir /path/to/你的政策文件夹

# 或者单个文件：
python -m app.services.policy_ingest --file ~/Downloads/某份政策.pdf
```

**已知的格式限制**：只支持 `.docx` 和 `.pdf`，**不支持 `.doc`**（旧的二进制格式，Python 生态没有能可靠读取的库）——`--dir` 模式扫到 `.doc` 文件时不会跳过不提示，而是明确记进"跳过文件"列表并打印原因，提示你先用 Word/LibreOffice 另存为 `.docx` 再重新导入。如果你手上那批政策文件里有 `.doc`（比如"江苏省2025年重点高校招生专项计划.doc"这类），导入完看一眼终端打印的跳过列表，把这些文件转格式后再跑一次就行。扫描版 PDF（没有文字层、只有图片）同样会被明确报错（"没有从这份文档里提取到任何正文"），不会假装导入成功。

**幂等**：按文件的绝对路径识别"是不是同一份文档"，重新导入同一个路径会把这份文档名下的旧片段全部清空、按最新内容重新切分、重新生成向量，不是增量更新——因为"重新导入"通常意味着文档内容变了，旧片段的边界和向量大概率跟新内容对不上，全量重切成本也不高（政策文档就几十份）。

**检索**：Agent 新增了一个 `search_policy` 工具（Retrieval Agent 和 Advisor Agent 都能调用），接受一个查询文本，返回最相关的几个原文片段，每条都带着来源文档标题（+ 页码，如果有）。系统提示词（`app/services/agent_prompts.py`）里已经加了规则：涉及具体政策条文/报名条件/时间节点/专项计划资格这类"官方原文写的是什么"的问题，模型必须调用 `search_policy` 查原文并在回复里注明来源，查不到就如实告诉用户，不允许编造。

自动化测试：`tests/test_policy_chunker.py`（docx/pdf 切分逻辑，含真实写出的测试文件）、`tests/test_embeddings.py`（mock httpx，验证请求体里的 taskType/维度对不对、批量调用顺序不错乱、错误处理）、`tests/test_policy_ingest.py`（真实数据库 + mock embedding，验证落库/幂等重导入/`.doc`跳过/单文件报错都对）、`tests/test_policy_search.py`（真实数据库 + 真实 pgvector 排序，手工构造方向明确的向量验证余弦相似度排序和 `similarity` 数值算得对）——这几个测试都不会真的调用 Gemini API，跑 `pytest` 不花钱、不需要网络。

---

## 底线策略校验网关（推荐名单的强制校验）

Advisor Agent（负责给取舍/推荐建议的那个子 Agent）现在不能再直接"嘴上说"推荐哪几个学校——给出最终建议文字之前，必须先调用 `finalize_recommendation` 工具，把这次打算提到的院校专业组 `group_id` 完整提交，由 `app/services/recommendation_guard.py` 做一次纯代码的底线校验，跟这个项目一路坚持的"missing/ready 由服务器算、不采信模型自报"是同一条原则在"最终推荐结果"这一层的延伸。

**校验内容**：
- 硬性规则（复用 `rules_engine.evaluate_group()` 里已经验证过的判断，不重新发明一套）——省份/科类/选科要求/预算是否都满足，不满足的候选直接判定校验不通过。
- `group_id` 是否真实存在、名单里有没有重复。
- 名单大小上限（超过 8 个判定不通过）——防止把 `search_candidates` 的检索结果不加筛选整批提交，那样等于没做"取舍"，违背 Advisor Agent 的职责。
- 冲稳保结构风险——名单里 3 个及以上候选、且能计算出概率的候选全部集中在"冲刺"及以上风险等级、没有一个"稳妥"或"保底"时，给出结构性风险警告（这个不算硬性不通过，但警告文字必须传达给用户）。

**服务器端强制、不是只靠提示词自觉**：`app/services/orchestrator.py` 里 `_run_advisor_step()` 会检查这一轮的执行轨迹——如果模型没有成功调用过 `finalize_recommendation`（不管是压根没调用，还是调用了但没通过校验也没有重试成功），不管模型最后的文字回复写得多么言之凿凿，都会被服务器端替换成一句明确的兜底提示，不会把没校验过的建议写进聊天记录、返回给用户。这层拦截发生在写入数据库之前，不是"先存后删"。

**已知的局限**（如实记录，不含糊带过）：这一层能确定性验证"提交给 `finalize_recommendation` 的这份名单本身站不站得住"，但没法验证模型最终文字回复里提到的学校，是不是跟通过校验的那份名单完全一致——如果模型在校验通过之后，又在文字里额外夹带了一个没校验过的学校，这层拦不住。要彻底堵上这个缝，需要让最终回复也走结构化生成（比如要求 `is_final_answer` 时同时返回一份结构化的"本次提到的 group_id 列表"，服务器再核对文字和结构化列表是否一致），这是比现在这一步更大的改动，先如实记在这里，作为下一步可以做的加固项。

自动化测试：`tests/test_recommendation_guard.py`（纯校验逻辑，覆盖空名单/重复/不存在的 group_id/硬性规则不通过/超出数量上限/结构性风险警告）、`tests/test_advisor_recommendation_gate.py`（用脚本化假模型模拟"模型不听话、绕开校验直接给结论"和"模型按规矩调用并且能根据失败原因重试"两种场景，验证 orchestrator 这一层的强制拦截确实生效）。

---

## 目录结构

```
gaokao-agent/
├── docker-compose.yml
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt / pyproject.toml
│   ├── .env.example
│   ├── alembic/
│   │   ├── versions/d1cd47840988_create_core_tables.py   # 已实测 upgrade/downgrade
│   │   └── versions/501846486053_add_policy_documents_and_policy_chunks_.py  # pgvector 扩展 + 政策向量检索两张表 + HNSW 索引
│   └── app/
│       ├── main.py           # FastAPI 入口
│       ├── config.py         # 环境变量读取（含 policy_embedding_model/policy_embedding_dimensions）
│       ├── db.py             # SQLAlchemy 引擎/会话/连通性检查
│       ├── api/
│       │   ├── health.py         # 健康检查接口
│       │   ├── candidates.py     # POST /api/candidates/search
│       │   ├── chat.py           # POST /api/chat
│       │   └── plans.py          # POST /api/plans, GET /api/plans/{id}, GET .../export
│       ├── models/           # ORM 模型（含 policy_document.py：PolicyDocument/PolicyChunk 向量检索两张表）
│       ├── schemas/
│       │   ├── candidate.py      # ProfileIn / CandidateOut / SearchResponse
│       │   ├── chat.py           # ChatRequest / ChatResponse / ProfileOut
│       │   └── plan.py           # PlanConfirmRequest / PlanOut / DiffEntry
│       ├── services/
│       │   ├── seed_loader.py    # 种子数据导入脚本（幂等）
│       │   ├── admission_data_loader.py  # 真实历年录取位次/选科限制/招生计划 Excel 导入（幂等）
│       │   ├── admission_master_loader.py # "专家版"合并大表专用导入（三行表头+专业粒度聚合）
│       │   ├── score_rank_loader.py      # 真实一分一段表 Excel 导入（幂等）
│       │   ├── rank_lookup.py    # 一分一段表查询：分数↔位次换算
│       │   ├── rules_engine.py   # 资格过滤 + 冲稳保打分（从JS原型移植）
│       │   ├── agent_client.py   # Profile Agent 的 Gemini API 客户端，密钥只在服务器端
│       │   ├── gemini_tool_caller.py  # Retrieval/Advisor Agent 用的 Gemini function calling 客户端
│       │   ├── embeddings.py     # Gemini Embedding API 客户端（embedContent/batchEmbedContents，跟对话模型解耦）
│       │   ├── policy_chunker.py # 政策文档 docx/pdf 正文提取 + 分段切分（带重叠）
│       │   ├── policy_ingest.py  # 政策文档导入脚本：切分→生成向量→落库（幂等）
│       │   ├── policy_search.py  # 政策向量检索：pgvector 余弦相似度 + 来源标注
│       │   ├── profile_rules.py  # 必填项/missing 判断（服务器端确定性计算）
│       │   ├── plan_service.py   # 版本diff计算 + 导出文本生成
│       │   ├── tools.py          # 工具适配层：search_candidates/get_candidate_detail/compare_candidates/web_search/load_skill/search_policy/finalize_recommendation
│       │   ├── react_loop.py     # ReAct 循环状态机（重复调用检测/步数上限/token预算/超时保护）
│       │   ├── orchestrator.py   # 编排 profile/retrieval/advisor 三个 Agent + 底线校验网关的强制拦截
│       │   ├── recommendation_guard.py  # 底线策略校验网关：硬性规则/数据完整性/冲稳保结构
│       │   ├── agent_prompts.py  # Retrieval/Advisor 的系统提示词（含 Skill 目录拼接）
│       │   ├── mcp_client.py     # MCP 客户端管理器，web_search 工具的真实执行转发到这里
│       │   ├── skills.py         # Skill 目录/正文加载
│       │   ├── security.py       # 密码哈希 + JWT 编解码
│       │   └── limits.py         # 成本控制阈值（token预算/工具超时/整轮超时/限流）
│       ├── mcp_servers/
│       │   └── policy_search_server.py  # web_search 默认接的离线参考 MCP server
│       ├── skills/
│       │   ├── manifest.json     # Skill 目录（id/标题/摘要/文件名）
│       │   └── *.md              # 各领域政策知识正文
│       └── seed_data/
│           └── jiangsu_physics_seed.json
│   └── mcp_servers.json       # 配置 Agent 要连接哪些 MCP server（默认只配了离线参考实现）
├── scripts/
│   ├── test_chat_stub.py     # 不需要真实API Key的对话流程桩测试
│   ├── smoke_test_full_flow.py  # 有真实API Key后，跑通 对话→候选查询 全链路
│   └── test_plans_manual.py  # /api/plans 的手动冒烟测试（不需要Gemini Key）
└── frontend/
    ├── Dockerfile             # nginx 静态服务
    ├── index.html
    ├── prototype-reference.html  # 最早那版单文件原型，仅供参考，不再维护
    └── src/
        ├── config.js          # 后端地址配置，改这一个值就能切换部署环境
        ├── api.js             # 所有 fetch 调用集中在这里
        ├── main.js            # UI 状态管理 + DOM 渲染
        └── styles.css
```

## 部署到公网前的加固清单

- [x] 账号体系 + 多用户数据隔离（见"认证"一节）
- [x] `JWT_SECRET` 强制要求配置，不允许用默认值/空值启动
- [x] 安全响应头（`X-Content-Type-Options`/`X-Frame-Options`/`Referrer-Policy`，`environment=production` 时额外加 `Strict-Transport-Security`）—— 见 `backend/app/middleware.py`
- [x] Docker 镜像里应用进程不用 root 用户跑，加了健康检查
- [x] 前端有清晰可见的免责声明（页面顶部常驻提示条 + 页脚），说明数据仅供参考、不构成录取保证
- [x] Advisor Agent 的推荐名单强制经过底线策略校验网关（服务器端拦截，不是只靠提示词自觉）—— 见"底线策略校验网关"一节，已知局限是没法校验最终文字回复跟通过校验的名单是否完全一致
- [ ] 注册/登录接口限流（防批量注册/暴力破解），见"认证"一节的已知缺口
- [ ] 邮箱真实性验证（发确认邮件），同上
- [ ] 自动化测试套件（见 `backend/tests/` 和 `backend/pytest.ini`，`pytest` 一键跑）——**已完成**，部署前建议接入 CI 每次 push 自动跑一遍
- [ ] 数据库自动备份策略（`pg_dump` 定时任务，或托管数据库自带的备份功能）——还没有，自己起服务器部署的话需要另外配置
- [ ] HTTPS：本项目不处理 TLS termination，需要在反向代理（nginx/Caddy）或云平台的负载均衡层配置证书
- [ ] `docker-compose.yml` 里 Postgres 的 `5432:5432` 端口映射默认对宿主机开放，公网服务器上建议改成只在 Docker 内部网络可达（去掉这条 `ports` 配置），只保留本地开发时按需临时加上用来连 `psql` 调试
- [ ] 接入真实官方数据源替换种子数据（`app/seed_data/jiangsu_physics_seed.json` 目前是示例数据）

## 下一步

候选的几个方向：接入真实官方数据源替换种子数据、把上面清单里未勾选的项目逐个补上、部署到公网可访问的地址。
