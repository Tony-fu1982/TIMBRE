/** 任务队列抽屉 —— issue 04/08 UI：全部任务（新→旧）+ 固定退出提示 + 选中查看日志。 */
import { useEffect, useState } from "react";
import { api } from "../api";
import { Drawer, EmptyState, LogPanel, TaskRow } from "./index";
import { useStore } from "../store";
import type { Job } from "../types";

export function QueueDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { jobs, toast, errText, fetchLogs } = useStore();
  const [selected, setSelected] = useState<number | null>(null);
  const currentId = jobs.find((j) => j.id === selected)?.id ?? null;
  useEffect(() => { if (open && currentId != null) void fetchLogs(currentId); }, [open, currentId, fetchLogs]);

  async function retry(j: Job) {
    try {
      const n = await api.retry(j.id);
      setSelected(n.id);
      toast("已重新加入队列", "success");
    } catch (e) { toast(errText(e), "error"); }
  }

  return (
    <Drawer open={open} title="任务队列" onClose={onClose}>
      {/* ponytail: 48px = Drawer 头部高度（32px 关闭按钮 + 16px 下边距），头部若改版需同步；
          升级路径：lead 把 Drawer 主体改为 flex 布局后删掉这个 calc。 */}
      <div style={{ display: "flex", flexDirection: "column", height: "calc(100% - 48px)" }}>
        <p className="caption" style={{ flex: "none", marginBottom: 12 }}>应用退出后，等待任务将取消</p>
        {jobs.length === 0 ? <EmptyState text="暂无任务" /> : (
          <div style={{ flex: 1, minHeight: 0, overflowY: "auto", display: "flex", flexDirection: "column", gap: 4 }}>
            {jobs.map((j) => (
              <TaskRow key={j.id} job={j} selected={j.id === currentId}
                onSelect={(job) => setSelected(job.id)}
                onRetry={(job) => void retry(job)} />
            ))}
          </div>
        )}
        {currentId != null && (
          <div style={{ flex: "none", marginTop: 12 }}>
            <LogPanel jobId={currentId} />
          </div>
        )}
      </div>
    </Drawer>
  );
}
