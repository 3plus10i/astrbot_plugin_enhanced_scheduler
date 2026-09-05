/* 增强计划任务插件前端逻辑 */
async function init() {
    const bridge = window.AstrBotPluginPage;
    try { await bridge.ready(); } catch (e) { console.error("bridge 连接失败:", e); }

    let allTasks = {};
    let allLogs = [];
    let sessionsList = [];
    let validateTimer = null;
    let defaultPrompt = "";

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
    // JS getDay: 0=周日..6=周六 -> Python weekday: 0=周一..6=周日
    function pyWeekday(d) { return (d.getDay() + 6) % 7; }
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
            if (btn.getAttribute("data-tab") === "llm" && !llmLoaded) loadLlmRaw();
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
            defaultPrompt = res.default_standalone_prompt || "";
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
            const trigSummary = (t.triggers || []).map(tr => {
                const cn = {interval:"周期",cron:"cron",window:"区间",random:"随机",cooldown:"冷却"}[tr.type] || tr.type;
                return `<span class="badge badge-fixed">#${tr.id} ${cn}</span> `;
            }).join(" ");
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
                    <td><code>${escapeHtml(t.logic_expr)}</code></td>
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
            return;
        }
        const sorted = allLogs.slice().reverse();
        body.innerHTML = sorted.map(l => {
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
    }

    function renderConfig(cfg) {
        $("config-poll-interval").value = cfg.poll_interval != null ? cfg.poll_interval : 60;
        $("config-log-retention").value = cfg.log_retention != null ? cfg.log_retention : 200;
        $("config-llm-timeout").value = cfg.llm_timeout != null ? cfg.llm_timeout : 60;
        $("config-llm-log-retention").value = cfg.llm_log_retention != null ? cfg.llm_log_retention : 10;
    }

    // ── LLM 调用日志（原始文件内容，不渲染） ──
    let llmLoaded = false;
    async function loadLlmRaw() {
        try {
            const res = await bridge.apiGet("get_llm_logs_raw", {});
            if (!res || res.status !== "success") { showToast("获取 LLM 日志失败", true); return; }
            const content = res.content || "";
            $("llm-raw-content").textContent = content ? content : "暂无记录。";
            llmLoaded = true;
        } catch (e) { showToast("加载 LLM 日志失败: " + e.message, true); }
    }
    $("refresh-llm-btn").addEventListener("click", loadLlmRaw);
    $("clear-llm-btn").addEventListener("click", () => {
        confirmMode = "clear-llm";
        $("confirm-modal-text").textContent = "确定要清空所有 LLM 调用日志吗？此操作不可撤销。";
        confirmModal.classList.add("active");
    });

    function attachTaskRowListeners() {
        document.querySelectorAll(".edit-task-btn").forEach(b => b.addEventListener("click", () => openTaskModal("edit", b.getAttribute("data-id"))));
        document.querySelectorAll(".copy-task-btn").forEach(b => b.addEventListener("click", () => copyTask(b.getAttribute("data-id"))));
        document.querySelectorAll(".trigger-now-btn").forEach(b => b.addEventListener("click", () => triggerNow(b.getAttribute("data-id"))));
        document.querySelectorAll(".delete-task-btn").forEach(b => b.addEventListener("click", () => confirmDelete(b.getAttribute("data-id"))));
    }

    // ── 任务编辑弹窗 ──
    const modal = $("task-modal");
    function openModal() { modal.classList.add("active"); }
    function closeModal() { modal.classList.remove("active"); $("task-form").reset(); $("triggers-list").innerHTML = ""; $("targets-list").innerHTML = ""; }
    $("modal-close-btn").addEventListener("click", closeModal);
    $("modal-cancel-btn").addEventListener("click", closeModal);
    // 表单内容太多，点击遮罩层关闭容易误操作，暂时禁用
    // modal.addEventListener("click", e => { if (e.target === modal) closeModal(); });

    $("add-task-btn").addEventListener("click", () => openTaskModal("add", null));

    function openTaskModal(action, id) {
        $("task-action").value = action;
        $("task-id").value = id || "";
        $("triggers-list").innerHTML = "";
        $("targets-list").innerHTML = "";
        $("task-logic").value = "";
        $("task-text").value = "";

        if (action === "edit" && allTasks[id]) {
            const t = allTasks[id];
            $("modal-title-text").textContent = "编辑: " + t.name;
            $("task-name").value = t.name || "";
            $("task-enabled").checked = t.enabled !== false;
            $("task-logic").value = t.logic_expr || "";
            const m = (t.content && t.content.mode) || ((t.content && t.content.use_llm) ? "conversation" : "fixed");
            $("task-mode").value = m;
            $("task-text").value = (t.content && t.content.text) || "";
            $("task-system-prompt").value = (t.content && t.content.system_prompt) || defaultPrompt;
            (t.triggers || []).forEach(tr => addTriggerRow(tr));
            (t.targets || []).forEach(tg => addTargetRow(tg));
        } else {
            $("modal-title-text").textContent = "新增计划任务";
            $("task-enabled").checked = true;
            $("task-mode").value = "fixed";
            $("task-system-prompt").value = defaultPrompt;
            const firstId = addTriggerRow(null);
            $("task-logic").value = String(firstId);
            addTargetRow("");
        }
        updateContentMode();
        renderTemplateVars();
        scheduleValidate();
        openModal();
    }

    // ── 触发器组管理 ──
    const TYPE_LABELS = {
        interval: { label: "主动-周期型", desc: "从基准时间起，按固定周期累加触发；主动到点。" },
        cron: { label: "主动-cron型", desc: "按标准 5 段 cron 表达式（分 时 日 月 周）触发；主动到点。" },
        window: { label: "被动-区间型", desc: "每天指定时间段内为真；仅被逻辑规则引用时实时求值。" },
        random: { label: "被动-随机型", desc: "以给定概率为真；仅被逻辑规则引用时采样一次。" },
        cooldown: { label: "被动-冷却型", desc: "距上次成功发送超过冷却时长为真；仅被逻辑规则引用时实时求值。" },
    };

    function nextAvailableTriggerId() {
        const used = new Set();
        document.querySelectorAll("#triggers-list .trigger-card").forEach(c => used.add(parseInt(c.dataset.tid)));
        let id = 1;
        while (used.has(id)) id++;
        return id;
    }

    function addTriggerRow(data) {
        const list = $("triggers-list");
        const id = (data && data.id) || nextAvailableTriggerId();
        const card = document.createElement("div");
        card.className = "trigger-card";
        card.dataset.tid = id;
        const type = (data && data.type) || "interval";
        const cfg = (data && data.config) || {};
        card.innerHTML = `
            <div class="trigger-card-header">
                <span class="trigger-id-badge">${id}</span>
                <select class="trigger-type-select">
                    ${Object.keys(TYPE_LABELS).map(k => `<option value="${k}" ${k===type?'selected':''}>${TYPE_LABELS[k].label}</option>`).join("")}
                </select>
                <button class="btn btn-danger btn-sm remove-trigger-btn" type="button"><span>删除</span></button>
            </div>
            <div class="trigger-type-desc">${TYPE_LABELS[type].desc}</div>
            <div class="trigger-config-fields"></div>
            <div class="trigger-next-fire"></div>
        `;
        list.appendChild(card);
        renderTriggerFields(card, type, cfg);
        card.querySelector(".trigger-type-select").addEventListener("change", e => {
            card.querySelector(".trigger-type-desc").textContent = TYPE_LABELS[e.target.value].desc;
            renderTriggerFields(card, e.target.value, {});
            renderTemplateVars();
            scheduleValidate();
        });
        card.querySelector(".remove-trigger-btn").addEventListener("click", () => {
            card.remove();
            renderTemplateVars();
            scheduleValidate();
        });
        card.addEventListener("input", scheduleValidate);
        card.addEventListener("change", scheduleValidate);
        return id;
    }

    function renderTriggerFields(card, type, cfg) {
        const fields = card.querySelector(".trigger-config-fields");
        let html = "";
        if (type === "interval") {
            const baseVal = toDatetimeLocal(cfg.base_time) || defaultBaseTime();
            html = `
                <div class="full-row interval-row">
                    <span class="interval-duration-label">周期时长</span>
                    <input type="number" class="form-input field-days" min="0" value="${cfg.days||0}"><span class="unit">天</span>
                    <input type="number" class="form-input field-hours" min="0" value="${cfg.hours||0}"><span class="unit">小时</span>
                    <input type="number" class="form-input field-minutes" min="0" value="${cfg.minutes||0}"><span class="unit">分钟</span>
                </div>
                <div class="full-row"><label class="form-hint">基准时间（从此时间起按周期累加触发，默认=创建当天 0:00）</label><input type="datetime-local" class="form-input field-base-time" value="${escapeHtml(baseVal)}"></div>
            `;
        } else if (type === "cron") {
            html = `
                <div class="full-row"><label class="form-hint">cron 表达式（5段：分 时 日 月 周）</label><input type="text" class="form-input field-expr" placeholder="0 */4 * * *" value="${escapeHtml(cfg.expr||'')}"></div>
            `;
        } else if (type === "window") {
            const wd = cfg.weekdays || [];
            const days = ["周一","周二","周三","周四","周五","周六","周日"];
            html = `
                <div><label class="form-hint">起始 HH:MM</label><input type="time" class="form-input field-start" value="${escapeHtml(cfg.start||'08:00')}"></div>
                <div><label class="form-hint">结束 HH:MM</label><input type="time" class="form-input field-end" value="${escapeHtml(cfg.end||'20:00')}"></div>
                <div class="full-row"><label class="form-hint">星期（不选=每天）</label>
                    <div class="weekdays-group">
                        ${days.map((d,i) => `<label class="weekday-chip ${wd.includes(i)?'checked':''}"><input type="checkbox" value="${i}" ${wd.includes(i)?'checked':''}>${d}</label>`).join("")}
                    </div>
                </div>
            `;
        } else if (type === "random") {
            html = `<div><label class="form-hint">概率值 (0-1)</label><input type="number" class="form-input field-threshold" min="0" max="1" step="0.01" value="${cfg.threshold!=null?cfg.threshold:0.5}"></div>`;
        } else if (type === "cooldown") {
            html = `
                <div><label class="form-hint">小时</label><input type="number" class="form-input field-hours" min="0" value="${cfg.hours||0}"></div>
                <div><label class="form-hint">分钟</label><input type="number" class="form-input field-minutes" min="0" value="${cfg.minutes||0}"></div>
            `;
        }
        fields.innerHTML = html;
        fields.querySelectorAll(".weekday-chip").forEach(chip => {
            const cb = chip.querySelector("input");
            cb.addEventListener("change", () => {
                chip.classList.toggle("checked", cb.checked);
            });
        });
    }

    function collectTriggers() {
        const triggers = [];
        document.querySelectorAll("#triggers-list .trigger-card").forEach(card => {
            const id = parseInt(card.dataset.tid);
            const type = card.querySelector(".trigger-type-select").value;
            const cfg = {};
            if (type === "interval") {
                cfg.days = parseInt(card.querySelector(".field-days").value) || 0;
                cfg.hours = parseInt(card.querySelector(".field-hours").value) || 0;
                cfg.minutes = parseInt(card.querySelector(".field-minutes").value) || 0;
                cfg.base_time = card.querySelector(".field-base-time").value.trim();
            } else if (type === "cron") {
                cfg.expr = card.querySelector(".field-expr").value.trim();
            } else if (type === "window") {
                cfg.start = card.querySelector(".field-start").value || "00:00";
                cfg.end = card.querySelector(".field-end").value || "23:59";
                cfg.weekdays = Array.from(card.querySelectorAll(".weekday-chip input:checked")).map(c => parseInt(c.value));
            } else if (type === "random") {
                cfg.threshold = parseFloat(card.querySelector(".field-threshold").value);
            } else if (type === "cooldown") {
                cfg.hours = parseInt(card.querySelector(".field-hours").value) || 0;
                cfg.minutes = parseInt(card.querySelector(".field-minutes").value) || 0;
            }
            triggers.push({ id, type, config: cfg });
        });
        return triggers;
    }

    function appendLogicFor(id) {
        const input = $("task-logic");
        const cur = input.value.trim();
        input.value = cur ? `${cur}*${id}` : String(id);
    }
    $("add-trigger-btn").addEventListener("click", () => {
        const newId = addTriggerRow(null);
        appendLogicFor(newId);
        renderTemplateVars();
        scheduleValidate();
    });

    // ── 发送对象管理 ──
    function sessionOptionList(extraValue) {
        const ordered = [];
        const seen = new Set();
        sessionsList.forEach(s => { if (!seen.has(s)) { seen.add(s); ordered.push(s); } });
        if (extraValue && !seen.has(extraValue)) ordered.push(extraValue);
        return ordered.map(s => `<option value="${escapeHtml(s)}">${escapeHtml(maskUmo(s))}</option>`).join("");
    }
    function addTargetRow(value) {
        const list = $("targets-list");
        const row = document.createElement("div");
        row.className = "target-row";
        row.innerHTML = `
            <select class="target-select form-input">
                <option value="">选择会话…</option>
                <option value="__custom__">手动输入 UMO…</option>
                ${sessionOptionList(value)}
            </select>
            <input type="text" class="target-input form-input" placeholder="default:FriendMessage:..." style="display:none">
            <button class="btn btn-danger btn-sm remove-target-btn" type="button"><span>删除</span></button>
        `;
        list.appendChild(row);
        const sel = row.querySelector(".target-select");
        const inp = row.querySelector(".target-input");
        if (value) {
            if (sessionsList.includes(value)) {
                sel.value = value;
            } else {
                sel.value = "__custom__";
                inp.value = value;
                inp.style.display = "block";
            }
        }
        sel.addEventListener("change", () => {
            if (sel.value === "__custom__") { inp.style.display = "block"; inp.focus(); }
            else { inp.style.display = "none"; }
        });
        row.querySelector(".remove-target-btn").addEventListener("click", () => { row.remove(); });
    }
    function collectTargets() {
        return Array.from(document.querySelectorAll("#targets-list .target-row")).map(row => {
            const sel = row.querySelector(".target-select");
            if (sel.value === "__custom__") return row.querySelector(".target-input").value.trim();
            return sel.value;
        }).filter(v => v);
    }
    $("add-target-btn").addEventListener("click", () => { addTargetRow(""); });

    // ── 任务内容模式说明 ──
    const MODE_INFO = {
        fixed: {
            detail: "将下方文本（渲染 {{变量}} 后）直接作为消息发送到目标，不经过 AI。",
            requirement: "输入要求：要发送的固定消息，支持 {{time}} {{date}} {{weekday}} 及触发器结果 {{n}}（n=触发器编号）。",
        },
        standalone: {
            detail: "使用上方「独立 AI 系统提示」作为 system prompt + 下方任务提示词，单独调用 AI 生成回复后发送，不携带对话人格、不与其他插件互动。",
            requirement: "输入要求：给 AI 的任务提示词（作为 user 消息），支持 {{time}} {{date}} {{weekday}} 及触发器结果 {{n}}（n=触发器编号）。",
        },
        conversation: {
            detail: "将下方任务提示词交给目标对话配置的 AI 生成回复后发送，会携带该对话的人格 system prompt 及记忆插件注入。",
            requirement: "输入要求：给 AI 的任务提示词（作为 user 消息），支持 {{time}} {{date}} {{weekday}} 及触发器结果 {{n}}（n=触发器编号）。",
        },
    };
    function updateContentMode() {
        const mode = $("task-mode").value;
        const info = MODE_INFO[mode] || MODE_INFO.fixed;
        $("content-mode-detail").textContent = info.detail;
        $("content-mode-requirement").textContent = info.requirement;
        // 系统提示仅「独立 AI 回复」模式可编辑
        const sp = $("task-system-prompt");
        const isStandalone = mode === "standalone";
        sp.disabled = !isStandalone;
        sp.classList.toggle("disabled", !isStandalone);
    }
    $("task-mode").addEventListener("change", updateContentMode);

    // ── 模板变量及当前值（点击按钮插入到文本框光标处） ──
    function triggerResultPreview(type, cfg) {
        cfg = cfg || {};
        if (type === "interval") {
            const d = parseInt(cfg.days) || 0, h = parseInt(cfg.hours) || 0, m = parseInt(cfg.minutes) || 0;
            return `周期型触发器（周期${d}天${h}时${m}分）`;
        }
        if (type === "cron") return `CRON型触发器（${cfg.expr || "…"}）`;
        if (type === "window") {
            const wd = Array.isArray(cfg.weekdays) ? cfg.weekdays : [];
            const cn = ["一", "二", "三", "四", "五", "六", "日"];
            const scope = wd.length ? "每周" + wd.map(i => cn[i]).join("") : "每天";
            return `区间触发器：${scope}${cfg.start || "00:00"}-${cfg.end || "23:59"}`;
        }
        if (type === "random") {
            const th = (cfg.threshold != null && isFinite(cfg.threshold)) ? cfg.threshold : 0.5;
            return `随机触发器：?<${th}`;
        }
        if (type === "cooldown") {
            const h = parseInt(cfg.hours) || 0, m = parseInt(cfg.minutes) || 0;
            return `冷却触发器：大于${h}时${m}分`;
        }
        return "触发器结果";
    }
    function renderTemplateVars() {
        const now = new Date();
        const p = n => String(n).padStart(2, "0");
        const time = `${now.getFullYear()}-${p(now.getMonth()+1)}-${p(now.getDate())} ${p(now.getHours())}:${p(now.getMinutes())}:${p(now.getSeconds())}`;
        const date = `${now.getFullYear()}-${p(now.getMonth()+1)}-${p(now.getDate())}`;
        const weekday = "周" + "一二三四五六日"[pyWeekday(now)];
        const items = [
            { k: "time", v: time },
            { k: "date", v: date },
            { k: "weekday", v: weekday },
        ];
        // 触发器结果变量 {{id}}：按当前已添加的触发器动态生成，点击插入 {{n}}
        collectTriggers().forEach(tg => {
            items.push({ k: String(tg.id), v: triggerResultPreview(tg.type, tg.config || {}) });
        });
        $("template-vars").innerHTML = items.map(it =>
            `<button type="button" class="var-btn" data-tag="{{${it.k}}}"><code>{{${it.k}}}</code><span class="var-value">${escapeHtml(String(it.v))}</span></button>`
        ).join("");
        $("template-vars").querySelectorAll(".var-btn").forEach(btn => {
            btn.addEventListener("click", () => insertAtCursor($("task-text"), btn.getAttribute("data-tag")));
        });
    }

    function insertAtCursor(textarea, text) {
        const start = textarea.selectionStart;
        const end = textarea.selectionEnd;
        const val = textarea.value;
        textarea.value = val.slice(0, start) + text + val.slice(end);
        const pos = start + text.length;
        textarea.focus();
        textarea.setSelectionRange(pos, pos);
        textarea.dispatchEvent(new Event("input", { bubbles: true }));
    }

    // ── 逻辑规则实时校验 + 下次触发预览 ──
    function scheduleValidate() {
        clearTimeout(validateTimer);
        validateTimer = setTimeout(doValidate, 400);
    }
    async function doValidate() {
        const triggers = collectTriggers();
        const logic = $("task-logic").value.trim();
        const statusEl = $("logic-status");
        const nextEl = $("next-fire-hint");
        if (!triggers.length) {
            statusEl.textContent = "请先添加触发器";
            statusEl.style.color = "var(--color-warning)";
            nextEl.textContent = "";
            return;
        }
        if (!logic) {
            statusEl.textContent = "请填写逻辑规则";
            statusEl.style.color = "var(--color-warning)";
            nextEl.textContent = "";
            return;
        }
        try {
            const res = await bridge.apiPost("validate", { triggers, logic_expr: logic });
            if (!res || res.status !== "success") { return; }
            const r = res.results;
            updateTriggerNextFire(r.triggers);
            const trigErrs = (r.triggers || []).filter(t => !t.ok).map(t => `#${t.id}: ${t.msg}`);
            const logicOk = r.logic && r.logic.ok;
            if (trigErrs.length) {
                statusEl.innerHTML = "触发器配置有误: " + escapeHtml(trigErrs.join("; "));
                statusEl.style.color = "var(--color-danger)";
            } else if (logicOk) {
                statusEl.textContent = "√ 规则合法";
                statusEl.style.color = "var(--color-success)";
            } else {
                statusEl.textContent = "规则错误: " + (r.logic ? r.logic.msg : "未知");
                statusEl.style.color = "var(--color-danger)";
            }
            if (trigErrs.length) {
                nextEl.textContent = "";
            } else if (r.next_fire) {
                nextEl.textContent = "下次检查触发时间: " + fmtTs(r.next_fire);
                nextEl.style.color = "var(--color-primary)";
            } else {
                nextEl.textContent = "无主动触发器，不会自动触发";
                nextEl.style.color = "var(--text-secondary)";
            }
        } catch (e) { /* 静默 */ }
    }
    function updateTriggerNextFire(trigResults) {
        (trigResults || []).forEach(tr => {
            const card = document.querySelector(`#triggers-list .trigger-card[data-tid="${tr.id}"]`);
            if (!card) return;
            const el = card.querySelector(".trigger-next-fire");
            if (!el) return;
            const type = card.querySelector(".trigger-type-select").value;
            if (type === "interval" || type === "cron") {
                const fires = tr.future_fires || [];
                if (fires.length) {
                    el.textContent = "未来触发: " + fires.map(f => fmtTs(f)).join("，");
                    el.className = "trigger-next-fire active";
                } else {
                    el.textContent = "无法计算未来触发（请检查配置）";
                    el.className = "trigger-next-fire";
                }
            } else {
                el.textContent = "被动触发器：无固定触发点，被逻辑规则引用时实时求值";
                el.className = "trigger-next-fire";
            }
        });
    }
    $("task-logic").addEventListener("input", scheduleValidate);

    // ── 提交任务 ──
    $("task-form").addEventListener("submit", async e => {
        e.preventDefault();
        const name = $("task-name").value.trim();
        if (!name) { showToast("请填写任务名称", true); return; }
        const triggers = collectTriggers();
        if (!triggers.length) { showToast("至少需要一个触发器", true); return; }
        const logic = $("task-logic").value.trim();
        if (!logic) { showToast("请填写逻辑规则", true); return; }
        const payload = {
            id: $("task-id").value || undefined,
            name,
            enabled: $("task-enabled").checked,
            triggers,
            logic_expr: logic,
            content: { text: $("task-text").value, mode: $("task-mode").value, system_prompt: $("task-system-prompt").value },
            targets: collectTargets(),
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

    async function triggerNow(id) {
        try {
            const res = await bridge.apiPost("trigger_now", { id });
            if (res && res.status === "success") {
                showToast("已触发一次" + (res.detail ? "：" + res.detail : ""));
                await loadData();
            } else {
                showToast("触发失败: " + (res && res.message || ""), true);
            }
        } catch (e) { showToast("触发失败: " + e.message, true); }
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
                if (res && res.status === "success") { showToast("LLM 日志已清空"); closeConfirm(); llmLoaded = false; await loadLlmRaw(); }
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
