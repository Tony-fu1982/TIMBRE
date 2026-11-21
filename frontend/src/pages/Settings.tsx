/** 设置页 —— issue 09/12/14：下载目录 / AI 配置 / 调试日志（frontend-b）。 */
import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Button, Field, Panel } from "../components";
import { useStore } from "../store";

type Provider = "deepseek" | "qwen";
/** CONTRACTS §8 的默认模型与官方 Base URL。 */
const DEFAULT_MODEL: Record<Provider, string> = {
  deepseek: "deepseek-flash",
  qwen: "qwen3.8-omni-flash",
};
const DEFAULT_BASE: Record<Provider, string> = {
  deepseek: "https://api.deepseek.com/v1",
  qwen: "https://dashscope.aliyuncs.com/compatible-mode/v1",
};

export function SettingsPage() {
  const { settings, ai, saveSettings, saveAi, toast, errText } = useStore();
  const [dir, setDir] = useState(settings.download_dir);
  const [dirErr, setDirErr] = useState("");
  const [dirOk, setDirOk] = useState(false);
  const [pickedProvider, setProvider] = useState<Provider | null>(null);
  const provider = pickedProvider ?? ai.provider;
  const [model, setModel] = useState(ai.model);
  const [baseUrl, setBaseUrl] = useState(ai.base_url);
  const [apiKey, setApiKey] = useState("");
  const [modelErr, setModelErr] = useState("");
  const [baseErr, setBaseErr] = useState("");
  const [aiErr, setAiErr] = useState("");
  const [aiOk, setAiOk] = useState(false);
  const [saving, setSaving] = useState(false);
  const [configReady, setConfigReady] = useState(false);
  const [keySet, setKeySet] = useState(false);

  useEffect(() => {
    let current = true;
    setConfigReady(false);
    const pending = provider === ai.provider && ai.model ? Promise.resolve(ai) : api.aiConfig(provider);
    pending.then((cfg) => {
      if (!current) return;
      setModel(cfg.model || DEFAULT_MODEL[provider]);
      setBaseUrl(cfg.base_url);
      setKeySet(cfg.key_set);
      setConfigReady(true);
    }).catch((e) => { if (current) setAiErr(errText(e)); });
    return () => { current = false; };
  }, [provider, ai.provider, ai.model, ai.base_url, ai.key_set, errText]);
  useEffect(() => { setDir(settings.download_dir); }, [settings.download_dir]);

  const saveDir = async () => {
    setDirErr(""); setDirOk(false);
    if (!dir.trim()) { setDirErr("请填写下载目录"); return; }
    try {
      await saveSettings({ download_dir: dir.trim() });
      setDirOk(true);
    } catch (e) {
      setDirErr(errText(e)); // DIR_INVALID：字段级红字；store 失败不更新，回退到最后有效值
      setDir(settings.download_dir);
    }
  };

  const switchProvider = (p: Provider) => {
    if (p === provider) return;
    setProvider(p);
    setModel(DEFAULT_MODEL[p]);
    setBaseUrl(DEFAULT_BASE[p]);
    setApiKey("");
    setKeySet(false);
    setConfigReady(false);
    setModelErr(""); setBaseErr(""); setAiErr("");
    setAiOk(false);
  };

  const saveAiCfg = async () => {
    if (!configReady || saving) return;
    setModelErr(""); setBaseErr(""); setAiErr(""); setAiOk(false);
    if (!model.trim()) { setModelErr("请填写模型 ID"); return; }
    const base = baseUrl.trim();
    if (base && !base.startsWith("https://")) { setBaseErr("API 地址须为 https URL（留空使用官方默认）"); return; }
    setSaving(true);
    try {
      await saveAi({
        provider, model: model.trim(), base_url: base,
        ...(apiKey ? { api_key: apiKey } : {}), // 留空 = 保持原密钥
      });
      setApiKey("");
      setAiOk(true);
    } catch (e) {
      setAiErr(e instanceof ApiError && e.code === "INVALID_PROVIDER" ? "无效的 Provider，请重新选择" : errText(e));
    } finally { setSaving(false); }
  };

  const toggleDebug = async (on: boolean) => {
    try { await saveSettings({ debug_logs: on ? "1" : "0" }); }
    catch (e) { toast(errText(e), "error"); } // 失败时 store 未更新，勾选自动回退
  };

  return (
    <>
      <h1 className="page-title" style={{ marginBottom: "var(--sp-4)" }}>设置</h1>
      <div style={{ maxWidth: 720 }}>
        <Panel title="下载目录">
          <Field label="下载目录" error={dirErr} hint={!dirErr ? "下载、转换与导出的产物保存到此目录" : undefined}>
            <input className="input" aria-label="下载目录" value={dir} placeholder="绝对路径，如 D:\Music"
              onChange={(e) => { setDir(e.target.value); setDirOk(false); }} />
          </Field>
          <div className="row" style={{ marginTop: "var(--sp-3)" }}>
            <Button onClick={saveDir}>保存</Button>
            {dirOk && <span role="status" style={{ color: "var(--success)" }}>已保存</span>}
          </div>
        </Panel>
        <Panel title="AI 配置">
          <div className="stack">
            <Field label="Provider">
              <select className="select" aria-label="AI Provider" value={provider} disabled={saving}
                onChange={(e) => switchProvider(e.target.value as Provider)}>
                <option value="deepseek">DeepSeek</option>
                <option value="qwen">千问 Qwen</option>
              </select>
            </Field>
            <Field label="模型" error={modelErr} hint={!modelErr ? `默认 ${DEFAULT_MODEL[provider]}` : undefined}>
              <input className="input" aria-label="模型" value={model} disabled={!configReady || saving}
                onChange={(e) => { setModel(e.target.value); setAiOk(false); }} />
            </Field>
            <Field label="API Key" hint="密钥仅保存在本地，不会回显">
              <input className="input" type="password" autoComplete="new-password" aria-label="API Key"
                placeholder={keySet ? "已设置（留空保持不变）" : "未设置"} disabled={!configReady || saving}
                value={apiKey} onChange={(e) => { setApiKey(e.target.value); setAiOk(false); }} />
            </Field>
            <Field label="API 地址（Base URL）" error={baseErr}
              hint={!baseErr ? "留空使用官方默认；OpenAI 兼容地址须为 https" : undefined}>
              <input className="input" aria-label="API 地址" value={baseUrl} disabled={!configReady || saving}
                onChange={(e) => { setBaseUrl(e.target.value); setAiOk(false); }} />
            </Field>
            {aiErr && <span className="field-error" role="alert">{aiErr}</span>}
            <div className="row">
              <Button onClick={saveAiCfg} disabled={!configReady || saving}>保存</Button>
              {aiOk && <span role="status" style={{ color: "var(--success)" }}>已保存</span>}
              <span className="caption">当前密钥状态：{keySet ? "已设置" : "未设置"}</span>
            </div>
          </div>
        </Panel>
        <Panel title="调试日志">
          <div className="stack">
            <label className="row" style={{ cursor: "pointer" }}>
              <input type="checkbox" checked={settings.debug_logs === "1"}
                onChange={(e) => void toggleDebug(e.target.checked)}
                style={{ width: 16, height: 16, accentColor: "var(--primary)" }} />
              <span>记录调试日志</span>
            </label>
            <span className="caption">开启后任务日志包含诊断细节（不含密钥）；勾选即时保存。</span>
          </div>
        </Panel>
      </div>
    </>
  );
}
