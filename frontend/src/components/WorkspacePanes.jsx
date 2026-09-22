import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { useLocation, useOutlet } from "react-router-dom";

import { HOME_SECTION, ROLE_SECTIONS, sectionForPath } from "../lib/workspace-sections";
import { useAuth } from "../state/AuthContext";
import { PageLoader } from "./ui";

const PANES = {
  chat: lazy(() => import("../pages/PlaygroundPage").then((module) => ({ default: module.PlaygroundPage }))),
  library: lazy(() => import("../pages/DatasetsPage").then((module) => ({ default: module.DatasetsPage }))),
  tasks: lazy(() => import("../pages/TasksPage").then((module) => ({ default: module.TasksPage }))),
  review: lazy(() => import("../pages/CrawlerReviewPage").then((module) => ({ default: module.CrawlerReviewPage }))),
  reports: lazy(() => import("../pages/ReportsPage").then((module) => ({ default: module.ReportsPage }))),
  models: lazy(() => import("../pages/ModelsPage").then((module) => ({ default: module.ModelsPage }))),
  users: lazy(() => import("../pages/UsersPage").then((module) => ({ default: module.UsersPage }))),
};

/**
 * 工作台的右侧主区域：六个分区面板 + 一个详情层。
 *
 * 面板**首次访问才挂载，之后一直留着**（而不是每次切换重挂），这样筛选条件、填了一半
 * 的表单、滚动位置都还在。隐藏用的是 `visibility: hidden` 而不是 `display: none`：
 * 前者保留布局盒子，滚动位置才留得住；后者会把盒子收掉，scrollTop 归零。
 *
 * 常驻的代价是所有访问过的面板同时在 DOM 里，所以必须逐个 `inert` 掉 —— 否则隐藏
 * 面板里的链接和按钮仍会被 Tab 键和读屏软件摸到。只加 `inert` 就够：它已把子树移出
 * 无障碍树并禁止聚焦，再叠一个 `aria-hidden` 反而会在「焦点还留在里面」时被浏览器
 * 忽略并告警。
 */
export function WorkspacePanes() {
  const location = useLocation();
  const outlet = useOutlet();
  const { admin } = useAuth();

  const matched = sectionForPath(location.pathname);
  // 与路由层的 AdminRoute 同一套规则的兜底：重定向落地前的那一帧不放出越权面板。
  const roleSections = ROLE_SECTIONS[admin?.role] || new Set();
  const roleHome = admin?.role === "reviewer" ? "review" : HOME_SECTION;
  const active = matched && !roleSections.has(matched)
    ? roleHome
    : matched;
  const detailActive = active === null;

  const [visited, setVisited] = useState(() => new Set([roleHome, ...(active ? [active] : [])]));
  const paneRefs = useRef({});
  const previousActive = useRef(active);

  useEffect(() => {
    if (!active) return;
    setVisited((current) => (current.has(active) ? current : new Set(current).add(active)));
  }, [active]);

  useEffect(() => {
    const previous = previousActive.current;
    previousActive.current = active;
    if (previous === active) return;
    // 面板一旦 inert，停在里面的焦点会被浏览器丢回 <body>。只有当焦点原本就在
    // 被隐藏的那个面板里时才接管，免得把用户正在别处操作的焦点抢过来。
    const previousNode = previous ? paneRefs.current[previous] : null;
    if (!previousNode?.contains(document.activeElement)) return;
    paneRefs.current[active]?.focus({ preventScroll: true });
  }, [active]);

  // 详情层不常驻（每个详情都换一次参数），所以每次进一个新的详情都要回到顶部；
  // 分区面板不能这么做，那正是它们要保住的滚动位置。
  useEffect(() => {
    if (!detailActive) return;
    const node = paneRefs.current.detail;
    if (node) node.scrollTop = 0;
  }, [location.pathname, detailActive]);

  const registerPane = (key) => (node) => {
    if (node) paneRefs.current[key] = node;
    else delete paneRefs.current[key];
  };

  return (
    <main className="workspace-main" id="main-content">
      {Object.entries(PANES).map(([key, Pane]) => (visited.has(key) ? (
        <section
          key={key}
          ref={registerPane(key)}
          className={`page-pane${key === active ? " is-active" : ""}`}
          inert={key === active ? undefined : true}
          tabIndex={-1}
        >
          <Suspense fallback={<PageLoader />}><Pane /></Suspense>
        </section>
      ) : null))}

      <section
        ref={registerPane("detail")}
        className={`page-pane${detailActive ? " is-active" : ""}`}
        inert={detailActive ? undefined : true}
        tabIndex={-1}
      >
        <div className="route-transition" key={location.pathname}>
          {detailActive ? outlet : null}
        </div>
      </section>
    </main>
  );
}
