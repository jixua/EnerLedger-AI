import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const read = (path) => readFile(new URL(`../src/${path}`, import.meta.url), "utf8");

const [playground, session, sse] = await Promise.all([
  read("pages/PlaygroundPage.jsx"),
  read("state/ChatSessionContext.jsx"),
  read("lib/sse.js"),
]);

/**
 * @ 文档：在对话里引用一份知识库文档，然后生成报告或让 AI 解释它。
 *
 * 这两件事后端都已经有能力——知识库文档作为附件的链路（角色判定、来源冻结、
 * 召回收窄到这份文档）早就在，缺的只是前端入口。所以这里钉的是入口该做的事，
 * 以及一件不该做的事：别为「生成报告 / 解释」这两个按钮另造一套路由。
 */

test("@ 的识别与插入只有一份实现", () => {
  assert.match(playground, /from "\.\.\/lib\/doc-mention"/);
  assert.match(playground, /mentionQueryAt\(/);
  assert.match(playground, /applyMention\(/);
  // 页面里不该再写一套 @ 的正则或候选过滤
  assert.doesNotMatch(playground, /lastIndexOf\("@?"\)/, "输入框里另写了一套 @ 识别");
});

test("引用的文档按主体上报：document_id + role=SOURCE", () => {
  // 走 document_id 而不是 material_id：@ 的是知识库里的文档，不是本轮上传的材料
  assert.match(session, /document_id: documentIdOf\(activeMention\)/);
  assert.match(session, /role: "SOURCE"/);
  // 声明为主体，避免一份长得像报告模板的文档被当成模板
  assert.doesNotMatch(session, /document_id: documentIdOf\(activeMention\),\s*material_id/);
});

test("引用只对本轮有效：发送后清空", () => {
  assert.match(session, /setMentionedDocument\(null\)/);
});

test("候选只给当前范围内、已经可检索的文档", () => {
  assert.match(session, /mentionableDocuments/);
  assert.match(session, /isDocumentRetrievalReady/);
});

test("两个快捷入口只写句子，不另造一条生成报告的路", () => {
  assert.match(playground, /用它生成报告/);
  assert.match(playground, /解释这份文档/);
  assert.match(playground, /composeMentionIntent\("report"\)/);
  assert.match(playground, /composeMentionIntent\("explain"\)/);
  // 按钮只是把「用 @X 生成报告」写进输入框，真正的判定仍在后端意图识别那一条路上
  assert.doesNotMatch(playground, /createReport|createDocumentReport/, "按钮不该直接调建报告的接口");
});

test("附件形状：知识库文档带 document_id，材料带 material_id", () => {
  assert.match(sse, /document_id: attachment\.documentId \?\? attachment\.document_id/);
  assert.match(sse, /material_id: attachment\.material_id \?\? attachment\.materialId/);
});

test("候选列表翻不完时说一声，而不是默默截断", () => {
  assert.match(playground, /mentionTruncated/);
  assert.match(playground, /继续输入可筛选/);
});

test("输入框里的引用着色：覆盖层与输入框逐字对齐", async () => {
  const css = await read("pages.css");

  assert.match(playground, /composer-highlight/);
  assert.match(playground, /mentionRuns\(/);
  // 盒模型与字体写在同一条规则里：分头写，改了一处忘了另一处就会错行
  assert.match(css, /\.chat-composer textarea,\s*\.composer-highlight \{/);

  // 引用片段只许改颜色和底色。加内边距或字重会改变字宽，覆盖层的字就与输入框错开
  const start = css.indexOf(".composer-highlight__mention {");
  assert.notEqual(start, -1, "找不到引用片段的样式");
  const body = css.slice(start, css.indexOf("}", start));
  assert.doesNotMatch(body, /padding|font-weight|letter-spacing|font-size/, "引用样式改变了字宽");

  // 组字期间把颜色交还给输入框，否则中文输入法看不见正在打的字
  assert.match(css, /\.composer-input\.is-composing textarea \{ color: var\(--ink\)/);
  assert.match(playground, /onCompositionStart/);
});
