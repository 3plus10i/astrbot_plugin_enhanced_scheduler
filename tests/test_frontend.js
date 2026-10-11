/* Exercise the actual single-target form handlers without a browser or dependencies. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../pages/enhanced_scheduler/index.js"), "utf8");
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
    TYPE_INFO: {interval: {}}, nextTriggerId: 2, selectedTriggerId: 1,
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
console.log("Frontend checks passed: targets, JSON, name limit, empty action, lossless duration conversion, numeric bounds.");
