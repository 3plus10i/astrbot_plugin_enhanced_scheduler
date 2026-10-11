/* 增强计划任务插件前端逻辑 */
async function init() {
    const bridge = window.AstrBotPluginPage;
    try { await bridge.ready(); } catch (e) { console.error("bridge 连接失败:", e); }

    let allTasks = {};
    let sessionsList = [];
    let validateTimer = null;
    let validateRevision = 0;
    let savedConfig = null;
    const LOGS_PAGE_SIZE = 20;
    let logsPage = 0;

    // ── 工具函数 ──
    function $(id) { return document.getElementById(id); }
    function escapeHtml(s) {
        if (s === null || s === undefined) return "";
        return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
    }
    function showToast(msg, isError) {
        const t = $("toast");
        t.textContent = msg;
        t.classList.toggle("error", !!isError);
        t.classList.add("show");
        setTimeout(() => t.classList.remove("show"), 3000);
    }
    function fmtTs(ts) {
        if (!ts) return "—";
        const d = new Date(ts * 1000);
        if (isNaN(d.getTime())) return "—";
        const p = n => String(n).padStart(2, "0");
        return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    }
    // 当前自然日 0 点，格式化为 datetime-local 所需 YYYY-MM-DDTHH:MM
    function defaultBaseTime() {
        const d = new Date();
        d.setHours(0, 0, 0, 0);
        const p = n => String(n).padStart(2, "0");
        return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}T00:00`;
    }
    // 后端存的 base_time 可能是 YYYY-MM-DDTHH:MM:SS 或带空格，统一截断为 datetime-local 的 YYYY-MM-DDTHH:MM
    function toDatetimeLocal(iso) {
        if (!iso) return "";
        const s = String(iso).trim().replace(" ", "T");
        const m = s.match(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})/);
        return m ? m[1] : s;
    }

    // ── Tab 切换 ──
    document.querySelectorAll(".tab-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
            document.querySelectorAll(".tab-view").forEach(v => v.classList.remove("active"));
            btn.classList.add("active");
            const tab = btn.getAttribute("data-tab");
            document.querySelectorAll(".tab-btn").forEach(b => {
                if (b === btn) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
            });
            $("view-" + tab).classList.add("active");
            if (tab === "logs") loadRunLogs(logsPage);
            if (tab === "llm" && !llmLoaded) loadLlmLogs(0);
        });
    });

    // ── 加载数据 ──
    async function loadData() {
        try {
            const res = await bridge.apiGet("get_data", {});
            if (!res || res.status !== "success") { showToast("获取数据失败", true); return; }
            allTasks = res.tasks || {};
            renderTasks();
            renderConfig(res.config || {});
        } catch (e) {
            showToast("加载失败: " + e.message, true);
        }
    }
    async function loadSessions() {
        try {
            const res = await bridge.apiGet("get_sessions", {});
            if (res && res.status === "success") sessionsList = res.sessions || [];
        } catch (e) { /* 静默 */ }
    }

    function renderTasks() {
        closeTaskMenu();
        const body = $("tasks-body");
        const keys = Object.keys(allTasks);
        $("task-count").textContent = `${keys.filter(k => allTasks[k].enabled).length} 项启用 / 共 ${keys.length} 项`;
        if (!keys.length) {
            body.innerHTML = `<tr><td colspan="6" class="table-empty">暂无计划任务，点击右上角新建。</td></tr>`;
            return;
        }
        body.innerHTML = keys.map(k => {
            const t = allTasks[k];
            const trgList = Array.isArray(t.triggers) ? t.triggers.filter(tr => tr && typeof tr === "object") : [];
            const labels = {interval: "周期", cron: "cron", random: "随机", window: "区间", cooldown: "冷却"};
            const trigSummary = [...new Set(trgList.map(tr => labels[tr.type] || tr.type))].map(escapeHtml).join("，");
            const ct = t.content || {};
            const mode = ct.mode || (ct.use_llm ? "conversation" : "fixed");
            const hasText = !!(ct.text && String(ct.text).trim());
            const modeCN = {fixed:"固定文本", standalone:"独立 AI", conversation:"对话 AI"}[mode] || mode;
            const nf = t._next_fire;
            const runtime = t._runtime || {};
            const stateText = {waiting:"就绪", running:"执行中", faulted:"调度故障", disabled:"已停用", needs_configuration:"需要重新配置"}[runtime.state] || (t.enabled ? "就绪" : "已停用");
            const stateClass = {faulted:"badge-failed", needs_configuration:"badge-warning", running:"badge-running"}[runtime.state] || (t.enabled ? "badge-active" : "badge-inactive");
            return `
                <tr>
                    <td><button type="button" class="task-name edit-task-btn" data-id="${escapeHtml(k)}">${escapeHtml(t.name)}</button><div class="cell-secondary task-target" title="${escapeHtml(t.target || '')}">${escapeHtml(t.target || "未指定发送对象")}</div></td>
                    <td class="cell-wrap"><span class="badge ${stateClass}">${escapeHtml(stateText)}</span>${runtime.error ? `<div class="form-hint row-error">${escapeHtml(runtime.error)}</div>` : ''}</td>
                    <td class="cell-wrap">${trigSummary || '—'}</td>
                    <td class="next-check">${nf ? `<time class="tabular">${fmtTs(nf).slice(0, 16)}</time><div class="cell-secondary next-relative" data-next-fire="${Number(nf)}">${remainingTime(nf)}</div>` : '—'}</td>
                    <td>${hasText ? escapeHtml(modeCN) : '空动作'}</td>
                    <td><div class="actions-cell">
                        <button type="button" class="text-btn edit-task-btn" data-id="${escapeHtml(k)}">编辑</button>
                        <button type="button" class="icon-btn more-task-btn" data-id="${escapeHtml(k)}" aria-label="${escapeHtml(t.name)}的更多操作" aria-expanded="false">⋯</button>
                    </div></td>
                </tr>`;
        }).join("");
        attachTaskRowListeners();
    }
    function remainingTime(ts) {
        const total = Math.max(0, Math.ceil((Number(ts) * 1000 - Date.now()) / 60000));
        if (!total) return "即将检查";
        const d = Math.floor(total / 1440), h = Math.floor(total % 1440 / 60), m = total % 60;
        return `还剩 ${d ? d + "天" : ""}${h ? h + "时" : ""}${m}分`;
    }

    async function loadRunLogs(page) {
        if (typeof page === "number") logsPage = page;
        const body = $("logs-body");
        let res;
        try {
            res = await bridge.apiGet("get_run_logs", { limit: LOGS_PAGE_SIZE, offset: logsPage * LOGS_PAGE_SIZE });
        } catch (e) {
            showToast("加载运行日志失败: " + e.message, true);
            return;
        }
        if (!res || res.status !== "success") { showToast("加载运行日志失败", true); return; }
        const total = res.total || 0;
        const pages = Math.max(1, Math.ceil(total / LOGS_PAGE_SIZE));
        // 日志被裁剪后当前页可能越界，回到最后一页
        if (logsPage > pages - 1) return loadRunLogs(pages - 1);
        const entries = res.entries || [];
        if (!entries.length) {
            body.innerHTML = `<tr><td colspan="5" class="table-empty">暂无日志。任务触发后会在此显示最近记录。</td></tr>`;
            $("logs-pager").innerHTML = "";
            return;
        }
        body.innerHTML = entries.map(l => {
            const tof = l.trigger_tof || {};
            const tofStr = Object.keys(tof).map(id => `#${id} ${tof[id] ? '✓' : '✗'}`).join("，");
            const triggerInfo = l.source === "manual" ? "手动触发，跳过触发器检查。" : `${tofStr ? tofStr + "；" : ""}${l.logic_result ? "条件全部通过。" : "条件未全部满足。"}`;
            const actionCN = {send_llm:"AI 发送",send_fixed:"发送文本",noop:"空动作",partial:"部分失败",skipped:"跳过",failed:"失败"}[l.action] || l.action;
            const actionBadge = ["skipped", "noop"].includes(l.action) ? "badge-skipped" : (l.action === "failed" || l.action === "partial" ? "badge-failed" : "badge-active");
            const detail = l.detail || "";
            // Old run logs remain readable; new records have a single target_result.
            const results = l.target_result ? [l.target_result] : (l.targets_result || []);
            const targetInfo = results.map(tr => `发送对象：${tr.umo || "—"}，${tr.ok ? '成功' : '失败' + (tr.error ? '：' + tr.error : '')}。`).join(" ");
            return `<tr>
                <td><time class="log-time">${fmtTs(l.time)}</time></td>
                <td class="cell-wrap">${escapeHtml(l.task_name)}</td>
                <td class="cell-wrap">${escapeHtml(triggerInfo)}</td>
                <td><span class="badge ${actionBadge}">${escapeHtml(actionCN)}</span></td>
                <td class="log-detail">${escapeHtml(detail)} ${escapeHtml(targetInfo)}</td>
            </tr>`;
        }).join("");
        renderLogsPager(pages, total);
    }

    function renderLogsPager(pages, total) {
        const pager = $("logs-pager");
        pager.innerHTML = `
            <button class="btn btn-secondary btn-sm" id="logs-page-prev" ${logsPage <= 0 ? "disabled" : ""}>上一页</button>
            <span class="page-info">第 ${logsPage + 1} / ${pages} 页 / 共 ${total} 条</span>
            <button class="btn btn-secondary btn-sm" id="logs-page-next" ${logsPage >= pages - 1 ? "disabled" : ""}>下一页</button>`;
        $("logs-page-prev").addEventListener("click", () => {
            if (logsPage > 0) loadRunLogs(logsPage - 1);
        });
        $("logs-page-next").addEventListener("click", () => {
            if (logsPage < pages - 1) loadRunLogs(logsPage + 1);
        });
    }

    function renderConfig(cfg) {
        if (savedConfig && configIsDirty()) return;
        $("config-log-retention").value = cfg.log_retention != null ? cfg.log_retention : 100;
        $("config-llm-timeout").value = cfg.llm_timeout != null ? cfg.llm_timeout : 60;
        $("config-llm-log-retention").value = cfg.llm_log_retention != null ? cfg.llm_log_retention : 10;
        savedConfig = configValues();
        updateConfigState();
    }
    function configValues() {
        return {log_retention: $("config-log-retention").value, llm_timeout: $("config-llm-timeout").value, llm_log_retention: $("config-llm-log-retention").value};
    }
    function configIsDirty() {
        const values = configValues();
        return savedConfig && Object.keys(values).some(key => String(values[key]) !== String(savedConfig[key]));
    }
    function updateConfigState() {
        const dirty = !!configIsDirty();
        $("save-config-btn").classList.toggle("btn-primary", dirty);
        $("save-config-btn").classList.toggle("btn-secondary", !dirty);
        $("config-dirty-hint").hidden = !dirty;
    }
    ["config-log-retention", "config-llm-timeout", "config-llm-log-retention"].forEach(id => $(id).addEventListener("input", updateConfigState));

    // ── LLM 调用日志（分页骨架 + 逐条加载；正文展开时才渲染，图片点击才取） ──
    let llmLoaded = false;
    let llmPage = 0;
    let llmTotal = 0;
    let llmLoadToken = 0;  // 切页/刷新时自增，作废在途请求
    const LLM_PAGE_SIZE = 10;
    const llmDetailCache = new Map();  // 记录文件名 -> 正文
    const llmImageCache = new Map();   // 图片文件名 -> data URL
    // 后端抽离图片后写入的占位标记：[img:<md5>.<ext>:<字节数>]
    const IMG_MARK_RE = /\[img:([0-9a-f]{32})\.([a-z0-9]{2,5}):(\d+)\]/g;

    function fmtSize(n) {
        n = Number(n) || 0;
        if (n < 1024) return n + " B";
        if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
        return (n / 1048576).toFixed(2) + " MB";
    }
    function fmtDuration(ms) {
        return (Math.round((Number(ms) || 0) / 100) / 10) + "s";
    }
    function yieldToUi() { return new Promise(r => setTimeout(r, 0)); }
    function showLlmLoading() {
        $("llm-log-list").innerHTML = `<div class="llm-loading"><span class="spinner"></span>正在加载…</div>`;
        $("llm-pager").innerHTML = "";
    }
    async function loadLlmLogs(page) {
        if (typeof page === "number") llmPage = page;
        const token = ++llmLoadToken;
        showLlmLoading();
        let res;
        try {
            res = await bridge.apiGet("get_llm_logs", { limit: LLM_PAGE_SIZE, offset: llmPage * LLM_PAGE_SIZE });
        } catch (e) {
            if (token !== llmLoadToken) return;
            $("llm-log-list").innerHTML = `<div class="empty-hint">加载失败：${escapeHtml(e.message)}</div>`;
            showToast("加载 LLM 日志失败: " + e.message, true);
            return;
        }
        if (token !== llmLoadToken) return;
        if (!res || res.status !== "success") {
            $("llm-log-list").innerHTML = `<div class="empty-hint">获取 LLM 日志失败。</div>`;
            showToast("获取 LLM 日志失败", true);
            return;
        }
        llmTotal = res.total || 0;
        llmLoaded = true;
        // 记录被裁剪后当前页可能越界，回到最后一页
        const maxPage = Math.max(0, Math.ceil(llmTotal / LLM_PAGE_SIZE) - 1);
        if (llmPage > maxPage) return loadLlmLogs(maxPage);
        const entries = res.entries || [];
        if (!entries.length) {
            $("llm-log-list").innerHTML = `<div class="empty-hint">暂无记录。</div>`;
            renderLlmPager();
            return;
        }
        const cards = renderLlmPlaceholders(entries);
        renderLlmPager();
        // 串行逐条取正文：到达一条即就绪一条，可展开一条
        for (const card of cards) {
            if (token !== llmLoadToken) return;
            await loadLlmCard(card, token);
            await yieldToUi();
        }
    }
    // 一次性占位整页折叠条，正文区先显示加载占位
    function renderLlmPlaceholders(entries) {
        const list = $("llm-log-list");
        list.innerHTML = entries.map(m => {
            const modeCN = m.mode === "conversation" ? "对话AI" : "独立AI";
            const imgInfo = (m.images && m.images.length) ? ` / ${m.images.length}图 ${fmtSize(m.image_bytes)}` : "";
            return `
                <details class="llm-log-card">
                    <summary class="llm-log-summary">
                        <span class="llm-log-time">${escapeHtml(m.time || "")}</span>
                        <span class="llm-log-meta">
                            <span class="badge ${m.mode === "conversation" ? "badge-llm" : "badge-fixed"}">${modeCN}</span>
                            <span class="badge ${m.ok ? "badge-active" : "badge-failed"}">${m.ok ? "成功" : "失败"}</span>
                            <span>${escapeHtml(m.task_name || "")}</span>
                            <span>${escapeHtml(m.umo || "")}</span>
                            <span>${fmtSize(m.text_bytes)}${imgInfo}</span>
                            <span>${fmtDuration(m.duration_ms)}</span>
                        </span>
                        <span class="llm-log-preview">${escapeHtml(m.preview || "")}</span>
                        <span class="llm-log-status"><span class="spinner"></span></span>
                    </summary>
                    <div class="llm-log-body">
                        <div class="llm-log-toolbar">
                            <button type="button" class="btn btn-secondary btn-sm llm-format-toggle" hidden>格式化</button>
                        </div>
                        <pre class="llm-log-full llm-log-placeholder"><span class="spinner"></span>正在加载本条…</pre>
                    </div>
                </details>`;
        }).join("");
        const cards = Array.from(list.querySelectorAll(".llm-log-card"));
        cards.forEach((card, i) => {
            card._meta = entries[i];
            card._name = entries[i].name;
            // toggle 事件不冒泡，逐卡片绑定：首次展开时才把正文文本写入页面
            card.addEventListener("toggle", () => {
                if (card.open && card._entry && !card._rendered) renderLlmCardContent(card);
            });
        });
        return cards;
    }
    // 取单条正文并标记就绪（已展开则立即渲染）
    async function loadLlmCard(card, token) {
        const name = card._name;
        let body = llmDetailCache.get(name);
        if (!body) {
            try {
                const res = await bridge.apiGet("get_llm_log_detail", { name });
                if (token !== llmLoadToken) return;
                if (!res || res.status !== "success") throw new Error((res && res.message) || "加载失败");
                body = res.body;
                llmDetailCache.set(name, body);
            } catch (e) {
                if (token !== llmLoadToken) return;
                const pre = card.querySelector(".llm-log-full");
                pre.classList.remove("llm-log-placeholder");
                pre.textContent = "加载失败：" + e.message;
                setLlmStatus(card, "error", "失败");
                return;
            }
        }
        card._entry = body;
        setLlmStatus(card, "ready", "已就绪");
        const pre = card.querySelector(".llm-log-full");
        pre.classList.remove("llm-log-placeholder");
        pre.textContent = "";
        const btn = card.querySelector(".llm-format-toggle");
        if (btn) btn.hidden = false;
        if (card.open) renderLlmCardContent(card);
    }
    function setLlmStatus(card, state, text) {
        const el = card.querySelector(".llm-log-status");
        if (!el) return;
        el.className = "llm-log-status " + state;
        el.textContent = text;
    }
    function renderLlmPager() {
        const pager = $("llm-pager");
        if (!pager) return;
        const pages = Math.max(1, Math.ceil(llmTotal / LLM_PAGE_SIZE));
        const page = Math.min(llmPage, pages - 1);
        pager.innerHTML = `
            <button class="btn btn-secondary btn-sm" id="llm-page-prev" ${page <= 0 ? "disabled" : ""}>上一页</button>
            <span class="page-info">第 ${page + 1} / ${pages} 页 / 共 ${llmTotal} 条</span>
            <button class="btn btn-secondary btn-sm" id="llm-page-next" ${page >= pages - 1 ? "disabled" : ""}>下一页</button>`;
        const prev = $("llm-page-prev"), next = $("llm-page-next");
        if (prev) prev.addEventListener("click", () => { if (page > 0) loadLlmLogs(page - 1); });
        if (next) next.addEventListener("click", () => { if (page < pages - 1) loadLlmLogs(page + 1); });
    }
    // 把正文里的图片占位标记折叠成 chip，返回 HTML 与图片引用列表。
    // 文件名/体积以记录首行元数据里的图片清单为准（标记文本仅作兜底），
    // 避免标记被误读时拼出不存在的文件名。
    function foldImages(text, metaImages) {
        const metas = metaImages || [];
        const imgList = [];
        let html = "", last = 0, m;
        IMG_MARK_RE.lastIndex = 0;
        while ((m = IMG_MARK_RE.exec(text)) !== null) {
            html += escapeHtml(text.slice(last, m.index));
            const md5 = m[1];
            const info = metas.find(i => i && i.md5 === md5);
            const name = (info && info.name) || `${md5}.${m[2]}`;
            const size = info && info.size != null ? info.size : (Number(m[3]) || 0);
            const ext = String(name).split(".").pop().toUpperCase();
            const idx = imgList.length;
            imgList.push({ name, size });
            html += `<span class="b64-chip" data-idx="${idx}" title="点击加载图片">图片 ${escapeHtml(ext)} / ${fmtSize(size)}</span>`;
            last = IMG_MARK_RE.lastIndex;
        }
        html += escapeHtml(text.slice(last));
        return { html, imgList };
    }
    // 按卡片当前格式渲染正文（首次展开或切换格式时调用）
    function renderLlmCardContent(card) {
        if (!card._entry) return;
        const text = card._formatted ? JSON.stringify(card._entry, null, 2) : JSON.stringify(card._entry);
        const { html, imgList } = foldImages(text, card._meta && card._meta.images);
        card._imgList = imgList;
        card._rendered = true;
        card.querySelector(".llm-log-full").innerHTML = html;
        const btn = card.querySelector(".llm-format-toggle");
        if (btn) btn.textContent = card._formatted ? "原始格式" : "将JSON格式化";
    }
    async function copyToClipboard(text) {
        try {
            if (navigator.clipboard && window.isSecureContext) {
                await navigator.clipboard.writeText(text);
                return true;
            }
        } catch (e) { /* 落回下面 execCommand */ }
        try {
            const ta = document.createElement("textarea");
            ta.value = text;
            ta.style.position = "fixed";
            ta.style.opacity = "0";
            document.body.appendChild(ta);
            ta.select();
            const ok = document.execCommand("copy");
            document.body.removeChild(ta);
            return ok;
        } catch (e) { return false; }
    }
    // 直接把 base64 data URL 渲染为图片（正则已限定字符集，无 XSS 风险）
    function showB64Popover(anchor, dataUrl) {
        const pop = $("b64-popover");
        pop._dataUrl = dataUrl;
        pop.innerHTML = `
            <img class="b64-img" src="${dataUrl}" alt="base64 图片" title="右键可保存图片">
            <div class="b64-actions">
                <button type="button" class="btn btn-secondary btn-sm b64-copy-btn">复制 Base64</button>
            </div>`;
        const img = pop.querySelector(".b64-img");
        if (img) img.addEventListener("error", () => {
            const err = document.createElement("div");
            err.className = "b64-img-error";
            err.textContent = "无法解析该 Base64 图片";
            img.replaceWith(err);
        });
        // 定位：触发元素偏下时显示在其上方，避免超出视口
        const r = anchor.getBoundingClientRect();
        pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 372)) + "px";
        if (r.bottom > window.innerHeight * 0.6) {
            pop.style.top = "auto";
            pop.style.bottom = (window.innerHeight - r.top + 6) + "px";
        } else {
            pop.style.bottom = "auto";
            pop.style.top = (r.bottom + 6) + "px";
        }
        pop.classList.add("show");
    }
    $("llm-log-list").addEventListener("click", async ev => {
        const chip = ev.target.closest(".b64-chip");
        if (chip) {
            const card = chip.closest(".llm-log-card");
            const idx = parseInt(chip.getAttribute("data-idx"), 10);
            const img = (card && card._imgList && card._imgList[idx]) || null;
            if (!img) { showToast("未找到图片内容", true); return; }
            // 图片按内容地址缓存：同一张图在任意记录中出现都只取一次
            if (!img._dataUrl) {
                chip.classList.add("loading");
                try {
                    let dataUrl = llmImageCache.get(img.name);
                    if (!dataUrl) {
                        const res = await bridge.apiGet("get_llm_image", { name: img.name });
                        if (!res || res.status !== "success") throw new Error((res && res.message) || "加载失败");
                        dataUrl = res.data_url;
                        llmImageCache.set(img.name, dataUrl);
                    }
                    img._dataUrl = dataUrl;
                } catch (e) {
                    showToast("图片加载失败：" + e.message, true);
                    return;
                } finally {
                    chip.classList.remove("loading");
                }
            }
            showB64Popover(chip, img._dataUrl);
            return;
        }
        const btn = ev.target.closest(".llm-format-toggle");
        if (btn) {
            const card = btn.closest(".llm-log-card");
            if (card && card._entry) {
                card._formatted = !card._formatted;
                renderLlmCardContent(card);
            }
        }
    });
    document.addEventListener("click", async ev => {
        const copyBtn = ev.target.closest(".b64-copy-btn");
        if (copyBtn) {
            const pop = $("b64-popover");
            const dataUrl = pop && pop._dataUrl;
            if (dataUrl && await copyToClipboard(dataUrl)) showToast("Base64 图片内容已复制");
            else showToast("复制失败，请手动选择复制", true);
            return;
        }
        if (ev.target.closest(".b64-chip") || ev.target.closest("#b64-popover")) return;
        const pop = $("b64-popover");
        if (pop) pop.classList.remove("show");
    });
    $("refresh-llm-btn").addEventListener("click", () => { llmDetailCache.clear(); loadLlmLogs(llmPage); });
    $("clear-llm-btn").addEventListener("click", () => {
        confirmMode = "clear-llm";
        $("confirm-modal-text").textContent = "确定要清空所有 LLM 调用日志吗？此操作不可撤销。";
        confirmModal.showModal();
    });

    function attachTaskRowListeners() {
        document.querySelectorAll(".edit-task-btn").forEach(b => b.addEventListener("click", () => openTaskModal("edit", b.getAttribute("data-id"))));
        document.querySelectorAll(".more-task-btn").forEach(b => b.addEventListener("click", () => openTaskMenu(b)));
    }
    let menuAnchor = null;
    function closeTaskMenu() {
        $("task-menu").hidden = true;
        if (menuAnchor) menuAnchor.setAttribute("aria-expanded", "false");
        menuAnchor = null;
    }
    function openTaskMenu(button) {
        const wasOpen = menuAnchor === button && !$("task-menu").hidden;
        closeTaskMenu();
        if (wasOpen) return;
        menuAnchor = button;
        button.setAttribute("aria-expanded", "true");
        const id = escapeHtml(button.dataset.id);
        const menu = $("task-menu");
        menu.innerHTML = `<button type="button" data-action="copy" data-id="${id}">复制任务</button><button type="button" data-action="trigger" data-id="${id}">触发一次</button><div class="menu-divider"></div><button type="button" class="danger-text" data-action="delete" data-id="${id}">删除任务</button>`;
        menu.hidden = false;
        const rect = button.getBoundingClientRect();
        menu.style.left = `${Math.max(12, Math.min(rect.right - menu.offsetWidth, window.innerWidth - menu.offsetWidth - 12))}px`;
        menu.style.top = `${Math.max(12, Math.min(rect.bottom + 5, window.innerHeight - menu.offsetHeight - 12))}px`;
        menu.querySelector("button").focus();
    }
    $("task-menu").addEventListener("click", e => {
        const button = e.target.closest("[data-action]");
        if (!button) return;
        const id = button.dataset.id;
        closeTaskMenu();
        if (button.dataset.action === "copy") copyTask(id);
        if (button.dataset.action === "trigger") triggerNow(id, button);
        if (button.dataset.action === "delete") confirmDelete(id);
    });
    document.addEventListener("click", e => { if (!e.target.closest("#task-menu, .more-task-btn")) closeTaskMenu(); });
    document.addEventListener("keydown", e => {
        if (e.key === "Escape" && menuAnchor) { const anchor = menuAnchor; closeTaskMenu(); anchor.focus(); e.preventDefault(); }
    });
    window.addEventListener("scroll", closeTaskMenu, true);
    window.addEventListener("resize", closeTaskMenu);

    // ── 任务编辑弹窗 ──
    const modal = $("task-modal");
    const ACTIVE_TYPES = ["interval", "cron"];
    const PASSIVE_TYPES = ["window", "random", "cooldown"];
    const TYPE_INFO = {
        interval: { kind: "active", label: "周期触发", desc: "从基准时间起，按固定间隔检查任务。" },
        cron: { kind: "active", label: "cron 触发", desc: "按日历时间检查任务。" },
        window: { kind: "passive", label: "时间区间", desc: "仅在所选星期与时间区间内通过检查。" },
        random: { kind: "passive", label: "随机条件", desc: "主动触发后，按设置的概率判断是否通过。" },
        cooldown: { kind: "passive", label: "冷却条件", desc: "距本任务上次成功达到指定时长后，条件通过。" },
    };
    const WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

    let triggers = [];            // [{id, type, config}]
    let selectedTriggerId = null;
    let nextTriggerId = 1;
    let lastValidate = { triggers: [], has_active: false, next_fire: null };
    let lastSuccessTime = 0;

    let editorTab = "basic";
    function chooseEditorTab(name) {
        editorTab = name;
        document.querySelectorAll("[data-editor-tab]").forEach(button => {
            const active = button.dataset.editorTab === name;
            button.setAttribute("aria-selected", String(active));
            button.tabIndex = active ? 0 : -1;
        });
        ["basic", "rules", "content"].forEach(tab => { $("editor-" + tab).hidden = tab !== name; });
        $("task-form-body").scrollTop = 0;
    }
    document.querySelectorAll("[data-editor-tab]").forEach(button => button.addEventListener("click", () => chooseEditorTab(button.dataset.editorTab)));
    $("editor-tabs").addEventListener("keydown", e => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
        const tabs = [...document.querySelectorAll("[data-editor-tab]")];
        const index = tabs.indexOf(document.activeElement);
        if (index < 0) return;
        e.preventDefault();
        const next = e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1 : (index + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
        chooseEditorTab(tabs[next].dataset.editorTab);
        tabs[next].focus();
    });
    function updateEditorName() {
        const name = $("task-name").value;
        $("editor-task-name").textContent = name;
        $("editor-task-name").hidden = !name;
        $("task-name-count").textContent = `${name.length} / 32`;
    }
    $("task-name").addEventListener("input", () => {
        $("task-name").value = $("task-name").value.slice(0, 32);
        updateEditorName();
    });
    function openModal() { modal.classList.add("active"); if (!modal.open) modal.showModal(); }
    function closeModal() {
        clearTimeout(validateTimer);
        ++validateRevision;
        modal.close();
        modal.classList.remove("active");
        $("task-form").reset();
        triggers = []; selectedTriggerId = null;
        $("active-trigger-list").innerHTML = "";
        $("passive-trigger-list").innerHTML = "";
        $("trigger-panel").innerHTML = "";
        setJsonMode(false);
    }
    $("modal-close-btn").addEventListener("click", closeModal);
    $("modal-cancel-btn").addEventListener("click", closeModal);
    modal.addEventListener("cancel", e => { e.preventDefault(); closeModal(); });
    // 表单内容太多，点击遮罩层关闭容易误操作，暂时禁用
    // modal.addEventListener("click", e => { if (e.target === modal) closeModal(); });

    $("add-task-btn").addEventListener("click", () => openTaskModal("add", null));

    function openTaskModal(action, id) {
        closeTaskMenu();
        const task = (action === "edit" && allTasks[id]) ? allTasks[id] : null;
        setJsonMode(false);
        chooseEditorTab("basic");
        $("task-action").value = action;
        $("task-id").value = id || "";
        $("task-text").value = "";
        $("task-system-prompt").value = "";
        $("trigger-panel").innerHTML = "";
        lastValidate = { triggers: [], has_active: false, next_fire: null };
        lastSuccessTime = task ? Number(task.last_success_time || 0) : 0;

        if (task) {
            $("modal-title-text").textContent = "编辑任务";
            $("task-name").value = task.name || "";
            $("task-enabled").checked = task.enabled !== false;
            const c = task.content || {};
            $("task-mode").value = c.mode || (c.use_llm ? "conversation" : "fixed");
            $("task-text").value = c.text || "";
            $("task-system-prompt").value = c.system_prompt || "";
            $("task-time-aware").checked = c.time_aware !== false;
            $("task-holiday-aware").checked = c.holiday_aware === true;
            triggers = (Array.isArray(task.triggers) ? task.triggers : []).filter(tr => tr && TYPE_INFO[tr.type]).map(tr => ({
                id: tr.id,
                type: tr.type,
                config: normalizeDurationConfig(tr.type, JSON.parse(JSON.stringify(tr.config || {}))),
            }));
            $("target-custom").value = task.target || "";
        } else {
            $("modal-title-text").textContent = "新建任务";
            $("task-name").value = "";
            $("task-enabled").checked = true;
            $("task-mode").value = "fixed";
            $("task-time-aware").checked = true;
            $("task-holiday-aware").checked = false;
            triggers = [{ id: 1, type: "interval", config: defaultConfigFor("interval") }];
            $("target-custom").value = "";
        }
        nextTriggerId = triggers.reduce((max, t) => Math.max(max, t.id), 0) + 1;
        selectedTriggerId = triggers.length ? triggers[0].id : null;
        $("migration-notice").hidden = !task || !task.migration_notice;
        $("migration-notice").textContent = task ? task.migration_notice || "" : "";
        $("target-method-manual").checked = !!$("target-custom").value && !sessionsList.includes($("target-custom").value);
        $("target-method-session").checked = !$("target-method-manual").checked;
        updateEditorName();
        updateTargetMethod();
        renderTargetPicker();
        renderTriggerCanvas();
        renderTriggerPanel();
        updateContentMode();
        updateValidateHints();
        scheduleValidate();
        openModal();
    }

    // ── 触发器：按编号分组的槽位与参数面板 ──
    function activeTriggers() { return triggers.filter(t => ACTIVE_TYPES.includes(t.type)); }
    function passiveTriggers() { return triggers.filter(t => PASSIVE_TYPES.includes(t.type)); }

    function defaultConfigFor(type) {
        if (type === "interval") return { base_time: defaultBaseTime(), days: 0, hours: 1, minutes: 0 };
        if (type === "cron") return { expr: "" };
        if (type === "window") return { start: "08:00", end: "20:00", weekdays: [0, 1, 2, 3, 4, 5, 6] };
        if (type === "random") return { threshold: 0.5 };
        return { hours: 4, minutes: 0 };
    }
    function normalizeDurationConfig(type, cfg) {
        if (!["interval", "cooldown"].includes(type)) return cfg;
        const d = type === "interval" ? Number(cfg.days || 0) : 0;
        const h = Number(cfg.hours || 0), m = Number(cfg.minutes || 0);
        if (![d, h, m].every(n => Number.isSafeInteger(n) && n >= 0)) return cfg;
        const total = d * 1440 + h * 60 + m;
        if (!Number.isSafeInteger(total)) return cfg;
        return type === "interval" ? {...cfg, days: Math.floor(total / 1440), hours: Math.floor(total % 1440 / 60), minutes: total % 60} : {...cfg, hours: Math.floor(total / 60), minutes: total % 60};
    }

    function renderTriggerCanvas() {
        const render = items => items.map(t => `<button type="button" class="rule-row tnode${t.id === selectedTriggerId ? ' selected' : ''}" data-tid="${escapeHtml(t.id)}" aria-pressed="${t.id === selectedTriggerId}"><span class="rule-number">#${escapeHtml(t.id)}</span><span>${escapeHtml(TYPE_INFO[t.type].label)}</span></button>`).join("");
        $("active-trigger-list").innerHTML = render(activeTriggers());
        $("passive-trigger-list").innerHTML = render(passiveTriggers());
        $("trigger-canvas").querySelectorAll("[data-tid]").forEach(button => button.addEventListener("click", () => {
            selectedTriggerId = Number(button.dataset.tid);
            renderTriggerCanvas();
            renderTriggerPanel();
        }));
        updateCanvasErrors();
    }

    function renderTriggerPanel() {
        const panel = $("trigger-panel");
        const t = triggers.find(x => x.id === selectedTriggerId);
        if (!t) {
            panel.innerHTML = `<p class="field-hint">暂无触发器，请从左侧添加。</p>`;
            return;
        }
        const info = TYPE_INFO[t.type];
        const options = (info.kind === "active" ? ACTIVE_TYPES : PASSIVE_TYPES)
            .map(k => `<option value="${k}" ${k === t.type ? 'selected' : ''}>${TYPE_INFO[k].label}</option>`).join("");
        panel.innerHTML = `<div class="rule-property"><div class="trigger-panel-header"><h3><span class="heading-id">#${escapeHtml(t.id)}</span> ${escapeHtml(info.label)}</h3></div><p class="field-hint trigger-type-desc">${escapeHtml(info.desc)}</p><div class="field"><label for="panel-type-select">触发器类型</label><select id="panel-type-select">${options}</select></div>${renderTriggerFields(t)}<div class="trigger-next-fire" id="panel-next-fire"></div></div><div class="rule-delete-row"><button class="btn btn-danger" type="button" id="panel-delete-btn">删除此触发器</button></div>`;
        $("panel-type-select").addEventListener("change", e => {
            t._configs = t._configs || {};
            t._configs[t.type] = JSON.parse(JSON.stringify(t.config));
            t.type = e.target.value;
            t.config = t._configs[t.type] || defaultConfigFor(t.type);
            lastValidate = {triggers: [], next_fire: null};
            renderTriggerCanvas();
            renderTriggerPanel();
            updateValidateHints();
            scheduleValidate();
        });
        $("panel-delete-btn").addEventListener("click", () => removeTrigger(t.id));
        panel.querySelectorAll("[data-field]").forEach(el => {
            const update = () => { if (el.type === "number") clampNumericInput(el); syncPanelToTrigger(t); };
            el.addEventListener("input", update);
            el.addEventListener("change", update);
            if (el.type === "number") el.addEventListener("wheel", e => e.preventDefault(), {passive: false});
        });
        panel.querySelectorAll(".weekday-chip input").forEach(cb => cb.addEventListener("change", () => syncPanelToTrigger(t)));
        updatePanelNextFire();
    }

    function clampNumericInput(el) {
        if (el.value === "") return;
        let value = Number(el.value);
        if (!Number.isFinite(value)) return;
        value = Math.max(Number(el.min || 0), Math.min(el.max === "" ? Number.MAX_SAFE_INTEGER : Number(el.max), value));
        el.value = String(el.step === "1" ? Math.trunc(value) : value);
    }
    function renderTriggerFields(t) {
        const c = t.config || {};
        const number = (key, label, max = Number.MAX_SAFE_INTEGER) => `<label><input type="number" data-field="${key}" min="0" max="${max}" step="1" aria-label="${label}" value="${escapeHtml(c[key] || 0)}"><span>${label}</span></label>`;
        if (t.type === "interval") return `<div class="trigger-config-fields"><div class="field full-row"><label for="trigger-base">基准时间</label><input type="datetime-local" id="trigger-base" data-field="base_time" value="${escapeHtml(toDatetimeLocal(c.base_time) || defaultBaseTime())}"></div><div class="field full-row"><span class="field-label">间隔时长</span><div class="duration-fields">${number('days', '天')}${number('hours', '小时', 23)}${number('minutes', '分钟', 59)}</div></div></div>`;
        if (t.type === "cron") return `<div class="field"><label for="trigger-expr">cron 表达式</label><input id="trigger-expr" class="mono" data-field="expr" placeholder="0 */4 * * *" value="${escapeHtml(c.expr || '')}"><p class="field-hint">五段依次为：分、时、日、月、周。</p></div>`;
        if (t.type === "window") {
            const wd = Array.isArray(c.weekdays) ? c.weekdays : [];
            return `<div class="trigger-config-fields"><div class="field"><label for="trigger-start">开始时间</label><input id="trigger-start" type="time" data-field="start" value="${escapeHtml(c.start || '08:00')}"></div><div class="field"><label for="trigger-end">结束时间</label><input id="trigger-end" type="time" data-field="end" value="${escapeHtml(c.end || '20:00')}"></div><div class="field full-row"><span class="field-label">星期</span><div class="weekdays-group">${WEEKDAY_CN.map((day, i) => `<label class="weekday-chip"><input type="checkbox" data-weekday="${i}" ${wd.includes(i) ? 'checked' : ''}>${day}</label>`).join('')}</div><p class="field-hint">支持跨天区间，例如 22:00 至次日 06:00。</p></div></div>`;
        }
        if (t.type === "random") return `<div class="field narrow-field"><label for="trigger-probability">通过概率</label><input id="trigger-probability" type="number" data-field="threshold" min="0" max="1" step="0.01" value="${escapeHtml(c.threshold != null ? c.threshold : 0.5)}"><p class="field-hint">范围 0–1，0.15 表示 15%。</p></div>`;
        return `<div class="field"><span class="field-label">冷却时长</span><div class="duration-fields two">${number('hours', '小时')}${number('minutes', '分钟', 59)}</div></div>`;
    }

    function syncPanelToTrigger(t) {
        const panel = $("trigger-panel");
        const get = f => { const el = panel.querySelector(`[data-field="${f}"]`); return el ? el.value : ""; };
        const int = (f, d) => { const v = parseInt(get(f)); return isNaN(v) ? d : v; };
        if (t.type === "interval") {
            t.config = { days: int("days", 0), hours: int("hours", 0), minutes: int("minutes", 0), base_time: get("base_time") };
        } else if (t.type === "cron") {
            t.config = { expr: get("expr").trim() };
        } else if (t.type === "window") {
            t.config = {
                start: get("start") || "00:00",
                end: get("end") || "23:59",
                weekdays: Array.from(panel.querySelectorAll(".weekday-chip input:checked")).map(cb => parseInt(cb.dataset.weekday)),
            };
        } else if (t.type === "random") {
            const v = parseFloat(get("threshold"));
            t.config = { threshold: isNaN(v) ? 0 : v };
        } else if (t.type === "cooldown") {
            t.config = { hours: int("hours", 0), minutes: int("minutes", 0) };
        }
        renderTriggerCanvas();
        updateValidateHints();
        scheduleValidate();
    }

    function removeTrigger(id) {
        triggers = triggers.filter(t => t.id !== id);
        if (selectedTriggerId === id) selectedTriggerId = triggers.length ? triggers[0].id : null;
        renderTriggerCanvas();
        renderTriggerPanel();
        updateValidateHints();
        scheduleValidate();
    }

    function addTrigger(kind) {
        const type = kind === "active" ? "interval" : "window";
        const t = { id: nextTriggerId++, type, config: defaultConfigFor(type) };
        triggers.push(t);
        selectedTriggerId = t.id;
        renderTriggerCanvas();
        renderTriggerPanel();
        updateValidateHints();
        scheduleValidate();
    }
    $("add-active-trigger-btn").addEventListener("click", () => addTrigger("active"));
    $("add-passive-trigger-btn").addEventListener("click", () => addTrigger("passive"));

    // ── 单个发送目标 ──
    function renderTargetPicker() {
        const target = $("target-custom").value;
        const sessions = target && !sessionsList.includes(target) ? [target, ...sessionsList] : sessionsList;
        $("target-select").innerHTML = `<option value="">不指定发送对象</option>` +
            sessions.map(s => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join("");
        $("target-select").value = target;
    }
    function updateTargetMethod() {
        const manual = $("target-method-manual").checked;
        $("target-select").hidden = manual;
        $("target-custom").hidden = !manual;
    }
    $("target-method-session").addEventListener("change", () => { renderTargetPicker(); updateTargetMethod(); });
    $("target-method-manual").addEventListener("change", updateTargetMethod);
    $("target-select").addEventListener("change", () => {
        $("target-custom").value = $("target-select").value;
    });
    $("target-custom").addEventListener("input", () => {
        $("target-select").value = sessionsList.includes($("target-custom").value) ? $("target-custom").value : "";
    });
    $("target-custom").addEventListener("keydown", e => {
        if (e.key === "Enter") e.preventDefault();
    });

    // ── 任务内容模式说明 ──
    const MODE_INFO = {
        fixed: {
            detail: "直接把下方文本作为消息发送到目标，不经过 AI。",
            label: "发送内容",
        },
        standalone: {
            detail: "使用本任务的系统提示调用 AI，不注入会话人格与上下文。",
            label: "任务提示词",
        },
        conversation: {
            detail: "使用目标会话的人格与上下文生成回复。",
            label: "任务提示词",
        },
    };
    function updateContentMode() {
        const mode = $("task-mode").value;
        const info = MODE_INFO[mode] || MODE_INFO.fixed;
        $("content-mode-detail").textContent = info.detail;
        $("task-text-label").textContent = info.label;
        // 独立 AI 系统提示仅 standalone 模式使用
        $("system-prompt-group").style.display = mode === "standalone" ? "" : "none";
        // 两个感知开关仅对两种 AI 模式生效
        setSwitchDisabled("task-time-aware", mode === "fixed");
        setSwitchDisabled("task-holiday-aware", mode === "fixed");
    }
    function setSwitchDisabled(id, disabled) {
        const el = $(id);
        el.disabled = disabled;
        el.closest(".switch-toggle").classList.toggle("disabled", disabled);
    }
    $("task-mode").addEventListener("change", updateContentMode);

    // ── 触发器实时校验 + 下次检查触发时间 ──
    function scheduleValidate() {
        clearTimeout(validateTimer);
        ++validateRevision;
        validateTimer = setTimeout(doValidate, 400);
    }
    function updateValidateHints() {
        const el = $("next-fire-hint");
        if (!activeTriggers().length) {
            el.textContent = "无主动型触发器，任务不会自动触发";
            el.style.color = "var(--color-danger)";
            return;
        }
        el.textContent = lastValidate.next_fire
            ? "下次检查：" + fmtTs(lastValidate.next_fire)
            : "下次检查：待计算…";
        el.style.color = "var(--text-secondary)";
    }
    function updatePanelNextFire() {
        const el = $("panel-next-fire");
        const t = triggers.find(x => x.id === selectedTriggerId);
        if (!el || !t) return;
        const r = (lastValidate.triggers || []).find(x => x.id === t.id);
        if (r && !r.ok) {
            el.style.display = "";
            el.textContent = "配置有误：" + r.msg;
            el.className = "trigger-next-fire error";
            return;
        }
        // 区间型/随机型没有可展示的时间信息，整行隐藏
        if (t.type === "window" || t.type === "random") {
            el.style.display = "none";
            return;
        }
        el.style.display = "";
        if (t.type === "cooldown") {
            el.textContent = "上次任务成功时间：" + (lastSuccessTime ? fmtTs(lastSuccessTime) : "从未成功执行");
            el.className = "trigger-next-fire";
            return;
        }
        const fires = (r && r.future_fires) || [];
        el.innerHTML = fires.length ? `<h4>未来三次检查</h4>${fires.map(f => `<time>${fmtTs(f)}</time>`).join("")}` : "未来检查：请完成配置…";
        el.className = fires.length ? "trigger-next-fire active" : "trigger-next-fire";
    }
    function updateCanvasErrors() {
        (lastValidate.triggers || []).forEach(r => {
            const node = document.querySelector(`#trigger-canvas .tnode[data-tid="${r.id}"]`);
            if (node) node.classList.toggle("invalid", !r.ok);
        });
    }
    async function doValidate() {
        if (!modal.classList.contains("active")) return;
        const revision = validateRevision;
        const payload = triggers.map(t => ({ id: t.id, type: t.type, config: t.config }));
        try {
            const res = await bridge.apiPost("validate", { triggers: payload });
            if (revision !== validateRevision || !modal.open) return;
            if (!res || res.status !== "success") return;
            lastValidate = res.results || { triggers: [], next_fire: null };
            updateValidateHints();
            updatePanelNextFire();
            updateCanvasErrors();
        } catch (e) { /* 静默 */ }
    }

    // ── 表单 ↔ JSON（进阶纯文本配置） ──
    function buildTaskPayload() {
        return {
            id: $("task-id").value || undefined,
            name: $("task-name").value.trim(),
            enabled: $("task-enabled").checked,
            triggers: triggers.map(t => ({ id: t.id, type: t.type, config: t.config })),
            content: {
                text: $("task-text").value,
                mode: $("task-mode").value,
                system_prompt: $("task-system-prompt").value,
                time_aware: $("task-time-aware").checked,
                holiday_aware: $("task-holiday-aware").checked,
            },
            target: $("target-custom").value.trim(),
        };
    }

    // 把 JSON 回填到表单（未知类型的触发器会被忽略并提示）
    function applyTaskPayload(obj) {
        const c = obj.content || {};
        $("task-name").value = obj.name || "";
        $("task-enabled").checked = obj.enabled !== false;
        $("task-mode").value = c.mode || "fixed";
        $("task-text").value = c.text || "";
        $("task-system-prompt").value = c.system_prompt || "";
        $("task-time-aware").checked = c.time_aware !== false;
        $("task-holiday-aware").checked = c.holiday_aware === true;

        const raw = Array.isArray(obj.triggers) ? obj.triggers : [];
        const unknown = raw.filter(t => !t || !TYPE_INFO[t.type]).length;
        triggers = raw.filter(t => t && TYPE_INFO[t.type]).map((t, i) => ({
            id: Number.isInteger(t.id) && t.id > 0 ? t.id : i + 1,
            type: t.type,
            config: normalizeDurationConfig(t.type, (t.config && typeof t.config === "object" && !Array.isArray(t.config)) ? t.config : {}),
        }));
        $("target-custom").value = obj.target || "";
        renderTargetPicker();
        updateEditorName();
        nextTriggerId = triggers.reduce((max, t) => Math.max(max, t.id), 0) + 1;
        selectedTriggerId = triggers.length ? triggers[0].id : null;

        renderTriggerCanvas();
        renderTriggerPanel();
        updateContentMode();
        updateValidateHints();
        scheduleValidate();
        if (unknown) showToast(`已忽略 ${unknown} 个类型未知的触发器`, true);
    }

    let jsonMode = false;
    function setJsonMode(on) {
        jsonMode = on;
        $("task-form-body").style.display = on ? "none" : "";
        $("task-json-body").style.display = on ? "" : "none";
        $("editor-tabs").style.display = on ? "none" : "";
        $("task-json-toggle").setAttribute("aria-pressed", String(on));
        $("task-json-toggle").innerHTML = `<span>${on ? "表单模式" : "JSON 模式"}</span>`;
        if (on) $("task-json").value = JSON.stringify(buildTaskPayload(), null, 2);
    }
    $("task-json-toggle").addEventListener("click", () => {
        if (!jsonMode) { setJsonMode(true); return; }
        let obj;
        try {
            obj = JSON.parse($("task-json").value);
            if (!obj || typeof obj !== "object" || Array.isArray(obj)) throw new Error("顶层必须是对象");
        } catch (e) {
            showToast("JSON 解析失败：" + e.message, true);
            return;
        }
        if ("targets" in obj || (obj.target !== undefined && typeof obj.target !== "string")) {
            showToast("请使用单个 target 对话字符串，不再支持 targets 数组", true);
            return;
        }
        if (String(obj.name || "").trim().length > 32) { showToast("任务名称不能超过 32 个字符", true); return; }
        applyTaskPayload(obj);
        setJsonMode(false);
    });

    function validateForm() {
        if (!$("task-name").value.trim()) { showToast("请填写任务名称", true); chooseEditorTab("basic"); $("task-name").focus(); return false; }
        if ($("task-name").value.trim().length > 32) { showToast("任务名称不能超过 32 个字符", true); chooseEditorTab("basic"); return false; }
        if (!triggers.length) { showToast("至少需要一个触发器", true); return false; }
        if (!activeTriggers().length) { showToast("至少需要一个主动型触发器（周期型或 cron 型）", true); return false; }
        if (!$("target-custom").value.trim() && $("task-text").value.trim()) { showToast("请选择一个发送目标对话", true); return false; }
        return true;
    }

    // ── 提交任务 ──
    $("task-form").addEventListener("submit", async e => {
        e.preventDefault();
        let payload;
        if (jsonMode) {
            try {
                payload = JSON.parse($("task-json").value);
            } catch (err) {
                showToast("JSON 解析失败：" + err.message, true);
                return;
            }
            if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
                showToast("JSON 顶层必须是对象", true);
                return;
            }
            if (String(payload.name || "").trim().length > 32) { showToast("任务名称不能超过 32 个字符", true); return; }
            // id 以当前编辑的任务为准（新增时不带 id）
            const editingId = $("task-id").value;
            if (editingId) payload.id = editingId;
            else delete payload.id;
        } else {
            if (!validateForm()) return;
            payload = buildTaskPayload();
        }
        const btn = $("modal-submit-btn");
        btn.disabled = true; btn.textContent = "保存中...";
        try {
            const res = await bridge.apiPost("upsert_task", payload);
            if (res && res.status === "success") {
                showToast("保存成功");
                closeModal();
                await loadData();
            } else {
                showToast("保存失败: " + (res && res.message || "未知错误"), true);
            }
        } catch (err) {
            showToast("请求失败: " + err.message, true);
        } finally {
            btn.disabled = false; btn.textContent = "保存任务";
        }
    });

    // ── 复制 / 删除 ──
    async function copyTask(id) {
        try {
            const res = await bridge.apiPost("copy_task", { id });
            if (res && res.status === "success") { showToast("已复制为副本（默认停用）"); await loadData(); }
            else showToast("复制失败: " + (res && res.message || ""), true);
        } catch (e) { showToast("复制失败: " + e.message, true); }
    }

    async function triggerNow(id, btn) {
        // 触发期间禁用按钮并显示加载态，避免连点造成重复触发
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = `<span class="spinner spinner-xs"></span>触发中…`;
        }
        try {
            const res = await bridge.apiPost("trigger_now", { id });
            if (res && res.status === "success") {
                showToast("已触发一次" + (res.detail ? "：" + res.detail : ""));
                await loadData();
            } else {
                showToast("触发失败: " + (res && res.message || ""), true);
            }
        } catch (e) {
            showToast("触发失败: " + e.message, true);
        } finally {
            if (btn && btn.isConnected) {
                btn.disabled = false;
                btn.textContent = "触发一次";
            }
        }
    }

    let pendingDeleteId = null;
    let confirmMode = "delete";
    const confirmModal = $("confirm-modal-overlay");
    function confirmDelete(id) {
        confirmMode = "delete";
        pendingDeleteId = id;
        const t = allTasks[id];
        $("confirm-modal-text").textContent = `确定要删除任务「${t ? t.name : id}」吗？`;
        confirmModal.showModal();
    }
    function closeConfirm() { confirmModal.close(); pendingDeleteId = null; confirmMode = "delete"; }
    confirmModal.addEventListener("cancel", e => { e.preventDefault(); closeConfirm(); });
    $("confirm-modal-cancel-btn").addEventListener("click", closeConfirm);
    confirmModal.addEventListener("click", e => { if (e.target === confirmModal) closeConfirm(); });
    $("confirm-modal-ok-btn").addEventListener("click", async () => {
        const okBtn = $("confirm-modal-ok-btn");
        okBtn.disabled = true; okBtn.textContent = "处理中...";
        try {
            if (confirmMode === "clear-llm") {
                const res = await bridge.apiPost("clear_llm_logs", {});
                if (res && res.status === "success") {
                    showToast("LLM 日志已清空");
                    closeConfirm();
                    llmDetailCache.clear();
                    llmImageCache.clear();
                    await loadLlmLogs(0);
                }
                else showToast("清空失败: " + (res && res.message || ""), true);
            } else {
                if (!pendingDeleteId) { okBtn.disabled = false; okBtn.textContent = "确定"; return; }
                const res = await bridge.apiPost("delete_task", { id: pendingDeleteId });
                if (res && res.status === "success") { showToast("已删除"); closeConfirm(); await loadData(); }
                else showToast("删除失败: " + (res && res.message || ""), true);
            }
        } catch (e) { showToast("操作失败: " + e.message, true); }
        finally { okBtn.disabled = false; okBtn.textContent = "确定"; }
    });

    // ── 配置保存 ──
    $("config-form").addEventListener("submit", async e => {
        e.preventDefault();
        const payload = {
            log_retention: parseInt($("config-log-retention").value) || 100,
            llm_timeout: parseInt($("config-llm-timeout").value) || 60,
            llm_log_retention: parseInt($("config-llm-log-retention").value) || 10,
        };
        const btn = $("save-config-btn");
        btn.disabled = true; btn.textContent = "保存中...";
        try {
            const res = await bridge.apiPost("save_config", payload);
            if (res && res.status === "success") { savedConfig = payload; updateConfigState(); showToast("配置已保存"); }
            else showToast("保存失败: " + (res && res.message || ""), true);
        } catch (e) { showToast("保存失败: " + e.message, true); }
        finally { btn.disabled = false; btn.textContent = "保存配置"; }
    });

    $("refresh-logs-btn").addEventListener("click", () => loadRunLogs(logsPage));
    $("refresh-tasks-btn").addEventListener("click", loadData);

    // ── 初始化 ──
    await loadSessions();
    await loadData();
    setInterval(() => {
        if (document.hidden) return;
        document.querySelectorAll("[data-next-fire]").forEach(el => { el.textContent = remainingTime(el.dataset.nextFire); });
    }, 60000);
    setInterval(() => {
        if (document.hidden) return;
        if ($("view-logs").classList.contains("active")) loadRunLogs(logsPage);
        if ($("view-tasks").classList.contains("active") && !modal.classList.contains("active") && $("task-menu").hidden) loadData();
    }, 8000);
}

if (document.readyState === "complete" || document.readyState === "interactive") init();
else document.addEventListener("DOMContentLoaded", init);
