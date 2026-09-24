import { NavLink, useNavigate } from "react-router-dom";
import {
  LoaderCircle,
  LogOut,
  MessageSquareText,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  Trash2,
  X,
} from "lucide-react";

import { useAuth } from "../state/AuthContext";
import { useChatSession } from "../state/ChatSessionContext";
import { IconButton } from "./ui";

/**
 * 工作台左栏：功能区 + 最近对话，合成一列。
 *
 * 「最近对话」是从对话页搬过来的 —— 它原先挂在 PlaygroundPage 内部，一离开对话页
 * 就没了。放进这里，它和分区按钮同处一列，切面板时不会跟着消失。
 *
 * 左栏本身不做取数：会话状态由 ChatSessionProvider 提供，而它挂在路由之上，
 * 所以这里直接消费同一份，不会多拉一次。
 */
export function WorkspaceRail({ navigation, admin, collapsed, mobileOpen, onCollapse, onMobileClose }) {
  const navigate = useNavigate();
  const { logout } = useAuth();
  const {
    conversations,
    conversationId,
    historyLoading,
    historyError,
    pendingDeleteId,
    deletingId,
    setPendingDeleteId,
    clearHistoryError,
    openConversation,
    deleteConversation,
    isConversationStreaming,
  } = useChatSession();

  const isCompact = collapsed && !mobileOpen;
  const canChat = ["admin", "user", "reviewer"].includes(admin?.role);
  const homePath = "/";

  async function handleOpenConversation(id) {
    await openConversation(id);
    // 会话可能是在别的分区里点的：一并切回对话分区，否则点了没反应。
    navigate("/");
    onMobileClose();
  }

  return (
    <>
      {mobileOpen ? <button className="mobile-scrim" aria-label="关闭导航" onClick={onMobileClose} /> : null}
      <aside
        className={`workspace-rail ${isCompact ? "workspace-rail--collapsed" : ""} ${mobileOpen ? "workspace-rail--mobile-open" : ""}`}
      >
        <div className="workspace-rail__brand">
          <button className="brand-button" onClick={() => navigate(homePath)} aria-label="返回工作台首页">
            <span className={`brand-word${isCompact ? " brand-word--compact" : ""}`} aria-hidden="true">
              {isCompact ? <span className="brand-word__compact">AI</span> : (
                <>
                  <span className="brand-word__name">能碳会计</span>
                  <span className="brand-word__descriptor">AI 智能体</span>
                </>
              )}
            </span>
          </button>
          <div className="workspace-rail__brand-actions">
            <IconButton className="workspace-rail__collapse" label={collapsed ? "展开导航" : "收起导航"} onClick={onCollapse}>
              {isCompact ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}
            </IconButton>
            <IconButton className="workspace-rail__mobile-close" label="关闭导航" onClick={onMobileClose}><X size={18} /></IconButton>
          </div>
        </div>

        {canChat ? <button
          type="button"
          className="rail-new-chat"
          // 走 `?new=` 而不是直接调 startNewConversation()：那条路上还会中止正在跑的
          // 生成、清空草稿附件与输入框，只开一个临时桶的话这些都会留下。
          onClick={() => { navigate(`/?new=${Date.now()}`); onMobileClose(); }}
        >
          <Plus size={16} strokeWidth={2} />
          {isCompact ? null : <span>新建对话</span>}
        </button> : null}

        <nav className="rail-nav" aria-label="功能">
          {navigation.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              onClick={onMobileClose}
              className={({ isActive }) => `rail-nav__item${isActive ? " is-active" : ""}`}
              title={isCompact ? label : undefined}
            >
              <Icon size={17} strokeWidth={1.75} />
              {isCompact ? null : <span>{label}</span>}
            </NavLink>
          ))}
        </nav>

        {isCompact || !canChat ? null : (
          <section className="rail-conversations" aria-label="最近对话">
            <header className="rail-conversations__header"><span>最近对话</span></header>
            <div className="rail-conversations__list">
              {historyLoading ? <p className="rail-conversations__hint">正在读取…</p> : conversations.length ? conversations.map((item) => (
                <div className={`rail-conversations__item${item.conversation_id === conversationId ? " is-active" : ""}`} key={item.conversation_id}>
                  <button type="button" className="rail-conversations__open" onClick={() => { void handleOpenConversation(item.conversation_id); }}>
                    <MessageSquareText size={14} /><strong>{item.title}</strong>
                  </button>
                  {pendingDeleteId === item.conversation_id ? (
                    <div className="rail-conversations__confirm" role="group" aria-label="确认删除对话">
                      <button type="button" className="is-danger" onClick={() => { void deleteConversation(item.conversation_id); }} disabled={deletingId === item.conversation_id}>
                        {deletingId === item.conversation_id ? <LoaderCircle className="spin" size={12} /> : "删除"}
                      </button>
                      <button type="button" onClick={() => setPendingDeleteId(null)} disabled={Boolean(deletingId)}>取消</button>
                    </div>
                  ) : (
                    <button
                      type="button"
                      className="rail-conversations__delete"
                      aria-label={`删除对话：${item.title}`}
                      title={isConversationStreaming(item.conversation_id) ? "生成中，暂不能删除" : "删除整段对话"}
                      onClick={() => { clearHistoryError(); setPendingDeleteId(item.conversation_id); }}
                      disabled={isConversationStreaming(item.conversation_id)}
                    >
                      <Trash2 size={13} />
                    </button>
                  )}
                </div>
              )) : <p className="rail-conversations__hint">还没有历史对话</p>}
              {historyError ? <p className="rail-conversations__error" role="alert">{historyError}</p> : null}
            </div>
          </section>
        )}

        <div className="workspace-rail__footer">
          {isCompact ? null : (
            <div className="rail-profile" title="当前账号">
              <span className="rail-profile__avatar">{String(admin?.username || "A").slice(0, 1).toUpperCase()}</span>
              <span><strong>{admin?.username || "用户"}</strong><small>{admin?.role === "reviewer" ? "资料审核员" : admin?.role === "user" ? "普通账号" : "管理员"}</small></span>
            </div>
          )}
          <IconButton label="退出登录" onClick={logout}><LogOut size={17} /></IconButton>
        </div>
      </aside>
    </>
  );
}
