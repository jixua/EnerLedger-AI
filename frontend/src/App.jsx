import { lazy, Suspense } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { AppProvider } from "./state/AppContext";
import { AuthProvider, useAuth } from "./state/AuthContext";

const DatasetsPage = lazy(() => import("./pages/DatasetsPage").then((module) => ({ default: module.DatasetsPage })));
const DatasetDetailPage = lazy(() => import("./pages/DatasetDetailPage").then((module) => ({ default: module.DatasetDetailPage })));
const DocumentDetailPage = lazy(() => import("./pages/DocumentDetailPage").then((module) => ({ default: module.DocumentDetailPage })));
const DocumentAnalysisPage = lazy(() => import("./pages/DocumentAnalysisPage").then((module) => ({ default: module.DocumentAnalysisPage })));
const TasksPage = lazy(() => import("./pages/TasksPage").then((module) => ({ default: module.TasksPage })));
const PlaygroundPage = lazy(() => import("./pages/PlaygroundPage").then((module) => ({ default: module.PlaygroundPage })));
const ModelsPage = lazy(() => import("./pages/ModelsPage").then((module) => ({ default: module.ModelsPage })));
const SystemPage = lazy(() => import("./pages/SystemPage").then((module) => ({ default: module.SystemPage })));
const CrawlerReviewPage = lazy(() => import("./pages/CrawlerReviewPage").then((module) => ({ default: module.CrawlerReviewPage })));
const LoginPage = lazy(() => import("./pages/LoginPage").then((module) => ({ default: module.LoginPage })));

function PageLoader() {
  return <div className="route-loader"><span className="skeleton" /><span className="skeleton" /><span className="skeleton" /></div>;
}

function ProtectedApp() {
  const { authenticated, checking } = useAuth();
  const location = useLocation();
  if (checking) return <PageLoader />;
  if (!authenticated) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return <AppProvider><AppShell /></AppProvider>;
}

export function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="login" element={<Suspense fallback={<PageLoader />}><LoginPage /></Suspense>} />
          <Route element={<ProtectedApp />}>
            <Route index element={<Suspense fallback={<PageLoader />}><PlaygroundPage /></Suspense>} />
            <Route path="datasets" element={<Suspense fallback={<PageLoader />}><DatasetsPage /></Suspense>} />
            <Route path="datasets/:datasetId" element={<Suspense fallback={<PageLoader />}><DatasetDetailPage /></Suspense>} />
            <Route path="datasets/:datasetId/documents/:documentId" element={<Suspense fallback={<PageLoader />}><DocumentDetailPage /></Suspense>} />
            <Route path="datasets/:datasetId/documents/:documentId/analysis" element={<Suspense fallback={<PageLoader />}><DocumentAnalysisPage /></Suspense>} />
            <Route path="tasks" element={<Suspense fallback={<PageLoader />}><TasksPage /></Suspense>} />
            <Route path="playground" element={<Navigate to="/" replace />} />
            <Route path="models" element={<Suspense fallback={<PageLoader />}><ModelsPage /></Suspense>} />
            <Route path="system" element={<Suspense fallback={<PageLoader />}><SystemPage /></Suspense>} />
            <Route path="crawler/review" element={<Suspense fallback={<PageLoader />}><CrawlerReviewPage /></Suspense>} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
