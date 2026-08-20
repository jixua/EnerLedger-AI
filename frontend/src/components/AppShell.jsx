import { useEffect, useMemo, useRef, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  Activity,
  Bot,
  Database,
  Menu,
  MessageSquareText,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  LogOut,
  Workflow,
  X,
} from "lucide-react";
import { useApp } from "../state/AppContext";
import { Button, IconButton } from "./ui";
import { useAuth } from "../state/AuthContext";

const navigation = [
  { to: "/", label: "对话", icon: MessageSquareText, end: true },
  { to: "/datasets", label: "数据集", icon: Database },
  { to: "/tasks", label: "解析队列", icon: Workflow },
  { to: "/models", label: "模型配置", icon: Bot },
  { to: "/system", label: "系统状态", icon: Activity },
];

function Sidebar({ collapsed, mobileOpen, onCollapse, onMobileClose }) {
  const navigate = useNavigate();
  const isCompact = collapsed && !mobileOpen;

  return (
    <>
      {mobileOpen ? <button className="mobile-scrim" aria-label="关闭导航" onClick={onMobileClose} /> : null}
      <aside className={`sidebar ${isCompact ? "sidebar--collapsed" : ""} ${mobileOpen ? "sidebar--mobile-open" : ""}`}>
        <div className="sidebar__brand">
          <button className="brand-button" onClick={() => navigate("/")} aria-label="返回能碳会计 AI 智能体对话">
            <img src="/assets/brand/linkrag-mark-v4-static.svg" alt="" className="brand-mark" />
            {!isCompact ? (
              <span className="brand-word" aria-hidden="true">
                <span className="brand-word__name">能碳会计</span>
                <span className="brand-word__descriptor">AI 智能体</span>
              </span>
            ) : null}
          </button>
          <IconButton className="sidebar__mobile-close" label="关闭导航" onClick={onMobileClose}><X size={18} /></IconButton>
        </div>

        <div className="sidebar__section-label">{isCompact ? "" : "功能"}</div>
        <nav className="sidebar__nav" aria-label="主导航">
          {navigation.map(({ to, label, icon: Icon, end }) => (
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
  if (/^\/datasets\/[^/]+\/documents\/[^/]+\/analysis\/?$/.test(pathname)) return "分析报告";
  if (/^\/datasets\/[^/]+\/documents\/[^/]+\/?$/.test(pathname)) return "文档详情";
  if (pathname.startsWith("/datasets/")) return "数据集详情";
  return navigation.find((item) => item.to !== "/" && pathname.startsWith(item.to))?.label || "对话";
}

export function AppShell() {
  const [collapsed, setCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();
  const viewportRef = useRef(null);
  const { apiReachable, isDemo, lastError } = useApp();
  const { admin, logout } = useAuth();
  const breadcrumb = useMemo(() => getBreadcrumb(location.pathname), [location.pathname]);

  useEffect(() => {
    document.documentElement.classList.remove("dark");
    localStorage.removeItem("energy-carbon-theme");
  }, []);

  useEffect(() => {
    viewportRef.current?.scrollTo({ top: 0, left: 0 });
  }, [location.pathname]);

  return (
    <div className="app-frame">
      <Sidebar collapsed={collapsed} mobileOpen={mobileOpen} onCollapse={() => setCollapsed((value) => !value)} onMobileClose={() => setMobileOpen(false)} />
      <section className="workspace-panel">
        <header className="topbar">
          <div className="topbar__path">
            <IconButton className="topbar__menu" label="打开导航" onClick={() => setMobileOpen(true)}><Menu size={19} /></IconButton>
            <strong>{breadcrumb}</strong>
          </div>
          <div className="topbar__actions">
            <span className="admin-identity" title="当前管理员"><strong>{admin?.username}</strong><small>管理员</small></span>
            <Button onClick={() => navigate(`/?new=${Date.now()}`)}><Plus size={16} />新建对话</Button>
            <IconButton label="退出登录" onClick={logout}><LogOut size={17} /></IconButton>
          </div>
        </header>
        {lastError && !isDemo ? (
          <div className="service-banner" role="status">
            {apiReachable ? `接口已连接，但业务数据加载失败：${lastError}` : `无法连接后端服务：${lastError}`}
          </div>
        ) : null}
        <main ref={viewportRef} className="page-viewport" id="main-content"><Outlet /></main>
      </section>
    </div>
  );
}
