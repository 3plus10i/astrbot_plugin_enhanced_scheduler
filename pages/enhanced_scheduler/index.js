/* 增强计划任务插件前端逻辑 */
async function init() {
    const bridge = window.AstrBotPluginPage;
    try { await bridge.ready(); } catch (e) { console.error("bridge 连接失败:", e); }

    let allTasks = {};
    let allLogs = [];
    let sessionsList = [];
    let validateTimer = null;
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
    // 脱敏 UMO 末尾数字串（QQ/群号）：保留前3后2，中间打码
    // 神经病
    function maskUmo(umo) {
        return String(umo).replace(/(\d+)$/, (m) => {
            // if (m.length <= 4) return m;
            // const keep = Math.min(3, Math.floor(m.length / 2));
            // return m.slice(0, keep) + "*".repeat(m.length - keep - 2) + m.slice(-2);
            return m;
        });
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
            $("view-" + btn.getAttribute("data-tab")).classList.add("active");
            if (btn.getAttribute("data-tab") === "llm" && !llmLoaded) loadLlmLogs(0);
        });
    });

    const backBtn = $("back-btn");
    if (window.self !== window.top) backBtn.style.display = "none";
    else backBtn.addEventListener("click", () => showToast("请使用外部导航返回"));

    // ── 加载数据 ──
    async function loadData() {
        try {
            const res = await bridge.apiGet("get_data", {});
            if (!res || res.status !== "success") { showToast("获取数据失败", true); return; }
            allTasks = res.tasks || {};
            allLogs = res.logs || [];
            renderTasks();
            renderLogs();
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
        const body = $("tasks-body");
        const keys = Object.keys(allTasks);
        if (!keys.length) {
            body.innerHTML = `<tr><td colspan="8" class="table-empty">暂无计划任务，点击右上角新增。</td></tr>`;
            return;
        }
        body.innerHTML = keys.map(k => {
            const t = allTasks[k];
            const trgList = t.triggers || [];
            const activeCount = trgList.filter(tr => tr.type === "interval" || tr.type === "cron").length;
            const passiveCount = trgList.length - activeCount;
            const trigSummary = `<span class="badge badge-llm">${activeCount} 主动</span> <span class="badge badge-fixed">${passiveCount} 被动</span>`;
            const ct = t.content || {};
            const mode = ct.mode || (ct.use_llm ? "conversation" : "fixed");
            const hasText = !!(ct.text && String(ct.text).trim());
            const modeCN = {fixed:"固定文本", standalone:"独立AI", conversation:"对话AI"}[mode] || mode;
            const contentBadge = hasText
                ? `<span class="badge ${mode === 'fixed' ? 'badge-fixed' : 'badge-llm'}">${escapeHtml(modeCN)}</span>`
                : '<span class="badge badge-noop">空动作</span>';
            const nf = t._next_fire;
            return `
                <tr>
                    <td class="cell-wrap"><strong>${escapeHtml(t.name)}</strong></td>
                    <td><span class="badge ${t.enabled ? 'badge-active' : 'badge-inactive'}">${t.enabled ? '启用' : '停用'}</span></td>
                    <td class="cell-wrap">${trigSummary || '—'}</td>
                    <td>${nf ? fmtTs(nf) : '—'}</td>
                    <td>${contentBadge}</td>
                    <td>${(t.targets || []).length} 个</td>
                    <td><div class="actions-cell">
                        <button class="btn btn-edit edit-task-btn" data-id="${escapeHtml(k)}">编辑</button>
                        <button class="btn btn-secondary btn-sm copy-task-btn" data-id="${escapeHtml(k)}">复制</button>
                        <button class="btn btn-secondary btn-sm trigger-now-btn" data-id="${escapeHtml(k)}">触发一次</button>
                        <button class="btn btn-danger delete-task-btn" data-id="${escapeHtml(k)}">删除</button>
                    </div></td>
                </tr>`;
        }).join("");
        attachTaskRowListeners();
    }

    function renderLogs() {
        const body = $("logs-body");
        if (!allLogs.length) {
            body.innerHTML = `<tr><td colspan="6" class="table-empty">暂无日志。任务触发后会在此显示最近记录。</td></tr>`;
            $("logs-pager").innerHTML = "";
            logsPage = 0;
            return;
        }
        const sorted = allLogs.slice().reverse();
        const pages = Math.max(1, Math.ceil(sorted.length / LOGS_PAGE_SIZE));
        logsPage = Math.min(logsPage, pages - 1);
        body.innerHTML = sorted.slice(logsPage * LOGS_PAGE_SIZE, (logsPage + 1) * LOGS_PAGE_SIZE).map(l => {
            const tof = l.trigger_tof || {};
            const tofStr = Object.keys(tof).map(id => `#${id}:${tof[id] ? '✓' : '✗'}`).join(" ");
            const actionCN = {send_llm:"LLM发送",send_fixed:"发送文本",noop:"空动作",partial:"部分失败",skipped:"逻辑未过",failed:"失败"}[l.action] || l.action;
            const actionBadge = l.action === "skipped" ? "badge-skipped" : (l.action === "failed" || l.action === "partial" ? "badge-failed" : "badge-active");
            const detail = l.detail || "";
            const targetsInfo = (l.targets_result || []).map(tr => `${escapeHtml(maskUmo(tr.umo))}:${tr.ok ? '✓' : '✗'+escapeHtml(tr.error||'')}`).join(" ");
            return `<tr>
                <td>${fmtTs(l.time)}</td>
                <td class="cell-wrap">${escapeHtml(l.task_name)}</td>
                <td class="cell-wrap">${escapeHtml(tofStr)}</td>
                <td>${l.source === "manual" ? '<span class="badge badge-llm">手动</span>' : (l.logic_result ? '<span class="badge badge-active">通过</span>' : '<span class="badge badge-skipped">未过</span>')}</td>
                <td><span class="badge ${actionBadge}">${escapeHtml(actionCN)}</span></td>
                <td class="cell-wrap">${escapeHtml(detail)} ${escapeHtml(targetsInfo)}</td>
            </tr>`;
        }).join("");
        renderLogsPager(pages, sorted.length);
    }

    function renderLogsPager(pages, total) {
        const pager = $("logs-pager");
        pager.innerHTML = `
            <button class="btn btn-secondary btn-sm" id="logs-page-prev" ${logsPage <= 0 ? "disabled" : ""}>上一页</button>
            <span class="page-info">第 ${logsPage + 1} / ${pages} 页 · 共 ${total} 条</span>
            <button class="btn btn-secondary btn-sm" id="logs-page-next" ${logsPage >= pages - 1 ? "disabled" : ""}>下一页</button>`;
        $("logs-page-prev").addEventListener("click", () => {
            if (logsPage > 0) { logsPage--; renderLogs(); }
        });
        $("logs-page-next").addEventListener("click", () => {
            if (logsPage < pages - 1) { logsPage++; renderLogs(); }
        });
    }

    function renderConfig(cfg) {
        $("config-poll-interval").value = cfg.poll_interval != null ? cfg.poll_interval : 60;
        $("config-log-retention").value = cfg.log_retention != null ? cfg.log_retention : 200;
        $("config-llm-timeout").value = cfg.llm_timeout != null ? cfg.llm_timeout : 60;
        $("config-llm-log-retention").value = cfg.llm_log_retention != null ? cfg.llm_log_retention : 10;
    }

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
            const imgInfo = (m.images && m.images.length) ? ` · ${m.images.length}图 ${fmtSize(m.image_bytes)}` : "";
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
            <span class="page-info">第 ${page + 1} / ${pages} 页 · 共 ${llmTotal} 条</span>
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
            html += `<span class="b64-chip" data-idx="${idx}" title="点击加载图片">图片 ${escapeHtml(ext)} · ${fmtSize(size)}</span>`;
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
        confirmModal.classList.add("active");
    });

    function attachTaskRowListeners() {
        document.querySelectorAll(".edit-task-btn").forEach(b => b.addEventListener("click", () => openTaskModal("edit", b.getAttribute("data-id"))));
        document.querySelectorAll(".copy-task-btn").forEach(b => b.addEventListener("click", () => copyTask(b.getAttribute("data-id"))));
        document.querySelectorAll(".trigger-now-btn").forEach(b => b.addEventListener("click", () => triggerNow(b.getAttribute("data-id"), b)));
        document.querySelectorAll(".delete-task-btn").forEach(b => b.addEventListener("click", () => confirmDelete(b.getAttribute("data-id"))));
    }

    // ── 任务编辑弹窗 ──
    const modal = $("task-modal");
    const ACTIVE_TYPES = ["interval", "cron"];
    const PASSIVE_TYPES = ["window", "random", "cooldown"];
    const TYPE_INFO = {
        interval: { kind: "active", label: "主动-周期型", desc: "从基准时间开始，每经过周期触发一次" },
        cron: { kind: "active", label: "主动-cron型", desc: "按标准 5 段 cron 表达式（分 时 日 月 周）触发" },
        window: { kind: "passive", label: "被动-区间型", desc: "被主动型触发器唤起后，若在指定时间段内则激活" },
        random: { kind: "passive", label: "被动-随机型", desc: "被主动型触发器唤起后，以给定概率激活" },
        cooldown: { kind: "passive", label: "被动-冷却型", desc: "被主动型触发器唤起后，若本任务上次成功后到现在时间超过冷却时长则触发" },
    };
    const WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

    let triggers = [];            // [{id, type, config}]
    let targets = [];             // [umo]
    let selectedTriggerId = null;
    let nextTriggerId = 1;
    let lastValidate = { triggers: [], has_active: false, next_fire: null };
    let lastSuccessTime = 0;

    function openModal() { modal.classList.add("active"); }
    function closeModal() {
        modal.classList.remove("active");
        $("task-form").reset();
        triggers = []; targets = []; selectedTriggerId = null;
        $("trigger-canvas").innerHTML = "";
        $("trigger-panel").innerHTML = "";
        $("targets-tags").innerHTML = "";
    }
    $("modal-close-btn").addEventListener("click", closeModal);
    $("modal-cancel-btn").addEventListener("click", closeModal);
    // 表单内容太多，点击遮罩层关闭容易误操作，暂时禁用
    // modal.addEventListener("click", e => { if (e.target === modal) closeModal(); });

    $("add-task-btn").addEventListener("click", () => openTaskModal("add", null));

    function openTaskModal(action, id) {
        const task = (action === "edit" && allTasks[id]) ? allTasks[id] : null;
        $("task-action").value = action;
        $("task-id").value = id || "";
        $("task-text").value = "";
        $("task-system-prompt").value = "";
        $("trigger-panel").innerHTML = "";
        lastValidate = { triggers: [], has_active: false, next_fire: null };
        lastSuccessTime = task ? Number(task.last_success_time || 0) : 0;

        if (task) {
            $("modal-title-text").textContent = "编辑: " + task.name;
            $("task-name").value = task.name || "";
            $("task-enabled").checked = task.enabled !== false;
            const c = task.content || {};
            $("task-mode").value = c.mode || (c.use_llm ? "conversation" : "fixed");
            $("task-text").value = c.text || "";
            $("task-system-prompt").value = c.system_prompt || "";
            $("task-time-aware").checked = c.time_aware !== false;
            $("task-holiday-aware").checked = c.holiday_aware === true;
            triggers = (task.triggers || []).map(tr => ({
                id: tr.id,
                type: tr.type,
                config: JSON.parse(JSON.stringify(tr.config || {})),
            }));
            targets = (task.targets || []).slice();
        } else {
            $("modal-title-text").textContent = "新增计划任务";
            $("task-name").value = "";
            $("task-enabled").checked = true;
            $("task-mode").value = "fixed";
            $("task-time-aware").checked = true;
            $("task-holiday-aware").checked = false;
            triggers = [{ id: 1, type: "interval", config: defaultConfigFor("interval") }];
            targets = [];
        }
        nextTriggerId = triggers.reduce((max, t) => Math.max(max, t.id), 0) + 1;
        selectedTriggerId = triggers.length ? triggers[0].id : null;

        renderTargetPicker();
        renderTargets();
        renderTriggerCanvas();
        renderTriggerPanel();
        updateContentMode();
        updateValidateHints();
        scheduleValidate();
        openModal();
    }

    // ── 触发器：图形化框图 ──
    function activeTriggers() { return triggers.filter(t => ACTIVE_TYPES.includes(t.type)); }
    function passiveTriggers() { return triggers.filter(t => PASSIVE_TYPES.includes(t.type)); }

    function defaultConfigFor(type) {
        if (type === "interval") return { base_time: defaultBaseTime(), days: 0, hours: 1, minutes: 0 };
        if (type === "cron") return { expr: "" };
        if (type === "window") return { start: "08:00", end: "20:00", weekdays: [0, 1, 2, 3, 4, 5, 6] };
        if (type === "random") return { threshold: 0.5 };
        return { hours: 4, minutes: 0 };
    }

    function triggerSummaryText(t) {
        const c = t.config || {};
        if (t.type === "interval") {
            const d = parseInt(c.days) || 0, h = parseInt(c.hours) || 0, m = parseInt(c.minutes) || 0;
            const parts = [];
            if (d) parts.push(d + "天");
            if (h) parts.push(h + "时");
            if (m || !parts.length) parts.push(m + "分");
            return "每 " + parts.join("");
        }
        if (t.type === "cron") return c.expr ? c.expr : "未填写表达式";
        if (t.type === "window") {
            const wd = Array.isArray(c.weekdays) ? c.weekdays : [];
            const scope = wd.length ? "每周" + wd.map(i => WEEKDAY_CN[i] ? WEEKDAY_CN[i][1] : "").join("") : "每天";
            return `${scope} ${c.start || "00:00"}-${c.end || "23:59"}`;
        }
        if (t.type === "random") return `概率 ${c.threshold != null ? c.threshold : 0.5}`;
        if (t.type === "cooldown") return `冷却 ${parseInt(c.hours) || 0}时${parseInt(c.minutes) || 0}分`;
        return "";
    }

    const NODE_W = 176, NODE_H = 62, GAP_X = 58, GAP_Y = 24, PAD = 24;

    function renderTriggerCanvas() {
        const canvas = $("trigger-canvas");
        const actives = activeTriggers();
        const passives = passiveTriggers();
        const startR = 20;
        const startX = PAD + startR;
        const activeX = PAD + startR * 2 + 26;
        const chainX = activeX + NODE_W + GAP_X;
        const targetW = 96, targetH = 46;
        const rows = Math.max(actives.length, 1);
        const colH = rows * NODE_H + (rows - 1) * GAP_Y;
        const midY = PAD + colH / 2;
        const passiveX = i => chainX + i * (NODE_W + GAP_X);
        const targetX = passives.length
            ? passiveX(passives.length - 1) + NODE_W + GAP_X
            : activeX + NODE_W + GAP_X;
        const width = targetX + targetW + PAD;
        const height = PAD * 2 + colH;
        const nodeTop = i => PAD + i * (NODE_H + GAP_Y);
        const nodeCy = i => nodeTop(i) + NODE_H / 2;

        const wires = [];
        if (actives.length) {
            actives.forEach((t, i) => {
                wires.push(`<path class="wire" d="M ${startX + startR} ${midY} C ${startX + startR + 30} ${midY}, ${activeX - 30} ${nodeCy(i)}, ${activeX} ${nodeCy(i)}"/>`);
                const fromX = activeX + NODE_W;
                const toX = passives.length ? passiveX(0) : targetX;
                wires.push(`<path class="wire" d="M ${fromX} ${nodeCy(i)} C ${fromX + 30} ${nodeCy(i)}, ${toX - 30} ${midY}, ${toX} ${midY}" marker-end="url(#trigger-arrow)"/>`);
            });
        } else {
            wires.push(`<path class="wire dashed" d="M ${startX + startR} ${midY} L ${targetX} ${midY}" marker-end="url(#trigger-arrow)"/>`);
        }
        passives.forEach((t, i) => {
            if (i > 0) wires.push(`<path class="wire" d="M ${passiveX(i - 1) + NODE_W} ${midY} L ${passiveX(i)} ${midY}"/>`);
            if (i === passives.length - 1) wires.push(`<path class="wire" d="M ${passiveX(i) + NODE_W} ${midY} L ${targetX} ${midY}" marker-end="url(#trigger-arrow)"/>`);
        });

        const nodeHtml = (t, style) => `
            <div class="tnode ${TYPE_INFO[t.type].kind === "active" ? "active" : "passive"}${t.id === selectedTriggerId ? " selected" : ""}" data-tid="${t.id}" style="${style}">
                <span class="tnode-head">
                    <span class="tnode-id">${t.id}</span>
                    <span class="tnode-label">${TYPE_INFO[t.type].label}</span>
                </span>
                <span class="tnode-sub">${escapeHtml(triggerSummaryText(t))}</span>
            </div>`;

        canvas.style.width = width + "px";
        canvas.style.height = height + "px";
        canvas.innerHTML = `
            <svg class="trigger-wires" width="${width}" height="${height}">
                <defs>
                    <marker id="trigger-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
                        <path d="M 0 0 L 8 4 L 0 8 z" fill="rgba(148,163,184,0.9)"></path>
                    </marker>
                </defs>
                ${wires.join("")}
            </svg>
            <div class="tnode tstart" style="left:${PAD}px;top:${midY - startR}px;width:${startR * 2}px;height:${startR * 2}px;">Start</div>
            ${actives.map((t, i) => nodeHtml(t, `left:${activeX}px;top:${nodeTop(i)}px;width:${NODE_W}px;height:${NODE_H}px;`)).join("")}
            ${passives.map((t, i) => nodeHtml(t, `left:${passiveX(i)}px;top:${midY - NODE_H / 2}px;width:${NODE_W}px;height:${NODE_H}px;`)).join("")}
            <div class="tnode ttarget" style="left:${targetX}px;top:${midY - targetH / 2}px;width:${targetW}px;height:${targetH}px;">Target</div>
            ${actives.length ? "" : `<div class="canvas-hint" style="left:${activeX}px;top:${midY + 44}px;">还没有主动型触发器：任务不会自动触发</div>`}
        `;
        canvas.querySelectorAll(".tnode[data-tid]").forEach(el => {
            el.addEventListener("click", () => {
                selectedTriggerId = parseInt(el.dataset.tid);
                renderTriggerCanvas();
                renderTriggerPanel();
            });
        });
        updateCanvasErrors();
    }

    function renderTriggerPanel() {
        const panel = $("trigger-panel");
        const t = triggers.find(x => x.id === selectedTriggerId);
        if (!t) {
            panel.innerHTML = `<div class="empty-hint">点击上方节点查看并编辑该触发器的配置。</div>`;
            return;
        }
        const info = TYPE_INFO[t.type];
        const options = (info.kind === "active" ? ACTIVE_TYPES : PASSIVE_TYPES)
            .map(k => `<option value="${k}" ${k === t.type ? "selected" : ""}>${TYPE_INFO[k].label}</option>`).join("");
        panel.innerHTML = `
            <div class="trigger-panel-header">
                <span class="trigger-id-badge">${t.id}</span>
                <select class="trigger-type-select" id="panel-type-select">${options}</select>
                <span class="trigger-type-desc">${escapeHtml(info.desc)}</span>
                <button class="btn btn-danger btn-sm" type="button" id="panel-delete-btn"><span>删除</span></button>
            </div>
            ${renderTriggerFields(t)}
            <div class="trigger-next-fire" id="panel-next-fire"></div>
        `;
        $("panel-type-select").addEventListener("change", e => {
            t.type = e.target.value;
            t.config = defaultConfigFor(t.type);
            renderTriggerCanvas();
            renderTriggerPanel();
            scheduleValidate();
        });
        $("panel-delete-btn").addEventListener("click", () => removeTrigger(t.id));
        panel.querySelectorAll("[data-field]").forEach(el => {
            el.addEventListener("input", () => syncPanelToTrigger(t));
            el.addEventListener("change", () => syncPanelToTrigger(t));
        });
        panel.querySelectorAll(".weekday-chip input").forEach(cb => {
            cb.addEventListener("change", () => {
                cb.closest(".weekday-chip").classList.toggle("checked", cb.checked);
                syncPanelToTrigger(t);
            });
        });
        updatePanelNextFire();
    }

    function renderTriggerFields(t) {
        const c = t.config || {};
        if (t.type === "interval") {
            const baseVal = toDatetimeLocal(c.base_time) || defaultBaseTime();
            return `
                <div class="trigger-config-fields">
                    <div class="full-row"><label class="form-hint">基准时间</label>
                        <input type="datetime-local" class="form-input" data-field="base_time" value="${escapeHtml(baseVal)}"></div>
                    <div class="full-row interval-row">
                        <span class="interval-duration-label">周期时长</span>
                        <input type="number" class="form-input" data-field="days" min="0" value="${parseInt(c.days) || 0}"><span class="unit">天</span>
                        <input type="number" class="form-input" data-field="hours" min="0" value="${parseInt(c.hours) || 0}"><span class="unit">小时</span>
                        <input type="number" class="form-input" data-field="minutes" min="0" value="${parseInt(c.minutes) || 0}"><span class="unit">分钟</span>
                    </div>
                </div>`;
        }
        if (t.type === "cron") {
            return `
                <div class="trigger-config-fields">
                    <div class="full-row"><label class="form-hint">cron 表达式（5段：分 时 日 月 周）</label>
                        <input type="text" class="form-input" data-field="expr" placeholder="0 */4 * * *" value="${escapeHtml(c.expr || "")}"></div>
                </div>`;
        }
        if (t.type === "window") {
            const wd = Array.isArray(c.weekdays) ? c.weekdays : [];
            return `
                <div class="trigger-config-fields">
                    <div><label class="form-hint">起始 HH:MM</label><input type="time" class="form-input" data-field="start" value="${escapeHtml(c.start || "08:00")}"></div>
                    <div><label class="form-hint">结束 HH:MM</label><input type="time" class="form-input" data-field="end" value="${escapeHtml(c.end || "20:00")}"></div>
                    <div class="full-row"><label class="form-hint">星期</label>
                        <div class="weekdays-group">
                            ${WEEKDAY_CN.map((d, i) => `<label class="weekday-chip ${wd.includes(i) ? "checked" : ""}"><input type="checkbox" data-weekday="${i}" ${wd.includes(i) ? "checked" : ""}>${d}</label>`).join("")}
                        </div>
                    </div>
                </div>`;
        }
        if (t.type === "random") {
            return `
                <div class="trigger-config-fields">
                    <div><label class="form-hint">概率值 (0-1)</label>
                        <input type="number" class="form-input" data-field="threshold" min="0" max="1" step="0.01" value="${c.threshold != null ? c.threshold : 0.5}"></div>
                </div>`;
        }
        return `
            <div class="trigger-config-fields">
                <div class="full-row interval-row">
                    <input type="number" class="form-input" data-field="hours" min="0" value="${parseInt(c.hours) || 0}"><span class="unit">小时</span>
                    <input type="number" class="form-input" data-field="minutes" min="0" value="${parseInt(c.minutes) || 0}"><span class="unit">分钟</span>
                </div>
            </div>`;
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

    // ── 发送对象（UMO tag 多选） ──
    function renderTargetPicker() {
        $("target-select").innerHTML = `<option value="">选择活跃会话…</option>` +
            sessionsList.map(s => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join("");
    }
    function renderTargets() {
        const box = $("targets-tags");
        if (!targets.length) {
            box.innerHTML = `<span class="empty-hint">尚未选择发送对象</span>`;
            return;
        }
        box.innerHTML = targets.map((u, i) =>
            `<span class="umo-tag">${escapeHtml(u)}<span class="umo-tag-close" data-idx="${i}" title="移除">×</span></span>`
        ).join("");
        box.querySelectorAll(".umo-tag-close").forEach(el => {
            el.addEventListener("click", () => {
                targets.splice(parseInt(el.dataset.idx), 1);
                renderTargets();
            });
        });
    }
    function addTarget(value) {
        const v = String(value || "").trim();
        if (!v || targets.includes(v)) return;
        targets.push(v);
        renderTargets();
    }
    $("target-select").addEventListener("change", () => {
        addTarget($("target-select").value);
        $("target-select").value = "";
    });
    $("target-custom").addEventListener("keydown", e => {
        if (e.key !== "Enter") return;
        e.preventDefault();
        addTarget($("target-custom").value);
        $("target-custom").value = "";
    });

    // ── 任务内容模式说明 ──
    const MODE_INFO = {
        fixed: {
            detail: "直接把下方文本作为消息发送到目标，不经过 AI。",
            label: "发送内容",
        },
        standalone: {
            detail: "使用上方「独立 AI 系统提示」+ 下方任务提示词单独调用 AI 生成回复，不携带对话人格、不与其他插件互动。",
            label: "任务提示词",
        },
        conversation: {
            detail: "把下方任务提示词交给目标对话配置的 AI 生成回复，携带该对话的人格 system prompt 与记忆插件注入。",
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
            ? "下次检查触发时间: " + fmtTs(lastValidate.next_fire)
            : "下次检查触发时间：待计算…";
        el.style.color = "var(--color-primary)";
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
        el.textContent = fires.length ? "未来触发: " + fires.map(f => fmtTs(f)).join("，") : "未来触发：请完成配置……";
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
        const payload = triggers.map(t => ({ id: t.id, type: t.type, config: t.config }));
        try {
            const res = await bridge.apiPost("validate", { triggers: payload });
            if (!res || res.status !== "success") return;
            lastValidate = res.results || { triggers: [], next_fire: null };
            updateValidateHints();
            updatePanelNextFire();
            updateCanvasErrors();
        } catch (e) { /* 静默 */ }
    }

    // ── 画布平移（按住空白处拖动） ──
    const canvasViewport = $("trigger-canvas-viewport");
    let panState = null;
    canvasViewport.addEventListener("mousedown", e => {
        if (e.target.closest(".tnode")) return;
        panState = { x: e.clientX, y: e.clientY, sl: canvasViewport.scrollLeft, st: canvasViewport.scrollTop };
        canvasViewport.classList.add("panning");
        e.preventDefault();
    });
    window.addEventListener("mousemove", e => {
        if (!panState) return;
        canvasViewport.scrollLeft = panState.sl - (e.clientX - panState.x);
        canvasViewport.scrollTop = panState.st - (e.clientY - panState.y);
    });
    window.addEventListener("mouseup", () => {
        if (!panState) return;
        panState = null;
        canvasViewport.classList.remove("panning");
    });

    // ── 提交任务 ──
    $("task-form").addEventListener("submit", async e => {
        e.preventDefault();
        const name = $("task-name").value.trim();
        if (!name) { showToast("请填写任务名称", true); return; }
        if (!triggers.length) { showToast("至少需要一个触发器", true); return; }
        if (!activeTriggers().length) { showToast("至少需要一个主动型触发器（周期型或 cron 型）", true); return; }
        if (!targets.length && $("task-text").value.trim()) { showToast("请至少选择一个发送对象", true); return; }
        const payload = {
            id: $("task-id").value || undefined,
            name,
            enabled: $("task-enabled").checked,
            triggers: triggers.map(t => ({ id: t.id, type: t.type, config: t.config })),
            content: {
                text: $("task-text").value,
                mode: $("task-mode").value,
                system_prompt: $("task-system-prompt").value,
                time_aware: $("task-time-aware").checked,
                holiday_aware: $("task-holiday-aware").checked,
            },
            targets: targets.slice(),
        };
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
            btn.disabled = false; btn.textContent = "保存";
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
        confirmModal.classList.add("active");
    }
    function closeConfirm() { confirmModal.classList.remove("active"); pendingDeleteId = null; confirmMode = "delete"; }
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
            poll_interval: parseInt($("config-poll-interval").value) || 60,
            log_retention: parseInt($("config-log-retention").value) || 200,
            llm_timeout: parseInt($("config-llm-timeout").value) || 60,
            llm_log_retention: parseInt($("config-llm-log-retention").value) || 500,
        };
        const btn = $("save-config-btn");
        btn.disabled = true; btn.textContent = "保存中...";
        try {
            const res = await bridge.apiPost("save_config", payload);
            if (res && res.status === "success") showToast("配置已保存");
            else showToast("保存失败: " + (res && res.message || ""), true);
        } catch (e) { showToast("保存失败: " + e.message, true); }
        finally { btn.disabled = false; btn.textContent = "保存配置"; }
    });

    $("refresh-logs-btn").addEventListener("click", loadData);

    // ── 初始化 ──
    await loadSessions();
    await loadData();
    setInterval(() => {
        if ($("view-logs").classList.contains("active")) loadData();
    }, 8000);
}

if (document.readyState === "complete" || document.readyState === "interactive") init();
else document.addEventListener("DOMContentLoaded", init);
