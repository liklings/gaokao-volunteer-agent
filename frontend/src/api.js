// 所有对后端的调用集中在这一个文件里，UI 代码（main.js）不直接碰 fetch。
// 这样以后要改 API 路径、加认证头之类，只用改这一个文件——这次加登录认证头，
// 就是照这个设计说的做：只改了这一个文件，main.js 里所有调用 api.* 的地方
// 不用跟着改一行。

const DEFAULT_BASE = "http://localhost:8000";
const TOKEN_STORAGE_KEY = "gaokao_auth_token";

function getApiBase() {
  return (typeof window !== "undefined" && window.API_BASE_URL) || DEFAULT_BASE;
}

/* ---------------- 认证 token 管理 ---------------- */
// token 存 localStorage，不是内存变量——这样刷新页面/关掉浏览器重开都不用重新登录，
// 跟原来 profile_id 用 localStorage 记"上次会话"是同一个思路。

function getToken() {
  return typeof window !== "undefined" ? localStorage.getItem(TOKEN_STORAGE_KEY) : null;
}

function setToken(token) {
  localStorage.setItem(TOKEN_STORAGE_KEY, token);
}

function clearToken() {
  localStorage.removeItem(TOKEN_STORAGE_KEY);
}

function isLoggedIn() {
  return !!getToken();
}

// 401 是"这次请求需要登录/token 已失效"的统一信号：不管是从来没登录过，
// 还是登录过但 token 过期了，都在这里被拦下清掉本地 token，让上层 UI
// 统一按"需要登录"处理，不用在每个业务调用点各自判断一遍。
let onUnauthorized = null;
function setUnauthorizedHandler(handler) {
  onUnauthorized = handler;
}

async function apiFetch(path, options = {}, { skipAuthRedirect = false } = {}) {
  const base = getApiBase();
  const token = getToken();
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const resp = await fetch(base + path, { headers, ...options });

  let body = null;
  const text = await resp.text();
  if (text) {
    try { body = JSON.parse(text); } catch (e) { body = text; }
  }

  if (!resp.ok) {
    const detail = (body && typeof body === "object" && body.detail) ? body.detail : `请求失败：HTTP ${resp.status}`;
    const err = new Error(detail);
    err.status = resp.status;
    err.body = body;
    if (resp.status === 401 && !skipAuthRedirect) {
      clearToken();
      if (onUnauthorized) onUnauthorized();
    }
    throw err;
  }
  return body;
}

export async function checkHealth() {
  return apiFetch("/api/health");
}

/* ---------------- 认证接口 ---------------- */
export async function register(email, password) {
  // 注册/登录接口本身不需要带 token（这时候还没有），出错时也不应该触发
  // "跳回登录页"这个逻辑（本来就在登录页），所以 skipAuthRedirect: true。
  const result = await apiFetch(
    "/api/auth/register",
    { method: "POST", body: JSON.stringify({ email, password }) },
    { skipAuthRedirect: true },
  );
  setToken(result.access_token);
  return result.user;
}

export async function login(email, password) {
  const result = await apiFetch(
    "/api/auth/login",
    { method: "POST", body: JSON.stringify({ email, password }) },
    { skipAuthRedirect: true },
  );
  setToken(result.access_token);
  return result.user;
}

export function logout() {
  clearToken();
}

export async function getMe() {
  return apiFetch("/api/auth/me");
}

export async function sendChatMessage(profileId, message) {
  return apiFetch("/api/chat", {
    method: "POST",
    body: JSON.stringify({ profile_id: profileId, message }),
  });
}

export async function searchCandidates(profile) {
  return apiFetch("/api/candidates/search", {
    method: "POST",
    body: JSON.stringify(profile),
  });
}

export async function confirmPlan(profileId, groupIds) {
  return apiFetch("/api/plans", {
    method: "POST",
    body: JSON.stringify({ profile_id: profileId, group_ids: groupIds }),
  });
}

export async function getPlan(planId) {
  return apiFetch(`/api/plans/${planId}`);
}

export function getExportUrl(planId) {
  // 导出走的是浏览器直接打开的链接（<a href target=_blank>），不经过 apiFetch，
  // 没法带自定义 Authorization 头，所以这里把 token 作为查询参数带上；
  // 后端 export_plan 这个接口需要相应地支持从 query string 读 token
  // （见 backend 那次改动里对 /api/plans/{id}/export 的调整）。
  const token = getToken();
  const url = `${getApiBase()}/api/plans/${planId}/export`;
  return token ? `${url}?token=${encodeURIComponent(token)}` : url;
}

export { getApiBase, isLoggedIn, setUnauthorizedHandler };
