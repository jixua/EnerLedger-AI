import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

/**
 * 按钮样式的链接不能带下划线。
 *
 * React Router 的 Link 渲染出来是 <a>，浏览器给 <a> 加的是默认下划线；
 * .button / .icon-button 只管边框、底色和字号，没人管文字装饰，于是
 * 「打开报告」这类按钮式链接就顶着一条下划线，跟旁边的真按钮不像一组操作。
 *
 * 这条线不会报错，只能在样式表和卡片源码里各钉一次：样式表负责全站，
 * 卡片源码负责「别再冒出一个裸 Link」。
 */

const stylesCss = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");
const pagesCss = await readFile(new URL("../src/pages.css", import.meta.url), "utf8");
const chatReportCard = await readFile(new URL("../src/components/ChatReportCard.jsx", import.meta.url), "utf8");

/** 取一条规则的声明体，用来断言它写了什么、没写什么 */
function declarations(css, selector) {
  const start = css.indexOf(`${selector} {`);
  assert.notEqual(start, -1, `样式表里找不到规则：${selector}`);
  return css.slice(start, css.indexOf("}", start));
}

test("按钮与图标按钮都显式去掉文字装饰", () => {
  const block = declarations(stylesCss, ".button, .icon-button");
  assert.match(block, /text-decoration:\s*none/, "按钮式链接又会带上浏览器默认下划线");
});

test("对话报告卡片里的链接一律显式给样式", () => {
  // 裸 Link 会退回浏览器默认样式：灰字 + 下划线，在卡片里既不像链接也不像按钮
  const bare = [...chatReportCard.matchAll(/<Link\b[^>]*>/g)]
    .map((match) => match[0])
    .filter((tag) => !/className=/.test(tag));
  assert.deepEqual(bare, [], `卡片里出现了没有样式的链接：${bare.join(" ")}`);

  assert.match(declarations(pagesCss, ".chat-report-card__link"), /text-decoration:\s*none/);
});
