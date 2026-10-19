import * as api from "./api.js";

const $ = (sel) => document.querySelector(sel);
const $all = (sel) => Array.from(document.querySelectorAll(sel));
const esc = (s) => (s == null ? "" : String(s)).replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

const STORAGE_KEY = "gaokao_profile_id";

const state = {
  profileId: null,
  profile: null,
  missing: [],
  conflicts: [],
  ready: false,
  searchResult: null, // { results, audit, curated: { groups, collapsed, collapsed_count } }
  selectedGroupIds: new Set(),
  currentPlan: null,
  collapsedExpanded: false,
  ruleAuditExpanded: false, // 资格核验详情默认收起，见 renderRuleAuditToggle()
};

const FIELD_LABELS = {
  province: "省份", year: "年份/届", track: "类别", subjects: "选科组合",
  score: "分数", rank: "位次", major_keyword: "专业关键词",
  city_pref: "城市偏好", budget_max: "学费上限(元/年)",
};

/* ---------------- chat ---------------- */
function addMsg(role, text) {
  const log = $("#chatLog");
  const div = document.createElement("div");
  if (role === "user") { div.className = "msg user"; div.textContent = text; }
  else if (role === "sys") { div.className = "msg sys"; div.textContent = text; }
  else if (role === "err") { div.className = "msg sys"; div.style.color = "var(--red)"; div.textContent = text; }
  else { div.className = "msg agent"; div.innerHTML = '<span class="who">Agent</span>' + esc(text).replace(/\n/g, "<br>"); }
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

async function handleSend() {
  const input = $("#chatInput");
  const message = input.value.trim();
  if (!message) return;
  addMsg("user", message);
  input.value = "";
  $("#sendBtn").disabled = true;
  addMsg("sys", "正在请求后端…");

  try {
    const result = await api.sendChatMessage(state.profileId, message);
    removeLastSysMsg();

    state.profileId = result.profile_id;
    localStorage.setItem(STORAGE_KEY, String(state.profileId));
    state.profile = result.profile;
    state.missing = result.missing;
    state.conflicts = result.conflicts;
    state.ready = result.ready;

    addMsg("agent", result.reply);
    renderFieldPanel();

    if (state.ready) {
      addMsg("sys", "条件已收集齐，可以点击「查询候选」了。");
    }
  } catch (e) {
    removeLastSysMsg();
    addMsg("err", `请求失败：${e.message}`);
  } finally {
    $("#sendBtn").disabled = false;
  }
}

function removeLastSysMsg() {
  const log = $("#chatLog");
  const sysMsgs = log.querySelectorAll(".msg.sys");
  if (sysMsgs.length) sysMsgs[sysMsgs.length - 1].remove();
}

/* ---------------- sync field panel ---------------- */
function renderFieldPanel() {
  $("#profileIdLabel").textContent = state.profileId != null ? state.profileId : "未开始";

  const p = state.profile || {};
  const rows = [
    ["province", p.province], ["year", p.year], ["track", p.track],
    ["subjects", p.subjects && p.subjects.length ? p.subjects.join(" + ") : null],
    ["score", p.score], ["rank", p.rank], ["major_keyword", p.major_keyword],
    ["city_pref", p.city_pref], ["budget_max", p.budget_max != null ? p.budget_max + " 元/年" : null],
  ];
  $("#fieldList").innerHTML = rows.map(([k, v]) => {
    const missing = (v === null || v === undefined || v === "");
    return `<div class="field-row"><span class="k">${FIELD_LABELS[k]}</span><span class="v ${missing ? "missing" : ""}">${missing ? "待补充" : esc(v)}</span></div>`;
  }).join("");

  const missBox = $("#missingBox");
  if (state.missing && state.missing.length) {
    missBox.style.display = "block";
    $("#missingText").textContent = state.missing.join("、") + "。在对话框里补充即可。";
  } else { missBox.style.display = "none"; }

  const confBox = $("#conflictBox");
  if (state.conflicts && state.conflicts.length) {
    confBox.style.display = "block";
    $("#conflictText").textContent = state.conflicts.join("；");
  } else { confBox.style.display = "none"; }

  $("#searchBtn").disabled = !state.ready;
  $("#statusPill").textContent = state.ready ? "● 条件已就绪" : (state.profileId ? "● 收集条件中" : "● 未开始");
}

/* ---------------- candidates search ---------------- */
async function runSearch() {
  if (!state.profile) return;
  $("#searchBtn").disabled = true;
  $("#searchBtn").textContent = "查询中…";
  try {
    const payload = {
      province: state.profile.province,
      track: state.profile.track,
      subjects: state.profile.subjects || [],
      score: state.profile.score,
      rank: state.profile.rank,
      major_keyword: state.profile.major_keyword,
      city_pref: state.profile.city_pref,
      budget_max: state.profile.budget_max,
      // 这个开关不是聊天里收集的 profile 字段，是查询候选这一步单独的
      // 硬性过滤开关——用户勾选"只看省内院校"时，后端会额外用学校实际
      // 所在城市反查所属省份，排除掉那些"招生目标省份匹配、但物理上在
      // 外省"的院校（比如外省专科院校面向本省招生的计划）。
      home_province_only: $("#homeProvinceOnlyChk").checked,
      // 跟 home_province_only 一样，这两个也是"查询候选"这一步单独的开关，
      // 不是聊天收集的 profile 字段（聊天那边由 Profile Agent 提取、存进
      // Profile，见 agent_client.py）。这里用户没勾选就是 false（明确不
      // 接受），不传 null——REST 直连查询这条路径本来就没有"还没问过"这个
      // 三态的用武之地，勾选框天然只有勾/不勾两态，跟 ProfileIn 里 None 的
      // 语义（"还没问过"）对不上也没关系，反正 false 和 None 在
      // resolve_admission_gate() 里效果一样（都是默认排除）。
      accept_sino_foreign: $("#acceptSinoForeignChk").checked,
      accept_special_program: $("#acceptSpecialProgramChk").checked,
    };
    const result = await api.searchCandidates(payload);
    state.searchResult = result;
    state.collapsedExpanded = false;
    state.ruleAuditExpanded = false; // 每次新查询都重新收起，不沿用上一次的展开状态
    // 默认勾选进方案的是"精选展示"出来的这批（保底/稳妥/冲刺三档挑出来、
    // 好学校优先的那 10 所左右），不是后端 results 里未截断的全量候选——
    // 折叠区里的候选用户压根还没看到，不该被悄悄预选进方案。
    const curatedIds = result.curated.groups.flatMap((g) => g.candidates.map((c) => c.group_id));
    state.selectedGroupIds = new Set(curatedIds);

    document.getElementById("stepCandidates").style.display = "";
    document.getElementById("stepRules").style.display = "";
    renderRuleAudit();
    renderRuleAuditToggle();
    renderCandidates();
    // 查询候选后，用户真正想第一眼看到的是精选出来的保底/稳妥/冲刺候选，
    // 不是自己再往下翻——之前"资格核验"这个原始核验列表排在候选卡片
    // 前面，用户实际测试时第一眼看到的是几千条核验记录里的军校/职业
    // 学院，反而以为系统"随便从数据库检索几条就填上去了"。现在候选区
    // 已经调整到 DOM 里排在核验区前面，这里再补一次平滑滚动，保证不管
    // 用户查询前滚动条停在哪里，查询完都能直接看到精选结果，不用自己找。
    document.getElementById("stepCandidates").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    addMsg("err", `查询候选失败：${e.message}`);
  } finally {
    $("#searchBtn").disabled = false;
    $("#searchBtn").textContent = "查询候选";
  }
}

function renderRuleAudit() {
  // audit 现在由后端排好序（通过硬性规则的排前面）、并按 MAX_AUDIT_RETURNED
  // 截断过——不是数据库里真实核验过的全部候选，真实总数看 total_audit。
  // 候选一多（真实数据下单次查询能匹配几千条）之前这里没有任何数量提示，
  // 列表滚不到底也不知道后面还有多少、有没有被截断，这里补一条提示。
  const audit = state.searchResult ? state.searchResult.audit : [];
  const totalAudit = state.searchResult ? state.searchResult.total_audit : 0;
  const list = $("#ruleList");
  if (!audit.length) {
    list.innerHTML = `<div class="empty"><div class="big">暂无候选可核验</div>后端 audit 列表为空。</div>`;
    return;
  }
  const noteHtml = totalAudit > audit.length
    ? `<div class="list-note">共 ${totalAudit} 条候选参与了核验，当前显示前 ${audit.length} 条（已通过的排在最前面）。想看到更多可以在对话里补充城市偏好/学费预算/专业关键词等条件缩小范围。</div>`
    : `<div class="list-note">共 ${totalAudit} 条候选参与了核验。</div>`;
  list.innerHTML = noteHtml + audit.map((c) => {
    const icon = c.all_hard_pass ? '<span class="rule-icon pass">✓</span>' : '<span class="rule-icon fail">✕</span>';
    const failReasons = c.rules.filter((r) => !r.passed).map((r) => r.desc).join(" ");
    const summary = c.all_hard_pass ? "全部硬性规则通过" : "未通过：" + failReasons;
    return `<div class="rule-item">${icon}
      <div class="rule-main">
        <div class="name">${esc(c.university)} · ${esc(c.group_name || "")}</div>
        <div class="desc">${esc(summary)}</div>
      </div>
    </div>`;
  }).join("");
}

const CAT_CLASS = { baowen: "prob-baowen", wen: "prob-wen", chong: "prob-chong", jichong: "prob-jichong" };

// 之前候选卡片右上角的徽章显示一个"参考概率"百分比（比如"70%"）——用户
// 反馈这个数字是拍脑袋校准出来的线性公式硬算出来的，容易被误当成有统计
// 学依据，改成直接展示位次差（该专业组历年最低录取位次 - 考生位次），
// 冲稳保的档位标签本身才是主要信息，位次差是佐证这个档位的具体数字。
// 跟历年数据表格里"位次差"那一列用的是同一套措辞（靠前/靠后），保持
// 全站一致，不要各自发明一套文案。
function candDiffTxt(diff) {
  if (diff == null) return "—";
  return diff >= 0 ? `靠前 ${diff}` : `靠后 ${Math.abs(diff)}`;
}

function candCardHtml(c, idPrefix = "") {
  // idPrefix：discoveries 区块展示的候选，跟保底/稳妥/冲刺分组里的候选
  // 完全可能是同一个 group_id（信息差发现本来就是"从已经在tiers里的候选
  // 里再挑一遍值得特别提醒的"，不是另一批数据）——同一个 group_id 在
  // 页面上出现两张卡片时，如果 checkbox 的 id 不加区分，会撞出重复的
  // DOM id（<label for=...> 关联失效，点了这张卡片的复选框标签可能
  // 触发另一张卡片），所以调用方渲染不同区块时传不同的 idPrefix。
  const checked = state.selectedGroupIds.has(c.group_id) ? "checked" : "";
  const histRows = c.history.map((h) => {
    const diffTxt = candDiffTxt(h.rank_diff);
    const diffCls = h.rank_diff == null ? "" : (h.rank_diff >= 0 ? "diff-pos" : "diff-neg");
    return `<tr><td>${h.year}</td><td>${h.plan_count ?? "—"}</td><td>${h.min_score ?? "—"} / ${h.min_rank ?? "—"}</td><td class="${diffCls}">${diffTxt}</td></tr>`;
  }).join("");

  // school_tier 是"好学校优先展示"这次新加的学校质量参考数据（985/211/
  // 双一流/普通本科/专科），school_tier 为 null 说明参考表里查不到这所
  // 学校（不代表它不好，只是没有数据），不展示这个标签、不能瞎猜一个档次。
  const qualityBadge = c.school_tier
    ? `<span class="tag tag-quality">${esc(c.school_tier)}${c.school_overall_rank ? ` · 排名${c.school_overall_rank}` : ""}</span>`
    : "";

  return `<div class="cand-card ${checked ? "selected" : ""}" data-id="${c.group_id}">
    <div class="cand-check-row">
      <input type="checkbox" id="chk_${idPrefix}${c.group_id}" ${checked}>
      <label for="chk_${idPrefix}${c.group_id}">加入方案</label>
    </div>
    <div class="cand-top">
      <div>
        <div class="cand-name">${esc(c.university)}</div>
        <div class="cand-loc">${esc(c.city)} · ${esc(c.group_name || "")}</div>
        ${c.major_keywords && c.major_keywords.length
          ? `<div class="cand-majors">专业方向：${c.major_keywords.map(esc).join("、")}</div>` : ""}
      </div>
      <div class="prob-badge ${CAT_CLASS[c.category] || "prob-wen"}">
        <div class="pct">${esc(c.category_label || "待计算")}</div>
        <div class="lbl">${candDiffTxt(c.diff)}</div>
        <div class="sub">位次差</div>
      </div>
    </div>
    <div class="tag-row">${qualityBadge}${c.tags.map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</div>
    <div class="stat-row">
      <div class="stat"><span class="k">招生代码</span><span class="v">${c.code || "—"}</span></div>
      <div class="stat"><span class="k">选科要求</span><span class="v">${c.subjects_required.join("+")}</span></div>
      <div class="stat"><span class="k">学费</span><span class="v">${c.tuition != null ? c.tuition + "元/年" : "—"}</span></div>
    </div>
    <table class="hist"><thead><tr><th>年份</th><th>计划</th><th>最低分/位次</th><th>位次差</th></tr></thead>
      <tbody>${histRows}</tbody></table>
    ${c.rank_equivalent_note
      ? `<div class="rank-equiv-note">${esc(c.rank_equivalent_note)}</div>` : ""}
    ${c.why_recommend ? `<div class="why-box"><b>为什么推荐：</b>${esc(c.why_recommend)}</div>` : ""}
  </div>`;
}

function wireCandCardCheckboxes(container) {
  Array.from(container.querySelectorAll(".cand-card input[type=checkbox]")).forEach((cb) => {
    cb.addEventListener("change", () => {
      const card = cb.closest(".cand-card");
      const gid = card.getAttribute("data-id");
      if (cb.checked) { state.selectedGroupIds.add(gid); card.classList.add("selected"); }
      else { state.selectedGroupIds.delete(gid); card.classList.remove("selected"); }
      updateConfirmBtn();
    });
  });
}

function renderCandidates() {
  // 真实撞到的问题：光按位次差排序、加个数量上限还不够——排在最前面的
  // 经常是数据凑巧、但学校本身很普通的候选（比如潍坊科技学院那个案例），
  // 用户明确要的是"只看 10 所左右，分保底/稳妥/冲刺三档展示，档内好学校
  // 优先，其余折叠"。这个精选逻辑（分档、档内按学校质量排序、按比例分配
  // 名额）在后端 candidate_curation.curate() 里做好了，前端只管渲染
  // state.searchResult.curated 这个结构，不再自己对 results 做二次处理。
  const curated = state.searchResult ? state.searchResult.curated : null;
  const groupsEl = $("#curatedGroups");
  const toggleBtn = $("#toggleCollapsedBtn");
  const collapsedGrid = $("#collapsedGrid");

  const discoveriesEl = $("#discoveriesSection");

  if (!curated) {
    groupsEl.innerHTML = "";
    toggleBtn.style.display = "none";
    collapsedGrid.innerHTML = "";
    $("#candCountBadge").textContent = "";
    $("#confirmPlanBtn").disabled = true;
    $("#confirmPlanBtn").textContent = "确认方案（0）";
    discoveriesEl.style.display = "none";
    discoveriesEl.innerHTML = "";
    return;
  }

  // "信息差发现"：够格985/211/双一流、但学校所在城市不是热门省会/一线
  // 城市的候选（见后端 candidate_curation.find_discoveries 的说明）——
  // 真实痛点是考生可能压根没听说过这所学校、直接跳过了，哪怕它已经在
  // 下面的保底/稳妥/冲刺分组里出现过，也单独用一块醒目区域再提一次。
  if (curated.discoveries && curated.discoveries.length) {
    discoveriesEl.style.display = "";
    discoveriesEl.innerHTML = `<div class="discoveries-box">
      <h4>💡 你可能没注意到的好学校</h4>
      <p class="discoveries-hint">这几所是 985/211/双一流，但不在热门省会城市，容易被直接跳过</p>
      <div class="cand-grid">${curated.discoveries.map((c) => candCardHtml(c, "disc_")).join("")}</div>
    </div>`;
    wireCandCardCheckboxes(discoveriesEl);
  } else {
    discoveriesEl.style.display = "none";
    discoveriesEl.innerHTML = "";
  }

  const totalCurated = curated.groups.reduce((sum, g) => sum + g.candidates.length, 0);
  if (totalCurated === 0 && curated.collapsed_count === 0) {
    groupsEl.innerHTML = `<div class="empty">
      <div class="big">暂无符合条件的候选</div>
      可能是条件过于严格（学费/城市/专业关键词），可以回到对话里放宽条件重新查询。
    </div>`;
    toggleBtn.style.display = "none";
    collapsedGrid.innerHTML = "";
    $("#candCountBadge").textContent = "";
    $("#confirmPlanBtn").disabled = true;
    $("#confirmPlanBtn").textContent = "确认方案（0）";
    return;
  }

  $("#candCountBadge").textContent = `优选展示 ${totalCurated} 所（保底/稳妥/冲刺各挑好学校优先），`
    + `另有 ${curated.collapsed_count} 所折叠未展示`;

  groupsEl.innerHTML = curated.groups.map((g) => {
    if (g.total_in_category === 0) {
      return `<div class="curated-group">
        <h4>${esc(g.category_label)}<span class="curated-group-count">该档暂无通过硬性规则的候选</span></h4>
      </div>`;
    }
    const note = g.total_in_category > g.candidates.length
      ? `<span class="curated-group-count">共 ${g.total_in_category} 所，优选展示 ${g.candidates.length} 所好学校，其余在下方折叠区</span>`
      : `<span class="curated-group-count">共 ${g.candidates.length} 所</span>`;
    return `<div class="curated-group">
      <h4>${esc(g.category_label)}${note}</h4>
      <div class="cand-grid">${g.candidates.map(candCardHtml).join("")}</div>
    </div>`;
  }).join("");
  wireCandCardCheckboxes(groupsEl);

  if (curated.collapsed_count > 0) {
    toggleBtn.style.display = "";
    toggleBtn.textContent = state.collapsedExpanded
      ? "收起其余候选"
      : `展开查看其余 ${curated.collapsed_count} 所候选（含极冲/慎选和未入选优先展示的候选）`;
    collapsedGrid.style.display = state.collapsedExpanded ? "" : "none";
    if (state.collapsedExpanded) {
      collapsedGrid.innerHTML = curated.collapsed.map(candCardHtml).join("");
      wireCandCardCheckboxes(collapsedGrid);
    }
  } else {
    toggleBtn.style.display = "none";
    collapsedGrid.innerHTML = "";
  }

  updateConfirmBtn();
}

function updateConfirmBtn() {
  const n = state.selectedGroupIds.size;
  const btn = $("#confirmPlanBtn");
  btn.textContent = `确认方案（${n}）`;
  btn.disabled = n === 0;
}

/* ---------------- plan ---------------- */
async function confirmPlanAction() {
  if (!state.profileId || state.selectedGroupIds.size === 0) return;
  const btn = $("#confirmPlanBtn");
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = "提交中…";
  try {
    const plan = await api.confirmPlan(state.profileId, Array.from(state.selectedGroupIds));
    state.currentPlan = plan;
    document.getElementById("stepPlan").style.display = "";
    renderPlan();
    document.getElementById("stepPlan").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    addMsg("err", `确认方案失败：${e.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

function renderPlan() {
  const plan = state.currentPlan;
  if (!plan) return;

  $("#versionLabel").textContent = `版本 v${plan.version_n}（plan_id: ${plan.id}）`;
  $("#exportLink").href = api.getExportUrl(plan.id);

  $("#planCandList").innerHTML = plan.candidates.map((c) => `
    <div class="plan-cand-row">
      <div>
        <div class="name">${esc(c.university)}</div>
        <div class="meta">${esc(c.city)} · ${esc(c.group_name || "")} · 招生代码 ${c.code || "—"}</div>
      </div>
      <div class="meta">${esc(c.category_label || "待计算")} · ${candDiffTxt(c.rank_diff)}</div>
    </div>
  `).join("");

  const diffArea = $("#diffArea");
  if (!plan.diff.length) {
    diffArea.innerHTML = plan.version_n === 1
      ? `<div class="diff-box">这是第一版方案，修改条件后重新确认，这里会显示与上一版本的差异。</div>`
      : `<div class="diff-box">本次修改未改变候选列表。</div>`;
  } else {
    diffArea.innerHTML = `<div class="diff-box">` + plan.diff.map((d) => {
      const cls = d.kind === "add" ? "add" : (d.kind === "remove" ? "remove" : "chg");
      return `<div class="diff-line ${cls}">${esc(d.detail)}</div>`;
    }).join("") + `</div>`;
  }
}

/* ---------------- reset ---------------- */
function resetAll() {
  localStorage.removeItem(STORAGE_KEY);
  state.profileId = null; state.profile = null; state.missing = []; state.conflicts = [];
  state.ready = false; state.searchResult = null; state.selectedGroupIds = new Set(); state.currentPlan = null; state.collapsedExpanded = false; state.ruleAuditExpanded = false;
  $("#chatLog").innerHTML = "";
  addMsg("agent", "已重新开始。请告诉我你的省份、类别、分数或位次、选科组合和想了解的专业方向。");
  renderFieldPanel();
  ["stepRules", "stepCandidates", "stepPlan"].forEach((id) => { document.getElementById(id).style.display = "none"; });
}

/* ---------------- init ---------------- */
async function checkBackend() {
  const pill = $("#backendPill");
  try {
    const health = await api.checkHealth();
    if (health.database === "connected") {
      pill.textContent = "● 后端已连接"; pill.className = "pill ok";
    } else {
      pill.textContent = "● 后端在线，数据库未连接"; pill.className = "pill err";
    }
  } catch (e) {
    pill.textContent = "● 后端连不上"; pill.className = "pill err";
    addMsg("err", `连不上后端（${api.getApiBase()}）：${e.message}。请确认后端服务已启动。`);
  }
}

/* ---------------- auth ---------------- */
let authMode = "login"; // "login" | "register"

function showApp(user) {
  $("#authOverlay").style.display = "none";
  $("#appContent").style.display = "";
  $("#userPill").style.display = "";
  $("#userPill").textContent = `● ${user.email}`;
  $("#logoutBtn").style.display = "";
}

function showAuthOverlay() {
  $("#authOverlay").style.display = "";
  $("#appContent").style.display = "none";
  $("#userPill").style.display = "none";
  $("#logoutBtn").style.display = "none";
}

async function handleAuthSubmit(e) {
  e.preventDefault();
  const email = $("#authEmail").value.trim();
  const password = $("#authPassword").value;
  const errBox = $("#authError");
  errBox.style.display = "none";

  const btn = $("#authSubmitBtn");
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = authMode === "login" ? "登录中…" : "注册中…";

  try {
    const user = authMode === "login" ? await api.login(email, password) : await api.register(email, password);
    // 换了账号（或者第一次登录），不复用浏览器里可能留存的上一个账号的 profile_id——
    // 那个 profile 大概率不属于这次登录的账号，继续用只会立刻收到后端的 404。
    localStorage.removeItem(STORAGE_KEY);
    state.profileId = null; state.profile = null; state.missing = []; state.conflicts = [];
    state.ready = false; state.searchResult = null; state.selectedGroupIds = new Set(); state.currentPlan = null; state.collapsedExpanded = false; state.ruleAuditExpanded = false;
    $("#chatLog").innerHTML = "";
    showApp(user);
    addMsg("agent", "你好，我是志愿领航 Agent。请告诉我你的省份、类别（物理/历史）、分数或位次、选科组合，以及想了解的专业方向。");
    renderFieldPanel();
    await checkBackend();
  } catch (err) {
    errBox.style.display = "block";
    errBox.innerHTML = `<b>${authMode === "login" ? "登录失败" : "注册失败"}</b><span>${esc(err.message)}</span>`;
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

function handleLogout() {
  api.logout();
  showAuthOverlay();
  $("#authEmail").value = "";
  $("#authPassword").value = "";
}

function bindAuthEvents() {
  $("#authForm").addEventListener("submit", handleAuthSubmit);
  $("#logoutBtn").addEventListener("click", handleLogout);
  $all("#authModeSeg button").forEach((b) => {
    b.addEventListener("click", () => {
      $all("#authModeSeg button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      authMode = b.getAttribute("data-mode");
      $("#authSubmitBtn").textContent = authMode === "login" ? "登录" : "注册";
      $("#authError").style.display = "none";
    });
  });
  // 后端返回 401（token 失效/过期）时，api.js 统一在这里把用户"踢"回登录页，
  // 不用在每个业务调用点各自判断该不该跳转。
  api.setUnauthorizedHandler(() => {
    showAuthOverlay();
    $("#authError").innerHTML = `<b>登录已过期</b><span>请重新登录。</span>`;
    $("#authError").style.display = "block";
  });
}

function bindEvents() {
  $("#sendBtn").addEventListener("click", handleSend);
  $("#chatInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); }
  });
  $("#fillDemoBtn").addEventListener("click", () => {
    $("#chatInput").value = "我是江苏物理类考生，2026届，637分，位次5200，选科物理+化学，想找计算机相关专业，优先考虑省内城市。";
    $("#chatInput").focus();
  });
  $("#resetBtn").addEventListener("click", resetAll);
  $("#searchBtn").addEventListener("click", runSearch);
  $("#confirmPlanBtn").addEventListener("click", confirmPlanAction);
  $("#toggleCollapsedBtn").addEventListener("click", () => {
    state.collapsedExpanded = !state.collapsedExpanded;
    renderCandidates();
  });
  $("#toggleRuleAuditBtn").addEventListener("click", () => {
    state.ruleAuditExpanded = !state.ruleAuditExpanded;
    renderRuleAuditToggle();
  });
}

function renderRuleAuditToggle() {
  // 资格核验详情默认收起——日常报考不需要逐条翻这份原始核验记录，
  // 精选候选（保底/稳妥/冲刺）已经是筛选、排好序的结果，这份核验列表
  // 是给想深挖"某条候选具体因为哪条规则没通过"的用户看的，不该抢占
  // 查询候选后的第一屏。
  $("#ruleList").style.display = state.ruleAuditExpanded ? "" : "none";
  $("#toggleRuleAuditBtn").textContent = state.ruleAuditExpanded ? "收起资格核验详情" : "展开资格核验详情";
}

async function init() {
  $("#apiBaseLabel").textContent = `后端地址：${api.getApiBase()}`;
  bindAuthEvents();
  bindEvents();

  if (!api.isLoggedIn()) {
    showAuthOverlay();
    return;
  }

  // 本地存了 token，但没验证过是否还有效（可能已过期/被后端认为无效）——
  // 用 /api/auth/me 顺带验证一下，无效的话直接走 401 处理逻辑跳回登录页，
  // 不要让用户在一个"看起来登录了、其实一发消息就报错"的界面里卡住。
  let user;
  try {
    user = await api.getMe();
  } catch (e) {
    showAuthOverlay();
    return;
  }
  showApp(user);
  await checkBackend();

  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved) {
    state.profileId = Number(saved);
    addMsg("sys", `已恢复上次的会话（profile_id: ${state.profileId}），继续对话即可，或点"重新开始"清空。`);
  } else {
    addMsg("agent", "你好，我是志愿领航 Agent。请告诉我你的省份、类别（物理/历史）、分数或位次、选科组合，以及想了解的专业方向。");
  }
  renderFieldPanel();
}

init();
