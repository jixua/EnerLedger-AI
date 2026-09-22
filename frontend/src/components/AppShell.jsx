import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { Bot, Database, FileChartColumn, Menu, ShieldCheck, UserRoundCog, Workflow } from "lucide-react";

import { SECTIONS, sectionForPath } from "../lib/workspace-sections";
import { useApp } from "../state/AppContext";
import { useAuth } from "../state/AuthContext";
import { IconButton } from "./ui";
import { WorkspacePanes } from "./WorkspacePanes";
import { WorkspaceRail } from "./WorkspaceRail";

/**
 * 左栏的功能区。对话不在这里 —— 它是左栏顶部那个「新建对话」动作按钮，
 * 分区里的对话内容则由 WorkspacePanes 常驻渲染。
 */
const navigation = [
  { to: "/datasets", label: "资料库", icon: Database, roles: ["admin", "user"] },
  { to: "/tasks", label: "解析队列", icon: Workflow, roles: ["admin", "user"] },
  { to: "/crawler/review", label: "资料审核", icon: ShieldCheck, roles: ["admin", "reviewer"] },
  { to: "/reports", label: "报告中心", icon: FileChartColumn, roles: ["admin", "user"] },
  { to: "/models", label: "模型配置", icon: Bot, roles: ["admin", "user"] },
  { to: "/users", label: "用户管理", icon: UserRoundCog, roles: ["admin"] },
];

/** 左栏收起的记忆位。默认展开：收起态只剩图标，「最近对话」整段没了，
    而它现在是左栏的一半内容 —— 默认收起来等于默认把会话列表藏掉。 */
const SIDEBAR_COLLAPSED_KEY = "enerledger-sidebar-collapsed";

export function AppShell() {
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "1",
  );
  const [mobileOpen, setMobileOpen] = useState(false);
  const location = useLocation();
  const { apiReachable, isDemo, lastError } = useApp();
  const { admin } = useAuth();

  const visibleNavigation = navigation.filter(({ roles }) => roles.includes(admin?.role));

  const sectionKey = sectionForPath(location.pathname);
  const mobileTitle = SECTIONS.find((section) => section.key === sectionKey)?.label || "详情";

  useEffect(() => {
    document.documentElement.classList.remove("dark");
    localStorage.removeItem("energy-carbon-theme");
  }, []);

  function handleCollapse() {
    const next = !collapsed;
    localStorage.setItem(SIDEBAR_COLLAPSED_KEY, next ? "1" : "0");
    setCollapsed(next);
  }

  return (
    <div className="app-frame">
      <WorkspaceRail
        navigation={visibleNavigation}
        admin={admin}
        collapsed={collapsed}
        mobileOpen={mobileOpen}
        onCollapse={handleCollapse}
        onMobileClose={() => setMobileOpen(false)}
      />
      <section className="workspace-panel">
        {/* 窄屏才出现：左栏此时是抽屉，需要一个够得着的入口。宽屏下这条细条整个隐藏。 */}
        <header className="workspace-mobilebar">
          <IconButton className="workspace-mobilebar__menu" label="打开导航" onClick={() => setMobileOpen(true)}><Menu size={19} /></IconButton>
          <strong>{mobileTitle}</strong>
        </header>
        {lastError && !isDemo ? (
          <div className="service-banner" role="status">
            {apiReachable ? `接口已连接，但业务数据加载失败：${lastError}` : `无法连接后端服务：${lastError}`}
          </div>
        ) : null}
        <WorkspacePanes />
      </section>
    </div>
  );
}
