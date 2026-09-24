import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

import {
  listItems,
  metricItems,
  seriesItems,
  tableColumns,
  tableRows,
} from "../src/lib/reportBlocks.js";

/**
 * IR 的读法只认一种拼法，另一种拼法下的整块内容就会在页面上凭空消失。
 *
 * 下面这些 payload 都是真实报告里出现过的：同一条链路，模型一次写 items、一次写
 * cards；一次写 series、一次写 categories + values。放在一起钉住，免得下次又只
 * 认其中一个。
 */

test("指标卡：items 与 cards 都读得出来", () => {
  const canonical = {
    type: "metric_cards",
    data: { items: [{ label: "功能单位", value: "1 P单元" }] },
  };
  const legacy = {
    type: "metric_cards",
    data: {
      cards: [
        { label: "产品名称", value: "MCB微型断路器" },
        { label: "碳足迹总值", value: "0.44131 kgCO2e" },
      ],
    },
  };

  assert.deepEqual(metricItems(canonical).map((item) => item.label), ["功能单位"]);
  assert.deepEqual(
    metricItems(legacy).map((item) => item.label),
    ["产品名称", "碳足迹总值"]
  );
});

test("图表：series 与 categories/labels + values 都读得出来", () => {
  const canonical = {
    type: "bar_chart",
    data: { unit: "kgCO2e", series: [{ label: "原料获取", value: 0.39204 }] },
  };
  const barLegacy = {
    type: "bar_chart",
    data: { unit: "kgCO2e", categories: ["原料获取", "制造阶段"], values: [0.39204, 0.03411] },
  };
  const donutLegacy = {
    type: "donut_chart",
    data: { labels: ["PE粒子", "钢材"], values: [32.69, 0.62] },
  };

  assert.deepEqual(seriesItems(canonical), [{ label: "原料获取", value: 0.39204 }]);
  assert.deepEqual(seriesItems(barLegacy), [
    { label: "原料获取", value: 0.39204 },
    { label: "制造阶段", value: 0.03411 },
  ]);
  assert.deepEqual(seriesItems(donutLegacy), [
    { label: "PE粒子", value: 32.69 },
    { label: "钢材", value: 0.62 },
  ]);
});

test("表格：columns 与 headers 都当列名，且行按列名对齐", () => {
  const canonical = { columns: ["项目", "值"], rows: [["功能单位", "1 吨"]] };
  const legacy = { headers: ["项目", "值"], rows: [["功能单位", "1 吨"]] };

  assert.deepEqual(tableColumns(canonical), ["项目", "值"]);
  assert.deepEqual(tableColumns(legacy), ["项目", "值"]);
  assert.deepEqual(tableRows(legacy), [["功能单位", "1 吨"]]);

  // 对象行按列名取值：列名换了拼法也要对得上
  const objectRows = { headers: ["项目", "值"], rows: [{ 项目: "功能单位", 值: "1 吨" }] };
  assert.deepEqual(tableRows(objectRows), [["功能单位", "1 吨"]]);
});

test("列名全缺时不报错，行仍原样读出来", () => {
  assert.deepEqual(tableColumns({ rows: [["原材料", "392.04"]] }), []);
  assert.deepEqual(tableRows({ rows: [["原材料", "392.04"]] }), [["原材料", "392.04"]]);
});

test("列表项：items 缺省时退回块的 text", () => {
  assert.deepEqual(listItems({ data: { items: ["一", "二"] } }), ["一", "二"]);
  assert.deepEqual(listItems({ data: {}, text: "只有一句话" }), ["只有一句话"]);
});

test("在线视图从共用模块读 IR，不再自己维护一套键名", async () => {
  const source = await readFile(
    new URL("../src/components/ReportIrView.jsx", import.meta.url),
    "utf8"
  );

  assert.match(source, /from "\.\.\/lib\/reportBlocks"/);
  assert.doesNotMatch(source, /const (COLUMN_KEYS|ITEM_KEYS)\s*=/, "别名表不应在组件里另起一份");
});
