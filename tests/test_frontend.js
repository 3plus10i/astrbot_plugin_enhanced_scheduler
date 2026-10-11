/* Exercise the actual single-target form handlers without a browser or dependencies. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../pages/enhanced_scheduler/index.js"), "utf8").replace(/\r\n/g, "\n");
const elements = new Map();
function $(id) {
    if (!elements.has(id)) elements.set(id, {
        value: "", checked: false, style: {}, innerHTML: "", listeners: {},
        addEventListener(event, callback) { this.listeners[event] = callback; },
        setAttribute() {},
    });
    return elements.get(id);
}
const messages = [];
const context = vm.createContext({
    taskNameMaxLength: 32,
    $, sessionsList: ["test:FriendMessage:one", "test:FriendMessage:two"],
    escapeHtml: value => value, showToast: value => messages.push(value),
    triggers: [{id: 1, type: "interval", config: {minutes: 1}}],
    TYPE_INFO: {interval: {}}, lastTriggerTimestamp: 0, selectedTriggerId: 1,
    renderTriggerCanvas() {}, renderTriggerPanel() {}, updateContentMode() {},
    updateValidateHints() {}, scheduleValidate() {},
    normalizeDurationConfig: (type, config) => config, updateEditorName() {}, chooseEditorTab() {},
    activeTriggers: () => [{}],
});

const picker = source.slice(source.indexOf("    // ── 单个发送目标"), source.indexOf("    // ── 任务内容模式说明"));
const form = source.slice(source.indexOf("    function buildTaskPayload()"), source.indexOf("    // ── 提交任务"));
vm.runInContext(picker + form, context);
$("task-name").value = "test";
$("task-text").value = "hello";
$("task-mode").value = "fixed";

// A second selection replaces the first; typed text is saved without pressing Enter.
$("target-select").value = "test:FriendMessage:one";
$("target-select").listeners.change();
$("target-select").value = "test:FriendMessage:two";
$("target-select").listeners.change();
assert.equal(context.buildTaskPayload().target, "test:FriendMessage:two");
$("target-custom").value = "custom:FriendMessage:typed";
$("target-custom").listeners.input();
assert.equal(context.buildTaskPayload().target, "custom:FriendMessage:typed");
assert.equal(context.buildTaskPayload().targets, undefined);

// JSON round-trip keeps exactly one target; an array cannot silently replace it.
context.setJsonMode(true);
assert.equal(JSON.parse($("task-json").value).target, "custom:FriendMessage:typed");
$("task-json").value = JSON.stringify({name: "json", target: "json:FriendMessage:one", content: {text: "hello"}, triggers: []});
$("task-json-toggle").listeners.click();
assert.equal($("target-custom").value, "json:FriendMessage:one");
context.setJsonMode(true);
$("task-json").value = JSON.stringify({targets: ["one", "two"]});
$("task-json-toggle").listeners.click();
assert.equal($("target-custom").value, "json:FriendMessage:one");
assert.match(messages.at(-1), /不再支持 targets/);
assert.equal($("task-form-body").style.display, "none");

// A message needs a target, while an empty action can omit it.
vm.runInContext('triggers = [{id: 1, type: "interval", config: {minutes: 1}}]', context);
$("target-custom").value = "";
assert.equal(context.validateForm(), false);
$("task-text").value = "";
assert.equal(context.validateForm(), true);
$("task-name").value = "中".repeat(32);
assert.equal(context.validateForm(), true);
$("task-name").value = "中".repeat(33);
assert.equal(context.validateForm(), false);

// JSON cannot bypass the name limit or partially apply an invalid task.
context.setJsonMode(true);
$("task-json").value = JSON.stringify({name: "x".repeat(33), target: "other", triggers: []});
$("task-json-toggle").listeners.click();
assert.equal($("target-custom").value, "");
assert.match(messages.at(-1), /32/);

// Returning to the form cannot silently convert removed legacy fields.
$("task-json").value = JSON.stringify({name: "old", content: {use_llm: true}, target: "other"});
$("task-json-toggle").listeners.click();
assert.match(messages.at(-1), /不再支持旧任务格式/);
assert.equal($("target-custom").value, "");

// Editing old oversized units must preserve the total duration exactly.
const durationSource = source.slice(source.indexOf("    function normalizeDurationConfig("), source.indexOf("    function renderTriggerCanvas("));
vm.runInContext(durationSource, context);
const normalized = context.normalizeDurationConfig("interval", {days: 0, hours: 26, minutes: 74, base_time: "2026-10-10T00:00"});
assert.equal(normalized.days, 1);
assert.equal(normalized.hours, 3);
assert.equal(normalized.minutes, 14);
assert.equal(normalized.days * 1440 + normalized.hours * 60 + normalized.minutes, 26 * 60 + 74);
const cooldown = context.normalizeDurationConfig("cooldown", {hours: 28, minutes: 74});
assert.equal(cooldown.hours, 29);
assert.equal(cooldown.minutes, 14);
const numericSource = source.slice(source.indexOf("    function clampNumericInput("), source.indexOf("    function renderTriggerFields("));
vm.runInContext(numericSource, context);
const minuteInput = {value: "74", min: "0", max: "59", step: "1"};
context.clampNumericInput(minuteInput);
assert.equal(minuteInput.value, "59");
minuteInput.value = "-3";
context.clampNumericInput(minuteInput);
assert.equal(minuteInput.value, "0");

// IDs stay unique even for same-millisecond additions, deletions, and a clock rollback.
let clock = 1791710000100;
const triggerContext = vm.createContext({
    Date: class extends Date { static now() { return clock; } },
    ACTIVE_TYPES: ["interval", "cron"], PASSIVE_TYPES: ["window", "random", "cooldown"],
    triggers: [{id: clock - 4, type: "random"}, {id: clock - 3, type: "cron"},
               {id: clock - 2, type: "window"}, {id: clock - 5, type: "interval"}],
    lastTriggerTimestamp: 0, selectedTriggerId: clock - 3, allTasks: {},
    defaultConfigFor: () => ({}), renderTriggerCanvas() {}, renderTriggerPanel() {},
    updateValidateHints() {}, scheduleValidate() {},
});
const helpers = source.slice(source.indexOf("    function probabilityText("), source.indexOf("    async function loadRunLogs("));
const ids = source.slice(source.indexOf("    function activeTriggers("), source.indexOf("    function defaultConfigFor("));
const mutations = source.slice(source.indexOf("    function removeTrigger("), source.indexOf('    $("add-active-trigger-btn")'));
vm.runInContext(helpers + ids + mutations, triggerContext);
const originalIds = Array.from(triggerContext.triggers, t => t.id);
assert.deepEqual({...triggerContext.triggerLabels(triggerContext.triggers)}, {
    [clock - 5]: "A1", [clock - 3]: "A2", [clock - 4]: "B1", [clock - 2]: "B2",
});
triggerContext.removeTrigger(clock - 3);
triggerContext.addTrigger("active");
const firstNewId = triggerContext.selectedTriggerId;
assert.equal(firstNewId, clock);
assert.equal(triggerContext.triggerLabels(triggerContext.triggers)[firstNewId], "A2");
triggerContext.addTrigger("passive");
const secondNewId = triggerContext.selectedTriggerId;
assert.equal(secondNewId, firstNewId + 1);
triggerContext.removeTrigger(firstNewId);
clock -= 100;
triggerContext.addTrigger("active");
assert.equal(triggerContext.selectedTriggerId, secondNewId + 1);
assert.equal(triggerContext.triggerLabels(triggerContext.triggers)[triggerContext.selectedTriggerId], "A2");
triggerContext.removeTrigger(originalIds[0]);
assert.equal(triggerContext.triggerLabels(triggerContext.triggers)[originalIds[2]], "B1");
assert.equal(triggerContext.triggerLabels(triggerContext.triggers)[secondNewId], "B2");
assert.equal(new Set(Array.from(triggerContext.triggers, t => t.id)).size, triggerContext.triggers.length);

// Historical log labels come from the event snapshot, regardless of current numbering.
triggerContext.allTasks.changed = {triggers: [{id: 200, type: "random"}]};
assert.equal(triggerContext.formatTriggerResults({task_id: "changed", trigger_tof: {200: false, 300: true, 100: true},
    trigger_labels: {100: "A1", 200: "B1", 300: "B2"}}), "A1 ✓，B1 ✗，B2 ✓");
assert.equal(triggerContext.probabilityText(0.25), "以25%概率执行");
assert.equal(triggerContext.probabilityText(1), "以100%概率执行");
assert.notEqual(triggerContext.probabilityText(0.00000001), "以0%概率执行");

// Footer distinguishes pending, impossible, invalid, and probabilistic task times.
const hintContext = vm.createContext({
    $, lastValidate: {}, activeTriggers: () => [{}],
    fmtTs: () => "2026-10-11 17:28:00", probabilityText: triggerContext.probabilityText,
});
vm.runInContext(source.slice(source.indexOf("    function updateValidateHints("), source.indexOf("    function updatePanelNextFire(")), hintContext);
hintContext.lastValidate = {next_fire: 1791710880, execution_probability: 0.25};
hintContext.updateValidateHints();
assert.equal($("next-fire-hint").textContent, "下次任务时间：2026-10-11 17:28:00 以25%概率执行");
hintContext.lastValidate = {next_fire: null, execution_probability: 0, now: 1};
hintContext.updateValidateHints();
assert.match($("next-fire-hint").textContent, /不会执行.*0%/);
hintContext.lastValidate = {next_fire: null, execution_probability: 1, now: 1};
hintContext.updateValidateHints();
assert.match($("next-fire-hint").textContent, /无可执行/);
hintContext.lastValidate = {};
hintContext.updateValidateHints();
assert.match($("next-fire-hint").textContent, /待计算/);

// One 15-second timer refreshes the visible page; editing JSON and hidden pages stay intact.
(async () => {
    const calls = [], timers = [];
    let activeView = "tasks";
    const refreshDocument = {hidden: false, querySelectorAll: () => []};
    const refreshModal = {open: false};
    const menu = {hidden: true};
    const refreshContext = vm.createContext({
        document: refreshDocument, modal: refreshModal, jsonMode: false, llmPage: 2, logsPage: 3,
        $: id => id === "task-menu" ? menu : {classList: {contains: () => id === "view-" + activeView}},
        loadSessions: async () => calls.push("sessions"), loadData: async () => calls.push("data"),
        doValidate: async () => calls.push("validate"),
        loadRunLogs: async page => calls.push(["logs", page]),
        loadLlmLogs: async (page, background) => calls.push(["llm", page, background]),
        setInterval: (fn, ms) => timers.push({fn, ms}),
    });
    vm.runInContext(source.match(/const AUTO_REFRESH_INTERVAL_MS = \d+;/)[0], refreshContext);
    const startup = source.slice(source.indexOf("    // ── 初始化 ──"), source.lastIndexOf("\n}\n\nif (document.readyState"));
    await vm.runInContext(`(async () => {${startup}})()`, refreshContext);
    assert.equal(timers.length, 1);
    assert.equal(timers[0].ms, 15000);
    calls.length = 0;
    refreshModal.open = true;
    await timers[0].fn();
    assert.deepEqual(calls.splice(0), ["validate"]);
    refreshContext.jsonMode = true;
    await timers[0].fn();
    assert.equal(calls.length, 0);
    refreshModal.open = false;
    activeView = "logs";
    await timers[0].fn();
    assert.deepEqual(calls.splice(0), [["logs", 3]]);
    activeView = "llm";
    await timers[0].fn();
    assert.deepEqual(calls.splice(0), [["llm", 2, true]]);
    activeView = "settings";
    await timers[0].fn();
    assert.deepEqual(calls.splice(0), ["data"]);
    refreshDocument.hidden = true;
    await timers[0].fn();
    assert.equal(calls.length, 0);
    refreshDocument.hidden = false;
    activeView = "tasks";
    menu.hidden = false;
    await timers[0].fn();
    assert.equal(calls.length, 0);

    // An API request lasting more than an interval cannot accumulate repeated requests.
    activeView = "logs";
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    refreshContext.loadRunLogs = async () => { calls.push("logs"); await gate; };
    const ongoing = timers[0].fn();
    await timers[0].fn();
    assert.deepEqual(calls, ["logs"]);
    release();
    await ongoing;
    console.log("Frontend checks passed: forms, numeric bounds, stable IDs, continuous numbering, log labels, task probability, and the shared 15-second refresh timer.");
})().catch(error => { console.error(error); process.exitCode = 1; });
