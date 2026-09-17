/**
 * 报告任务的共享文案与派生判断。
 *
 * 状态文案此前在对话卡片、生成对话框和报告中心各存一份，已经出现漂移
 * （STALE_DOCUMENT 一处写"文档已更新"、一处写"文档版本已变化"）。这里作为
 * 唯一来源，三个界面都从这里取。
 */

export const REPORT_STATE_LABELS = {
  PENDING: "等待处理",
  PROCESSING: "正在生成",
  NEEDS_INPUT: "等待补充信息",
  SUCCEEDED: "生成完成",
  FAILED: "生成失败",
  CANCELLED: "已取消",
  STALE_DOCUMENT: "文档已更新",
};

export const REPORT_STATE_TONES = {
  SUCCEEDED: "is-done",
  FAILED: "is-failed",
  STALE_DOCUMENT: "is-failed",
  CANCELLED: "is-muted",
  NEEDS_INPUT: "is-waiting",
};

/** 任务仍在推进，界面需要轮询。 */
export const ACTIVE_REPORT_STATES = new Set(["PENDING", "PROCESSING"]);

/** 已进入终态但报告不可用，需要用户重试或重建。 */
export const FAILED_REPORT_STATES = new Set(["FAILED", "CANCELLED", "STALE_DOCUMENT"]);

/** 已有报告正文可读的状态。 */
export const READABLE_REPORT_STATES = new Set(["SUCCEEDED"]);

export function reportStateLabel(state) {
  return REPORT_STATE_LABELS[state] || state || "未知状态";
}

export function reportStateTone(state) {
  return REPORT_STATE_TONES[state] || "";
}

/**
 * 面向用户展示的报告类型名称。
 *
 * R1–R7 是模板注册表里的内部编号，只在接口字段和后端匹配逻辑里使用；界面上
 * 一律显示模板名称（如「产品碳足迹评价报告」）。
 */
export function reportTypeName(run) {
  return run?.report_type_name || run?.report_type || "报告";
}

export function isActiveReportRun(state) {
  return ACTIVE_REPORT_STATES.has(state);
}

export function reportRunPath(runId) {
  return `/reports/${encodeURIComponent(runId)}`;
}

export function reportSourceDocumentPath(run) {
  if (!run?.dataset_id || !run?.document_id) return null;
  return `/datasets/${run.dataset_id}/documents/${run.document_id}`;
}

/** 产物格式名：菜单里只写格式，按钮上写完整动作。 */
export function reportArtifactFormat(artifactType) {
  if (artifactType === "DOCX") return "Word";
  if (artifactType === "MARKDOWN") return "Markdown";
  return artifactType;
}

/** 产物下载按钮的文案：同一份报告在列表、详情页、对话卡片里措辞一致。 */
export function reportArtifactLabel(artifactType) {
  return `下载 ${reportArtifactFormat(artifactType)}`;
}

export function formatReportTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN");
}

/** 字段台账里代表"尚未落实"的状态，用于详情页的待确认清单。 */
export const UNRESOLVED_FIELD_STATUSES = new Set([
  "MISSING",
  "CONFLICT",
  "UNVERIFIED",
  "NOT_APPLICABLE",
]);

const FIELD_STATUS_LABELS = {
  FOUND: "文档已载明",
  CALCULATED: "由计算得出",
  USER_SUPPLIED: "用户补充",
  MISSING: "材料未提供",
  CONFLICT: "多来源冲突",
  UNVERIFIED: "未经验证",
  NOT_APPLICABLE: "不适用",
};

export function fieldStatusLabel(status) {
  return FIELD_STATUS_LABELS[status] || status || "未知";
}

export function unresolvedFields(reportIr) {
  return (reportIr?.field_ledger || []).filter((item) =>
    UNRESOLVED_FIELD_STATUSES.has(item?.status),
  );
}
