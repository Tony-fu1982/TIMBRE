/** 格式转换页 —— issue 15–19：加密/普通音频 → MP3/WAV（frontend-b）。 */
import { useState } from "react";
import { FolderOpen } from "lucide-react";
import { api } from "../api";
import { Badge, Button, EmptyState, Field, FileDrop, LogPanel, Panel, TaskRow } from "../components";
import { useStore } from "../store";
import type { Job } from "../types";

const ACCEPT = ".ncm,.qmc0,.qmc3,.qmcflac,.qmcogg,.qmcra,.mflac,.mgg,.kgm,.kgma,.kgg,.vpr,.kwm,.mp3,.wav,.flac,.m4a,.ogg,audio/*";

/** 客户端仅按扩展名提示格式，最终以后端识别为准（SPEC §4.9）。 */
const FORMAT_LABEL: Record<string, string> = {
  ncm: "NCM（网易云加密）",
  qmc0: "QMC（QQ 音乐加密）", qmc3: "QMC（QQ 音乐加密）", qmcflac: "QMC（QQ 音乐加密）",
  qmcogg: "QMC（QQ 音乐加密）", qmcra: "QMC（QQ 音乐加密）", mflac: "QMC（QQ 音乐加密）", mgg: "QMC（QQ 音乐加密）",
  kgm: "KGM（酷狗加密）", kgma: "KGMA（酷狗加密）", vpr: "VPR（酷狗加密）",
  kgg: "KGG（酷狗加密 · 部分支持）",
  kwm: "KWM（酷我加密）",
  mp3: "MP3", wav: "WAV", flac: "FLAC", m4a: "M4A", ogg: "OGG",
};
const ext = (name: string) => name.split(".").pop()?.toLowerCase() ?? "";

export function ConvertPage() {
  const { jobs, toast, errText, openFolder } = useStore();
  const [file, setFile] = useState<File | null>(null);
  const [fileErr, setFileErr] = useState("");
  const [target, setTarget] = useState("mp3");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<number | null>(null);

  const convertJobs = jobs.filter((j) => j.kind === "convert");
  const job = convertJobs.find((j) => j.id === selected) ?? null;

  const submit = async () => {
    setErr("");
    if (!file) { setFileErr("请选择要转换的音频文件"); return; }
    setBusy(true);
    try {
      const j = await api.submitConvert(file, target);
      setSelected(j.id);
      toast("已加入转换任务", "success");
    } catch (e) {
      // KEY_DB_UNAVAILABLE / UNSUPPORTED_FORMAT：直接展示后端中文指引，前端不编造路径或链接
      setErr(errText(e));
    } finally { setBusy(false); }
  };

  const retry = async (j: Job) => {
    try {
      const nj = await api.retry(j.id);
      setSelected(nj.id);
      toast("已重新入队", "success");
    } catch (e) { toast(errText(e), "error"); } // 原文件不存在等 BAD_AUDIO 中文提示来自后端
  };

  return (
    <>
      <h1 className="page-title" style={{ marginBottom: "var(--sp-4)" }}>格式转换</h1>
      <div className="two-col">
        <div>
          <Panel title="选择音频文件">
            <div className="stack">
              <Field label="音频文件" error={fileErr}>
                <FileDrop accept={ACCEPT} file={file}
                  onFile={(f) => { setFile(f); setFileErr(""); }} />
              </Field>
              {file && (
                <span className="caption">
                  检测到格式：{FORMAT_LABEL[ext(file.name)] ?? "未知"}（按扩展名提示，最终以后端识别为准）
                </span>
              )}
              {file && ext(file.name) === "kgg" && (
                <span className="field-hint" style={{ color: "var(--warning)" }}>
                  KGG 仅部分版本受支持；未支持的版本会提示「暂不支持此音频格式或版本」。
                </span>
              )}
              <Field label="输出格式">
                <select className="select" aria-label="输出格式" value={target}
                  onChange={(e) => setTarget(e.target.value)}>
                  <option value="mp3">MP3</option>
                  <option value="wav">WAV</option>
                </select>
              </Field>
              {err && <span className="field-error" role="alert">{err}</span>}
              <div><Button onClick={submit} disabled={busy}>开始转换</Button></div>
              <span className="caption">
                支持 NCM / QMC / KGM / KGMA / VPR / KGG（部分支持）/ KWM 及 MP3、WAV、FLAC、M4A、OGG；
                仅限自有或已授权下载的音频。密钥库缺失只影响确需酷狗密钥库的格式。
              </span>
            </div>
          </Panel>
          <Panel title="任务">
            {convertJobs.length === 0
              ? <EmptyState text="暂无转换任务" />
              : convertJobs.map((j) => (
                <TaskRow key={j.id} job={j} selected={j.id === selected}
                  onSelect={(x) => setSelected(x.id)} onRetry={retry} />
              ))}
          </Panel>
        </div>
        <div>
          {job && job.status === "success" && job.output_path && (
            <Panel title="产物" extra={<Badge status={job.status} />}>
              <div className="stack">
                <span className="mono" style={{ wordBreak: "break-all" }}>{job.output_path}</span>
                <div>
                  <Button variant="ghost" sm title="打开所在文件夹" aria-label="打开所在文件夹" onClick={() => void openFolder(job.id)}>
                    <FolderOpen size={14} />
                  </Button>
                </div>
              </div>
            </Panel>
          )}
          <LogPanel jobId={selected} />
        </div>
      </div>
    </>
  );
}
