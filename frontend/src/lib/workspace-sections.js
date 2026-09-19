/**
 * 工作台的六个常驻分区。
 *
 * 分区页不再由路由渲染，而是由 WorkspaceLayout 自己渲染并常驻（切按钮不卸载，
 * 筛选、表单、滚动位置都留着）。路由只剩三件事：URL、守卫、详情页。
 *
 * 因此「当前该显示哪个分区」必须只有一个判据 —— 就是这里。布局层和详情层都要
 * 走同一个函数，各判各的会出现「/reports/:runId 的详情已经盖上来，下面的报告
 * 列表分区同时也被点亮」这种叠层。
 */

export const SECTIONS = [
  { key: "chat", path: "/", label: "对话" },
  { key: "library", path: "/datasets", label: "资料库" },
  { key: "tasks", path: "/tasks", label: "解析队列" },
  { key: "review", path: "/crawler/review", label: "资料审核" },
  { key: "reports", path: "/reports", label: "报告中心" },
  { key: "models", path: "/models", label: "模型配置" },
];

/** 默认落在对话分区：一进项目就是对话界面。 */
export const HOME_SECTION = "chat";

/** 审核员只放行对话与资料审核，与路由层的 AdminRoute 是同一套规则的兜底。 */
export const REVIEWER_SECTIONS = new Set(["chat", "review"]);

/**
 * 命中某个分区返回其 key，否则返回 null。
 *
 * 必须**精确**匹配：`/reports/:runId` 是详情页，不能被 `startsWith("/reports")`
 * 抢成报告列表分区。返回 null 一律按「详情层」处理。
 */
export function sectionForPath(pathname) {
  const match = SECTIONS.find((section) => section.path === pathname);
  return match ? match.key : null;
}
