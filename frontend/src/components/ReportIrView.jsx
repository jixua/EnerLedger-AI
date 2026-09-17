/**
 * ReportIR 的在线渲染。
 *
 * 块类型与语义对齐后端 app/services/report_artifacts.py 的 _render_block：
 * paragraph/heading/signature_block/source_note 为文字，callout 为提示，
 * list 取 data.items，metric_cards 取 data.items，bar_chart/donut_chart 取
 * data.series，table 取 data.rows 与 data.columns。未识别的类型按 JSON 兜底，
 * 与 Word 导出保持一致，避免在线视图比下载产物少内容。
 */

const CHART_SERIES_SLOTS = 5;

function toNumber(value) {
  const parsed = typeof value === "number" ? value : Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatValue(value, unit) {
  if (value === null || value === undefined || value === "") return "—";
  const text = typeof value === "number" ? String(value) : String(value);
  return unit ? `${text} ${unit}` : text;
}

function listItems(block) {
  const items = block?.data?.items;
  if (Array.isArray(items) && items.length) {
    return items
      .map((item) => (item && typeof item === "object" ? item.text ?? item.label : item))
      .filter((text) => text !== null && text !== undefined && String(text).trim() !== "")
      .map((text) => String(text));
  }
  return block?.text ? [String(block.text)] : [];
}

function tableColumns(data, rows) {
  if (Array.isArray(data?.columns) && data.columns.length) return data.columns.map(String);
  const first = Array.isArray(rows) ? rows[0] : null;
  if (first && !Array.isArray(first) && typeof first === "object") return Object.keys(first);
  return [];
}

function tableRows(data) {
  const rows = Array.isArray(data?.rows) ? data.rows : [];
  const columns = tableColumns(data, rows);
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

function EvidenceChips({ ids }) {
  if (!ids?.length) return null;
  return (
    <span className="report-evidence" aria-label="引用证据">
      {ids.map((id) => <code key={id}>{id}</code>)}
    </span>
  );
}

function readableCell(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** 数据表兜底视图：图表旁始终提供等价表格，满足无障碍与打印需求。 */
function DataTable({ columns, rows, caption }) {
  if (!rows.length) return null;
  return (
    <details className="report-figure__data">
      <summary>查看数据表</summary>
      <div className="data-table-wrap">
        <table className="data-table report-table">
          {caption ? <caption>{caption}</caption> : null}
          {columns.length ? (
            <thead>
              <tr>{columns.map((name) => <th key={name}>{readableCell(name)}</th>)}</tr>
            </thead>
          ) : null}
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={rowIndex}>
                {row.map((cell, cellIndex) => <td key={cellIndex}>{readableCell(cell)}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

function ReportTable({ block }) {
  const data = block?.data || {};
  const columns = tableColumns(data, data.rows);
  const rows = tableRows(data);

  if (!rows.length) {
    return block.text ? <p className="report-block">{block.text}</p> : null;
  }

  return (
    <figure className="report-figure">
      <div className="data-table-wrap">
        <table className="data-table report-table">
          {columns.length ? (
            <thead>
              <tr>{columns.map((name, index) => <th key={`${name}-${index}`}>{readableCell(name)}</th>)}</tr>
            </thead>
          ) : null}
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={rowIndex}>
                {row.map((cell, cellIndex) => <td key={cellIndex}>{readableCell(cell)}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <EvidenceChips ids={block?.evidence_ids} />
    </figure>
  );
}

/** 指标值里出现中文说明它是一段话（产品名、功能单位），不是给眼球抓的数。 */
const CJK = /[\u4e00-\u9fff]/;

function MetricCards({ block }) {
  const items = Array.isArray(block?.data?.items) ? block.data.items : [];
  if (!items.length) return null;
  return (
    <div className="report-metrics">
      {items.map((item, index) => {
        const value = readableCell(item?.value);
        // 数值用指标字号，中文值退回正文偏大字号，避免一句话占满整行
        const isText = CJK.test(value);
        return (
          <div className="report-metric" key={`${item?.label ?? index}`}>
            <span className="report-metric__label">{readableCell(item?.label)}</span>
            <strong className={`report-metric__value${isText ? " is-text" : ""}`}>{value || "—"}</strong>
          </div>
        );
      })}
    </div>
  );
}

/**
 * 量值比较。全部同号时用单一色相按长度比较；一旦出现负值（减排、抵扣等），
 * 改用围绕零基线的发散条——长度条会把负值画成零宽，读者只能看到数字却看不到长度。
 */
function BarChart({ block }) {
  const series = Array.isArray(block?.data?.series) ? block.data.series : [];
  const unit = block?.data?.unit || "";
  const values = series.map((item) => toNumber(item?.value));
  const hasNegative = values.some((value) => value !== null && value < 0);
  const maxAbs = Math.max(0, ...values.map((value) => Math.abs(value ?? 0)));

  if (!series.length) return null;

  const dataTable = (
    <DataTable
      columns={["分项", unit ? `数值（${unit}）` : "数值"]}
      rows={series.map((item) => [item?.label, item?.value])}
    />
  );

  if (hasNegative) {
    return (
      <figure className="report-figure">
        <ul className="report-legend report-legend--polarity">
          <li><i style={{ background: "var(--report-positive)" }} aria-hidden="true" /><span className="report-legend__label">正值：排放 / 增加</span></li>
          <li><i style={{ background: "var(--report-negative)" }} aria-hidden="true" /><span className="report-legend__label">负值：减排 / 抵扣</span></li>
        </ul>
        <div className="report-diverging">
          {series.map((item, index) => {
            const value = values[index];
            const share = maxAbs > 0 && value !== null ? (Math.abs(value) / maxAbs) * 100 : 0;
            const positive = (value ?? 0) >= 0;
            return (
              <div className="report-diverge" key={`${item?.label ?? index}`}>
                <span className="report-diverge__label">{readableCell(item?.label)}</span>
                <span className="report-diverge__axis">
                  <span className="report-diverge__side report-diverge__side--negative">
                    {!positive ? (
                      <span className="report-diverge__fill is-negative" style={{ width: `${share.toFixed(2)}%` }} />
                    ) : null}
                  </span>
                  <span className="report-diverge__side report-diverge__side--positive">
                    {positive ? (
                      <span className="report-diverge__fill is-positive" style={{ width: `${share.toFixed(2)}%` }} />
                    ) : null}
                  </span>
                </span>
                <span className="report-diverge__value">{formatValue(value, unit)}</span>
              </div>
            );
          })}
        </div>
        {dataTable}
        <EvidenceChips ids={block?.evidence_ids} />
      </figure>
    );
  }

  return (
    <figure className="report-figure">
      <div className="report-bars">
        {series.map((item, index) => {
          const value = values[index];
          const ratio = value !== null && maxAbs > 0 ? Math.max(value, 0) / maxAbs : 0;
          return (
            <div className="report-bar" key={`${item?.label ?? index}`}>
              <span className="report-bar__label">{readableCell(item?.label)}</span>
              <span className="report-bar__track">
                <span
                  className="report-bar__fill"
                  style={{ width: `${(ratio * 100).toFixed(2)}%` }}
                />
              </span>
              <span className="report-bar__value">{formatValue(value, unit)}</span>
            </div>
          );
        })}
      </div>
      {dataTable}
      <EvidenceChips ids={block?.evidence_ids} />
    </figure>
  );
}

/**
 * 构成占比：用横向堆叠条而不是环形图——环形在类别多、名称长时难以读数。
 * 超过配色槽位数、或出现负值（此时"占总量多少"本身不成立）时退回表格，
 * 既不生成新色相，也不给出误导性的占比。
 */
function ShareChart({ block }) {
  const series = Array.isArray(block?.data?.series) ? block.data.series : [];
  const unit = block?.data?.unit || "";
  const values = series.map((item) => toNumber(item?.value));
  const total = values.reduce((sum, value) => sum + Math.max(value ?? 0, 0), 0);
  const hasNegative = values.some((value) => value !== null && value < 0);

  if (!series.length) return null;

  if (hasNegative) {
    return (
      <figure className="report-figure">
        <p className="report-figure__note">含负值分项（减排 / 抵扣），无法作为占比呈现，改为数据表。</p>
        <ReportTable block={block} />
      </figure>
    );
  }

  if (series.length > CHART_SERIES_SLOTS || total <= 0) {
    return <ReportTable block={block} />;
  }

  return (
    <figure className="report-figure">
      <div className="report-share" role="img" aria-label="构成占比">
        {series.map((item, index) => {
          const share = ((values[index] ?? 0) / total) * 100;
          return (
            <span
              key={`${item?.label ?? index}`}
              className="report-share__segment"
              style={{ width: `${share.toFixed(2)}%`, background: `var(--report-series-${index + 1})` }}
              title={`${readableCell(item?.label)} ${formatValue(item?.value, unit)}（${share.toFixed(1)}%）`}
            />
          );
        })}
      </div>
      <ul className="report-legend">
        {series.map((item, index) => (
          <li key={`${item?.label ?? index}`}>
            <i style={{ background: `var(--report-series-${index + 1})` }} aria-hidden="true" />
            <span className="report-legend__label">{readableCell(item?.label)}</span>
            <span className="report-legend__value">{formatValue(item?.value, unit)}</span>
            <span className="report-legend__share">
              {(((values[index] ?? 0) / total) * 100).toFixed(1)}%
            </span>
          </li>
        ))}
      </ul>
      <DataTable
        columns={["分项", unit ? `数值（${unit}）` : "数值", "占比"]}
        rows={series.map((item, index) => [
          item?.label,
          item?.value,
          `${(((values[index] ?? 0) / total) * 100).toFixed(1)}%`,
        ])}
      />
      <EvidenceChips ids={block?.evidence_ids} />
    </figure>
  );
}

function ReportBlock({ block }) {
  const type = block?.type;
  const text = typeof block?.text === "string" ? block.text.trim() : "";
  const evidence = block?.evidence_ids;

  switch (type) {
    case "paragraph":
      return <p className="report-block">{text}<EvidenceChips ids={evidence} /></p>;
    case "heading":
      return <h3 className="report-block report-block--heading">{text}<EvidenceChips ids={evidence} /></h3>;
    case "signature_block":
      return <p className="report-block report-block--signature">{text}<EvidenceChips ids={evidence} /></p>;
    case "source_note":
      return <p className="report-block report-block--source-note">{text}<EvidenceChips ids={evidence} /></p>;
    case "callout":
      return (
        <aside className="report-callout">
          <strong>提示</strong>
          <span>{text}</span>
          <EvidenceChips ids={evidence} />
        </aside>
      );
    case "list": {
      const items = listItems(block);
      return (
        <ul className="report-block report-block--list">
          {items.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}
          <EvidenceChips ids={evidence} />
        </ul>
      );
    }
    case "metric_cards":
      return <MetricCards block={block} />;
    case "bar_chart":
      return <BarChart block={block} />;
    case "donut_chart":
      return <ShareChart block={block} />;
    case "table":
      return <ReportTable block={block} />;
    case "page_break":
      return null;
    default: {
      const payload = block?.data ?? text;
      return (
        <pre className="report-block report-block--raw">
          {JSON.stringify(payload, null, 2)}
        </pre>
      );
    }
  }
}

export function ReportIrView({ reportIr }) {
  const sections = Array.isArray(reportIr?.sections) ? reportIr.sections : [];
  if (!sections.length) {
    return <p className="report-empty">报告正文为空。</p>;
  }

  return (
    <article className="report-document">
      {sections.map((section) => (
        <section className="report-section" key={section.section_id}>
          <h2>{section.title || section.section_id}</h2>
          {(section.blocks || []).map((block, index) => (
            <ReportBlock block={block} key={`${section.section_id}-${index}`} />
          ))}
        </section>
      ))}
    </article>
  );
}

export { ReportBlock, ReportTable };

export default ReportIrView;
