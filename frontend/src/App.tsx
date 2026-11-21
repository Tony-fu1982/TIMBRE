import { useCallback, useEffect, useState } from "react";
import { StoreProvider, useStore } from "./store";
import { Toasts, TopBar } from "./components";
import type { TabId } from "./components";
import { QueueDrawer } from "./components/QueueDrawer";
import { UploadPage } from "./pages/Upload";
import { DownloadPage } from "./pages/Download";
import { DnaPage } from "./pages/Dna";
import { ConvertPage } from "./pages/Convert";
import { SettingsPage } from "./pages/Settings";

function Shell() {
  const [tab, setTab] = useState<TabId>("upload");
  const { queueOpen, setQueueOpen, aiAuthFail, clearAiAuthFail, toast } = useStore();
  const closeQueue = useCallback(() => setQueueOpen(false), [setQueueOpen]);
  useEffect(() => {
    if (aiAuthFail == null) return;
    setTab("settings");  // SPEC §6：AI_AUTH_FAILED → 跳转 AI 设置
    toast("AI 密钥无效或无权限，请到「设置」检查", "error");
    clearAiAuthFail();
  }, [aiAuthFail, clearAiAuthFail, toast]);
  return (
    <div className="app">
      <TopBar tab={tab} onTab={setTab} />
      <main className="content">
        <div className="content-inner">
          {tab === "upload" && <UploadPage />}
          {tab === "download" && <DownloadPage />}
          {tab === "dna" && <DnaPage />}
          {tab === "convert" && <ConvertPage />}
          {tab === "settings" && <SettingsPage />}
        </div>
      </main>
      <QueueDrawer open={queueOpen} onClose={closeQueue} />
      <Toasts />
    </div>
  );
}

export default function App() {
  return <StoreProvider><Shell /></StoreProvider>;
}
