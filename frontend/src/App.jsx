import { lazy, Suspense } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { AppProvider } from "./state/AppContext";

const DatasetsPage = lazy(() => import("./pages/DatasetsPage").then((module) => ({ default: module.DatasetsPage })));
const DatasetDetailPage = lazy(() => import("./pages/DatasetDetailPage").then((module) => ({ default: module.DatasetDetailPage })));
const DocumentDetailPage = lazy(() => import("./pages/DocumentDetailPage").then((module) => ({ default: module.DocumentDetailPage })));
const TasksPage = lazy(() => import("./pages/TasksPage").then((module) => ({ default: module.TasksPage })));
const PlaygroundPage = lazy(() => import("./pages/PlaygroundPage").then((module) => ({ default: module.PlaygroundPage })));
const ModelsPage = lazy(() => import("./pages/ModelsPage").then((module) => ({ default: module.ModelsPage })));
const SystemPage = lazy(() => import("./pages/SystemPage").then((module) => ({ default: module.SystemPage })));

function PageLoader() {
  return <div className="route-loader"><span className="skeleton" /><span className="skeleton" /><span className="skeleton" /></div>;
}

export function App() {
  return (
    <BrowserRouter>
      <AppProvider>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<Suspense fallback={<PageLoader />}><PlaygroundPage /></Suspense>} />
            <Route path="datasets" element={<Suspense fallback={<PageLoader />}><DatasetsPage /></Suspense>} />
            <Route path="datasets/:datasetId" element={<Suspense fallback={<PageLoader />}><DatasetDetailPage /></Suspense>} />
            <Route path="datasets/:datasetId/documents/:documentId" element={<Suspense fallback={<PageLoader />}><DocumentDetailPage /></Suspense>} />
            <Route path="tasks" element={<Suspense fallback={<PageLoader />}><TasksPage /></Suspense>} />
            <Route path="playground" element={<Navigate to="/" replace />} />
            <Route path="models" element={<Suspense fallback={<PageLoader />}><ModelsPage /></Suspense>} />
            <Route path="system" element={<Suspense fallback={<PageLoader />}><SystemPage /></Suspense>} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </AppProvider>
    </BrowserRouter>
  );
}
