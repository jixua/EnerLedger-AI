import { useEffect, useMemo, useRef, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  Bot,
  Database,
  FileChartColumn,
  Menu,
  MessageSquareText,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  LogOut,
  ShieldCheck,
  Workflow,
  X,
} from "lucide-react";
import { useApp } from "../state/AppContext";
import { Button, IconButton } from "./ui";
import { useAuth } from "../state/AuthContext";

const navigation = [
  { to: "/", label: "对话", icon: MessageSquareText, end: true },
  { to: "/datasets", label: "碳知识库", icon: Database },
  { to: "/crawler/review", label: "资料审核", icon: ShieldCheck },
  { to: "/tasks", label: "解析队列", icon: Workflow },
  { to: "/reports", label: "报告中心", icon: FileChartColumn },
  { to: "/models", label: "模型配置", icon: Bot },
];

/**
 * 全局导航默认收起成 76px 图标栏。
 *
 * 展开态（256px）与「最近对话」列（218px）行高相同、宽度相近，两列并排会被读成
 * 两个同类列表；导航是背景信息，收窄后内容区也能拿回 180px。用户展开后的选择会
 * 被记住。移动端不受影响：≤1080px 时 `sidebar--collapsed` 本就是 320px 抽屉样式。
 */
const SIDEBAR_COLLAPSED_KEY = "enerledger-sidebar-collapsed";

function Sidebar({ admin, collapsed, mobileOpen, onCollapse, onMobileClose }) {
  const navigate = useNavigate();
  const isCompact = collapsed && !mobileOpen;
  const visibleNavigation = admin?.role === "reviewer"
    ? navigation.filter(({ to }) => to === "/" || to === "/crawler/review")
    : navigation;

  return (
    <>
      {mobileOpen ? <button className="mobile-scrim" aria-label="关闭导航" onClick={onMobileClose} /> : null}
      <aside className={`sidebar ${isCompact ? "sidebar--collapsed" : ""} ${mobileOpen ? "sidebar--mobile-open" : ""}`}>
        <div className="sidebar__atmosphere" aria-hidden="true" />
        <div className="sidebar__brand">
          <button className="brand-button" onClick={() => navigate("/")} aria-label="返回能碳会计 AI 智能体对话">
            <span className={`brand-word${isCompact ? " brand-word--compact" : ""}`} aria-hidden="true">
              {isCompact ? <span className="brand-word__compact">AI</span> : (
                <>
                <span className="brand-word__name">能碳会计</span>
                <span className="brand-word__descriptor">AI 智能体</span>
                </>
              )}
            </span>
          </button>
          <IconButton className="sidebar__mobile-close" label="关闭导航" onClick={onMobileClose}><X size={18} /></IconButton>
        </div>

        <div className="sidebar__section-label">{isCompact ? "" : "功能"}</div>
        <nav className="sidebar__nav" aria-label="主导航">
          {visibleNavigation.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              onClick={onMobileClose}
              className={({ isActive }) => `nav-item ${isActive ? "nav-item--active" : ""}`}
              title={isCompact ? label : undefined}
            >
              <Icon size={18} strokeWidth={1.75} />
              {!isCompact ? <span><strong>{label}</strong></span> : null}
              {!isCompact ? <i aria-hidden="true" /> : null}
            </NavLink>
          ))}
        </nav>

        <div className="sidebar__footer">
          {!isCompact ? (
            <div className="sidebar-profile" title="当前账号">
              <span className="sidebar-profile__avatar">{String(admin?.username || "A").slice(0, 1).toUpperCase()}</span>
              <span><strong>{admin?.username || "用户"}</strong><small>{admin?.role === "reviewer" ? "资料审核员" : "管理员"}</small></span>
            </div>
          ) : null}
          <button className="collapse-button" onClick={onCollapse} title={collapsed ? "展开侧栏" : "收起侧栏"}>
            {isCompact ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}
            {!isCompact ? <span>收起导航</span> : null}
          </button>
        </div>
      </aside>
    </>
  );
}

function getBreadcrumb(pathname) {
  if (/^\/reports\/[^/]+\/?$/.test(pathname)) return "报告详情";
  if (/^\/datasets\/[^/]+\/documents\/[^/]+\/?$/.test(pathname)) return "文档详情";
  if (pathname.startsWith("/datasets/")) return "知识库详情";
  return navigation.find((item) => item.to !== "/" && pathname.startsWith(item.to))?.label || "对话";
}

export function AppShell() {
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem(SIDEBAR_COLLAPSED_KEY) !== "0",
  );
  const [mobileOpen, setMobileOpen] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();
  const viewportRef = useRef(null);
  const { apiReachable, isDemo, lastError } = useApp();
  const { admin, logout } = useAuth();
  const breadcrumb = useMemo(() => getBreadcrumb(location.pathname), [location.pathname]);
  const isKnowledgeLibrary = location.pathname === "/datasets";

  useEffect(() => {
    document.documentElement.classList.remove("dark");
    localStorage.removeItem("energy-carbon-theme");
  }, []);

  useEffect(() => {
    viewportRef.current?.scrollTo({ top: 0, left: 0 });
  }, [location.pathname]);

  function handleCollapse() {
    const next = !collapsed;
    localStorage.setItem(SIDEBAR_COLLAPSED_KEY, next ? "1" : "0");
    setCollapsed(next);
  }

  return (
    <div className="app-frame">
      <Sidebar admin={admin} collapsed={collapsed} mobileOpen={mobileOpen} onCollapse={handleCollapse} onMobileClose={() => setMobileOpen(false)} />
      <section className="workspace-panel">
        <header className="topbar">
          <div className="topbar__path">
            <IconButton className="topbar__menu" label="打开导航" onClick={() => setMobileOpen(true)}><Menu size={19} /></IconButton>
            <strong>{breadcrumb}</strong>
          </div>
          <div className="topbar__actions">
            <Button onClick={() => navigate(isKnowledgeLibrary && admin?.role === "admin" ? `/datasets?create=${Date.now()}` : `/?new=${Date.now()}`)}>
              <Plus size={16} />{isKnowledgeLibrary ? "新建知识库" : "新建对话"}
            </Button>
            <IconButton label="退出登录" onClick={logout}><LogOut size={17} /></IconButton>
          </div>
        </header>
        {lastError && !isDemo ? (
          <div className="service-banner" role="status">
            {apiReachable ? `接口已连接，但业务数据加载失败：${lastError}` : `无法连接后端服务：${lastError}`}
          </div>
        ) : null}
        <main ref={viewportRef} className="page-viewport" id="main-content">
          <div className="route-transition" key={location.pathname}>
            <Outlet />
          </div>
        </main>
      </section>
    </div>
  );
}
