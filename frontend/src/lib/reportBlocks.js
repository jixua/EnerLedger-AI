/**
 * ReportIR 数据块的读法：与后端 app/services/report_blocks.py 一一对应。
 *
 * 四种渲染器（在线视图、Markdown、HTML、DOCX）读同一份 IR，不能各自认一套键名。
 * IR 的 data 由模型自由填写，同一种东西在真实报告里出现过多种拼法——表格列名有
 * columns / headers，指标卡有 items / cards，图表有 series，也有 categories（或
 * labels）与 values 平行给出。只认其中一种，另一种就会让整块内容在页面上凭空消失，
 * 而且不报错。
 *
 * 这里只做取值，不做排版；schema 那一侧负责要求「能通过校验的一定读得出来」。
 */

/** 别名表：与后端 report_blocks.py 的 _COLUMN_KEYS / _ITEM_KEYS / _LABEL_KEYS 一致。 */
export const COLUMN_KEYS = ["columns", "headers"];
export const ITEM_KEYS = ["items", "cards", "metrics"];
export const LABEL_KEYS = ["categories", "labels"];

export function toNumber(value) {
  const parsed = typeof value === "number" ? value : Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function formatValue(value, unit) {
  if (value === null || value === undefined || value === "") return "—";
  const text = typeof value === "number" ? String(value) : String(value);
  return unit ? `${text} ${unit}` : text;
}

export function readableCell(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** 分项列表：对象项原样保留，标量项补成 {label, value}。 */
function namedItems(raw) {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((item) => item !== null && item !== undefined)
    .map((item) => (item && typeof item === "object" ? item : { label: item, value: null }));
}

function firstNamed(data, keys) {
  for (const key of keys) {
    const items = namedItems(data?.[key]);
    if (items.length) return items;
  }
  return [];
}

/** 平行数组形态：values 配 categories / labels 取名。 */
function parallelItems(data) {
  const values = data?.values;
  if (!Array.isArray(values) || !values.length) return [];
  const labels = LABEL_KEYS.map((key) => data?.[key]).find((value) => Array.isArray(value)) ?? [];
  return values.map((value, index) => ({
    label: index < labels.length ? labels[index] : null,
    value,
  }));
}

export function metricItems(block) {
  return firstNamed(block?.data, ITEM_KEYS);
}

export function seriesItems(block) {
  const series = namedItems(block?.data?.series);
  return series.length ? series : parallelItems(block?.data);
}

export function listItems(block) {
  const items = firstNamed(block?.data, ITEM_KEYS);
  if (items.length) {
    return items
      .map((item) => item.text ?? item.label)
      .filter((text) => text !== null && text !== undefined && String(text).trim() !== "")
      .map((text) => String(text));
  }
  return block?.text ? [String(block.text)] : [];
}

export function tableColumns(data) {
  for (const key of COLUMN_KEYS) {
    if (Array.isArray(data?.[key]) && data[key].length) return data[key].map(String);
  }
  const first = Array.isArray(data?.rows) ? data.rows[0] : null;
  if (first && !Array.isArray(first) && typeof first === "object") return Object.keys(first);
  return [];
}

export function tableRows(data) {
  const rows = Array.isArray(data?.rows) ? data.rows : [];
  const columns = tableColumns(data);
  return rows.map((row) => {
    if (Array.isArray(row)) {
      return columns.length
        ? columns.map((_, index) => (index < row.length ? row[index] : ""))
        : row;
    }
    if (row && typeof row === "object") {
      return columns.length ? columns.map((name) => row[name]) : Object.values(row);
    }
    return [row];
  });
}
