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
    });
    return elements.get(id);
}
const messages = [];
const context = vm.createContext({
    $, sessionsList: ["test:FriendMessage:one", "test:FriendMessage:two"],
    escapeHtml: value => value, showToast: value => messages.push(value),
    triggers: [{id: 1, type: "interval", config: {minutes: 1}}],
    TYPE_INFO: {interval: {}}, nextTriggerId: 2, selectedTriggerId: 1,
    renderTriggerCanvas() {}, renderTriggerPanel() {}, updateContentMode() {},
    updateValidateHints() {}, scheduleValidate() {},
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
console.log("Frontend checks passed: replacement, direct typing, JSON round-trip/rejection, empty-action validation.");
