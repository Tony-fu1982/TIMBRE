/** 音乐 DNA 页 —— issue 12/13/14：参考音频分析 + 主题/DNA 生成（frontend-b）。 */
import { useState } from "react";
import { Download } from "lucide-react";
import { api } from "../api";
import { Badge, Button, CopyButton, EmptyState, Field, FileDrop, jobName, LogPanel, Panel, TaskRow } from "../components";
import { useStore } from "../store";
import { KIND_TEXT } from "../types";
import type { Job } from "../types";

const MODES: [string, string][] = [["full", "完整"], ["lyrics", "仅歌词"], ["style", "仅风格"]];
const MAX_BYTES = 50 * 1024 * 1024;
const modeText = (m: string) => MODES.find(([v]) => v === m)?.[1] ?? m;

/** 导出 UTF-8 TXT（带 BOM，SPEC §5）。 */
function exportTxt(name: string, text: string) {
  const url = URL.createObjectURL(new Blob(["\uFEFF" + text], { type: "text/plain;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000); // ponytail: 延迟回收，防个别浏览器尚未开始下载
}

/** 结果分段：标题 + 复制 + 原文（SPEC §4.8，按实际返回字段渲染）。 */
function ResultBlock({ title, text }: { title: string; text: string }) {
  return (
    <div className="result-card">
      <div className="spread" style={{ marginBottom: "var(--sp-2)" }}>
        <strong>{title}</strong>
        <CopyButton text={text} />
      </div>
      <pre>{text}</pre>
    </div>
  );
}

export function DnaPage() {
  const { jobs, ai, toast, errText, refresh } = useStore();
  const [file, setFile] = useState<File | null>(null);
  const [fileErr, setFileErr] = useState("");
  const [anaMode, setAnaMode] = useState("full");
  const [note, setNote] = useState("");
  const [anaErr, setAnaErr] = useState("");
  const [theme, setTheme] = useState("");
  const [dnaRef, setDnaRef] = useState(0);
  const [genMode, setGenMode] = useState("full");
  const [genErr, setGenErr] = useState("");
  const [busy, setBusy] = useState(false); // ponytail: 两表单共用一个提交锁，防重复提交
  const [selected, setSelected] = useState<number | null>(null);
  const [renaming, setRenaming] = useState(false);

  const taskJobs = jobs.filter((j) => j.kind === "dna" || j.kind === "generate");
  const refJobs = jobs.filter((j) => j.kind === "dna" && j.status === "success" && j.result?.dnaProfile);
  const job = taskJobs.find((j) => j.id === selected) ?? null;
  const jobMode = job ? String(job.options.mode ?? "full") : "full";

  const pickFile = (f: File | null) => {
    setFile(f);
    setFileErr(f && f.size > MAX_BYTES ? "文件超过 50MB 上限，请更换文件" : "");
  };

  const submitDna = async () => {
    setAnaErr("");
    if (!file) { setFileErr("请选择参考音频"); return; }
    if (file.size > MAX_BYTES) { setFileErr("文件超过 50MB 上限，请更换文件"); return; }
    setBusy(true);
    try {
      const j = await api.submitDna(file, anaMode, note);
      setSelected(j.id);
      setNote("");
      toast("已加入分析任务", "success");
    } catch (e) { setAnaErr(errText(e)); } // NO_AI_KEY 等中文指引来自后端（DNA 分析固定走 Qwen）
    finally { setBusy(false); }
  };

  const submitGen = async () => {
    setGenErr("");
    const t = theme.trim();
    const refValid = dnaRef !== 0 && refJobs.some((j) => j.id === dnaRef);
    if (!t && !refValid) { setGenErr("请填写主题或选择有效的 DNA 结果"); return; }
    setBusy(true);
    try {
      const j = await api.submitGenerate({
        mode: genMode,
        ...(t ? { theme: t } : {}),
        ...(refValid ? { dna_result_id: dnaRef } : {}),
      });
      setSelected(j.id);
      toast("已加入生成任务", "success");
    } catch (e) { setGenErr(errText(e)); }
    finally { setBusy(false); }
  };

  const saveName = async (v: string) => {
    if (!job || v === jobName(job)) return;
    try {
      await api.setLabel(job.id, v);
      await refresh();
      toast("已改名", "success");
    } catch (e) { toast(errText(e), "error"); }
  };

  const retry = async (j: Job) => {
    try {
      const nj = await api.retry(j.id);
      setSelected(nj.id);
      toast("已重新入队", "success");
    } catch (e) { toast(errText(e), "error"); } // BAD_AUDIO 等中文提示来自后端
  };

  // 结果分段：只渲染实际返回的字段（full 全有；lyrics 仅歌词；style 档案+提示词）
  const result = job && job.status === "success" ? job.result : null;
  const blocks: { title: string; text: string }[] = [];
  if (result) {
    if (result.dnaProfile != null) {
      blocks.push({ title: "DNA 档案", text: JSON.stringify(result.dnaProfile, null, 2) });
    }
    for (const [field, label] of [["lyrics", "歌词"], ["sunoPrompt", "Suno 提示词"]] as const) {
      const v = result[field] as { zh?: unknown; en?: unknown } | undefined;
      if (typeof v?.zh === "string" && v.zh) blocks.push({ title: `${label} · 中文`, text: v.zh });
      if (typeof v?.en === "string" && v.en) blocks.push({ title: `${label} · English`, text: v.en });
    }
  }

  const exportAll = () => {
    if (!job || blocks.length === 0) return;
    const head = `TIMBRE · ${KIND_TEXT[job.kind]} · 任务 #${job.id} · 模式：${modeText(jobMode)}`;
    const text = [head, ...blocks.map((b) => `【${b.title}】\n${b.text}`)].join("\n\n");
    exportTxt(`grow-music_${job.id}_${jobMode}.txt`, text);
  };

  const modeSelect = (label: string, value: string, onChange: (v: string) => void) => (
    <Field label={label}>
      <select className="select" aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}>
        {MODES.map(([v, t]) => <option key={v} value={v}>{t}</option>)}
      </select>
    </Field>
  );

  return (
    <>
      <h1 className="page-title" style={{ marginBottom: "var(--sp-4)" }}>音乐 DNA</h1>
      <div className="two-col">
        <div>
          <Panel title="分析参考音频">
            <div className="stack">
              <Field label="参考音频" error={fileErr} hint={!fileErr ? "仅分析自有或已授权的音频" : undefined}>
                <FileDrop accept="audio/*" file={file} onFile={pickFile} hint="≤50MB" />
              </Field>
              {modeSelect("分析模式", anaMode, setAnaMode)}
              <Field label="自定义名称（可选）">
                <input className="input" aria-label="自定义名称（可选）" maxLength={60}
                  placeholder="给这次分析起个名字，如：荒 · 莫小檀"
                  value={note} onChange={(e) => setNote(e.target.value)} />
              </Field>
              {!ai.key_set && <span className="field-hint">尚未配置 AI 密钥：请先到「设置」页填写（DNA 音频分析使用 Qwen）。</span>}
              {anaErr && <span className="field-error" role="alert">{anaErr}</span>}
              <div><Button onClick={submitDna} disabled={busy}>开始分析</Button></div>
            </div>
          </Panel>
          <Panel title="生成">
            <div className="stack">
              <Field label="主题">
                <textarea className="textarea" aria-label="主题" placeholder="描述想要的歌曲主题、情绪或场景…"
                  value={theme} onChange={(e) => setTheme(e.target.value)} />
              </Field>
              <Field label="DNA 参考（可选）"
                hint={refJobs.length === 0 ? "暂无可用 DNA：先完成一次参考音频分析" : "同时填写时，主题为创作要求，DNA 提供风格参考"}>
                <select className="select" aria-label="DNA 参考" value={dnaRef}
                  onChange={(e) => setDnaRef(Number(e.target.value))}>
                  <option value={0}>不使用 DNA 参考</option>
                  {refJobs.map((j) => (
                    <option key={j.id} value={j.id}>DNA #{j.id} · {jobName(j)}</option>
                  ))}
                </select>
              </Field>
              {modeSelect("生成模式", genMode, setGenMode)}
              {genErr && <span className="field-error" role="alert">{genErr}</span>}
              <div><Button onClick={submitGen} disabled={busy}>开始生成</Button></div>
            </div>
          </Panel>
          <Panel title="任务">
            {taskJobs.length === 0
              ? <EmptyState text="暂无分析与生成任务" />
              : taskJobs.map((j) => (
                <TaskRow key={j.id} job={j} selected={j.id === selected}
                  onSelect={(x) => setSelected(x.id)} onRetry={retry} />
              ))}
          </Panel>
        </div>
        <div>
          <Panel title="结果" extra={job ? <Badge status={job.status} /> : undefined}>
            {!job && <EmptyState text="提交任务或点击左侧任务行查看结果" />}
            {job && (
              <div className="stack">
                <div className="spread">
                  <span className="caption">{KIND_TEXT[job.kind]} · 模式：{modeText(jobMode)} · 任务 #{job.id}</span>
                  <span className="spread" style={{ gap: "var(--sp-2)" }}>
                    {renaming ? (
                      <input className="input" autoFocus key={job.id} style={{ width: "14rem" }}
                        aria-label="新名称（清空恢复默认）" maxLength={60} defaultValue={jobName(job)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") e.currentTarget.blur();
                          if (e.key === "Escape") { e.currentTarget.value = jobName(job); e.currentTarget.blur(); }
                        }}
                        onBlur={(e) => { setRenaming(false); void saveName(e.target.value); }} />
                    ) : (
                      <Button variant="ghost" sm onClick={() => setRenaming(true)}>改名</Button>
                    )}
                    {blocks.length > 0 && (
                      <Button variant="ghost" sm onClick={exportAll}><Download size={14} />导出 TXT</Button>
                    )}
                  </span>
                </div>
                {job.status === "failed" && (
                  <span className="field-error" role="alert">{job.error_msg || "任务失败"}</span>
                )}
                {job.status === "cancelled" && <span className="muted">已取消</span>}
                {(job.status === "queued" || job.status === "running") && (
                  <span className="muted">{job.status === "queued" ? "等待中…" : job.progress || "进行中…"}</span>
                )}
                {job.status === "success" && blocks.length === 0 && <EmptyState text="该任务没有可展示的结果" />}
                {blocks.map((b) => <ResultBlock key={b.title} title={b.title} text={b.text} />)}
              </div>
            )}
          </Panel>
          <LogPanel jobId={selected} />
        </div>
      </div>
    </>
  );
}
