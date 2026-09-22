import { lazy, Suspense } from "react";
import { BrowserRouter, Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { PageLoader } from "./components/ui";
import { AppProvider } from "./state/AppContext";
import { AuthProvider, useAuth } from "./state/AuthContext";
import { ChatSessionProvider } from "./state/ChatSessionContext";

const DatasetDetailPage = lazy(() => import("./pages/DatasetDetailPage").then((module) => ({ default: module.DatasetDetailPage })));
const DocumentDetailPage = lazy(() => import("./pages/DocumentDetailPage").then((module) => ({ default: module.DocumentDetailPage })));
const ReportDetailPage = lazy(() => import("./pages/ReportDetailPage").then((module) => ({ default: module.ReportDetailPage })));
const SystemPage = lazy(() => import("./pages/SystemPage").then((module) => ({ default: module.SystemPage })));
const LoginPage = lazy(() => import("./pages/LoginPage").then((module) => ({ default: module.LoginPage })));

function ProtectedApp() {
  const { admin, authenticated, checking } = useAuth();
  const location = useLocation();
  if (checking) return <PageLoader />;
  if (!authenticated) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  if (admin?.role === "reviewer" && !["/", "/crawler/review"].includes(location.pathname)) {
    return <Navigate to="/" replace />;
  }
  if (admin?.role === "user" && ["/crawler/review", "/users"].includes(location.pathname)) {
    return <Navigate to="/" replace />;
  }
  // 会话与工作台挂在路由之上：切面板只换视图，生成中的流不受影响。
  return (
    <AppProvider>
      <ChatSessionProvider>
        <Suspense fallback={<PageLoader />}>
          <Outlet />
        </Suspense>
      </ChatSessionProvider>
    </AppProvider>
  );
}

function AdminRoute({ children }) {
  const { admin } = useAuth();
  return admin?.role === "admin" ? children : <Navigate to="/" replace />;
}

function BusinessRoute({ children }) {
  const { admin } = useAuth();
  return ["admin", "user"].includes(admin?.role) ? children : <Navigate to="/crawler/review" replace />;
}

function ReviewRoute({ children }) {
  const { admin } = useAuth();
  return ["admin", "reviewer"].includes(admin?.role) ? children : <Navigate to="/" replace />;
}

/**
 * 六个分区（对话 / 资料库 / 解析队列 / 资料审核 / 报告中心 / 模型配置）由
 * AppShell 自己渲染并常驻，下面这些分区路由的 element 只登记 URL 与守卫、渲染 null。
 *
 * 分区路由之所以不渲染页面本身：页面若挂在 <Outlet/> 下，每切一次分区就换掉子元素
 * 类型，React 顺势把整棵子树卸载掉 —— 用户填了一半的筛选、表单、滚动位置全没了。
 * AppShell 处在恒定深度，切换时不会被重挂。
 */
export function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="login" element={<Suspense fallback={<PageLoader />}><LoginPage /></Suspense>} />
          <Route element={<ProtectedApp />}>
            <Route element={<AppShell />}>
              <Route index element={null} />
              <Route path="datasets" element={<BusinessRoute>{null}</BusinessRoute>} />
              <Route path="tasks" element={<BusinessRoute>{null}</BusinessRoute>} />
              <Route path="crawler/review" element={<ReviewRoute>{null}</ReviewRoute>} />
              <Route path="reports" element={<BusinessRoute>{null}</BusinessRoute>} />
              <Route path="models" element={<BusinessRoute>{null}</BusinessRoute>} />
              <Route path="users" element={<AdminRoute>{null}</AdminRoute>} />
              <Route path="reports/:runId" element={<BusinessRoute><Suspense fallback={<PageLoader />}><ReportDetailPage /></Suspense></BusinessRoute>} />
              <Route path="analysis-reports" element={<Navigate to="/reports" replace />} />
              <Route path="playground" element={<Navigate to="/" replace />} />
              <Route path="datasets/:datasetId" element={<BusinessRoute><Suspense fallback={<PageLoader />}><DatasetDetailPage /></Suspense></BusinessRoute>} />
              <Route path="datasets/:datasetId/documents/:documentId" element={<BusinessRoute><Suspense fallback={<PageLoader />}><DocumentDetailPage /></Suspense></BusinessRoute>} />
              <Route path="system" element={<BusinessRoute><Suspense fallback={<PageLoader />}><SystemPage /></Suspense></BusinessRoute>} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Route>
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
