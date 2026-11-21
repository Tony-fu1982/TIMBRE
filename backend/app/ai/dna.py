"""参考音频 DNA 分析（SPEC §4.8；02 §5/§6.1/§7）。

本地客观 3 维（bpm/scale/timeSignature，librosa）+ LLM 14 字段 → 17 维档案。
LLM 严禁输出本地 3 维；merge 时本地值最终覆盖。结果按模式取 SPEC 字段子集。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from ..errors import AppError
from . import audio_prep, generate, prompts, provider

LOCAL_KEYS = ("bpm", "scale", "timeSignature")

# §5.2 $s 十字段别名 + §5.4 四内联字段别名（大小写不敏感，按序首匹配）
_ALIASES: dict[str, list[str]] = {
    "style": ["style"],
    "subStyle": ["subStyle", "sub_style", "subGenre", "sub_genre"],
    "mood": ["mood", "emotions", "vibe"],
    "energy": ["energy", "intensity"],
    "instruments": ["instruments", "instrumentation", "inst"],
    "productionLogic": ["productionLogic", "production_logic", "logic"],
    "arrangementStructure": ["arrangementStructure", "structure", "arrangement"],
    "vocalStyle": ["vocalStyle", "vocal_style", "vocals"],
    "vocalGender": ["vocalGender", "vocalRole", "role", "gender", "vocal_gender"],
    "vocalRange": ["vocalRange", "range", "vocal_range"],
    "mixingStyle": ["mixingStyle", "mix", "mixing", "sound_engineering", "timbre", "soundQuality", "texture", "tone"],
    "chordProgression": ["chordProgression", "chords", "harmony", "progression"],
    "rhythmPattern": ["rhythmPattern", "rhythm", "groove", "beat_pattern"],
    "melodyContour": ["melodyContour", "melody", "melodic_shape", "topline"],
}
_ARRAY_FIELDS = {"mood", "instruments"}  # L_={en:[str],zh:[str]}，其余 Ss={en:str,zh:str}


def validate_llm(obj: dict) -> dict:
    """严格校验 14 字段：en+zh 双键、类型正确、非空；别名归一为规范键。"""
    if not isinstance(obj, dict):
        raise AppError("AI_RESULT_INVALID", "歌曲分析结果格式异常，请重试。")
    low = {str(k).lower(): v for k, v in obj.items()}
    out: dict = {}
    for field, aliases in _ALIASES.items():
        v = next((low[a.lower()] for a in aliases if a.lower() in low), None)
        if not isinstance(v, dict) or "en" not in v or "zh" not in v:
            raise AppError("AI_RESULT_INVALID", f"歌曲分析结果缺少或类型错误：{field}，请重试。")
        en, zh = v["en"], v["zh"]
        if field in _ARRAY_FIELDS:
            ok = (
                isinstance(en, list)
                and isinstance(zh, list)
                and bool(en)
                and bool(zh)
                and all(isinstance(x, str) and x.strip() for x in en + zh)
            )
            if not ok:
                raise AppError("AI_RESULT_INVALID", f"歌曲分析结果缺少或类型错误：{field}，请重试。")
            out[field] = {"en": [x.strip() for x in en], "zh": [x.strip() for x in zh]}
        else:
            if not (isinstance(en, str) and isinstance(zh, str) and en.strip() and zh.strip()):
                raise AppError("AI_RESULT_INVALID", f"歌曲分析结果缺少或类型错误：{field}，请重试。")
            out[field] = {"en": en.strip(), "zh": zh.strip()}
    return out


def merge(llm: dict, local: dict) -> dict:
    """17 维档案：本地 3 维最后覆盖（iN：LLM 越权输出也无效）。"""
    out = dict(llm)
    for k in LOCAL_KEYS:
        out[k] = local[k]
    return out


def local_dims(src) -> dict:
    """本地客观 3 维 + 节奏证据（02 §5.6/§7.2）。同步 CPU 密集，run 内 to_thread。"""
    import librosa
    import numpy as np

    y, sr = librosa.load(str(src), sr=22050, mono=True)
    dur = max(len(y) / sr, 1e-6)
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    bpm = str(int(round(float(np.atleast_1d(tempo)[0]))))
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)

    # 调性：chroma_cqt + Krumhansl-Schmuckler 24 模板相关（升号记名）
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr).mean(axis=1)
    major = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
    minor = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    best, best_i, best_mode = -2.0, 0, "Major"
    for i in range(12):
        for tpl, mode in ((major, "Major"), (minor, "Minor")):
            r = float(np.corrcoef(chroma, np.roll(tpl, i))[0, 1])
            if np.isfinite(r) and r > best:
                best, best_i, best_mode = r, i, mode
    scale = {
        "en": f"{names[best_i]} {best_mode}",
        "zh": f"{names[best_i]} {'大调' if best_mode == 'Major' else '小调'}",
    }

    # ponytail: 拍号为强拍能量启发式（项目实现，非原作 zY 逐字算法）；平手取 4/4
    env = librosa.onset.onset_strength(y=y, sr=sr)
    onset_times = librosa.onset.onset_detect(y=y, sr=sr, units="time")
    meter = 4
    if len(beat_times) >= 4 and len(env):
        pos = np.clip(librosa.time_to_frames(beat_times, sr=sr), 0, len(env) - 1)
        strengths = env[pos]
        mean_all = float(strengths.mean()) or 1.0
        scores = {}
        for m in (3, 4):
            down = strengths[np.arange(len(strengths)) % m == 0]
            scores[m] = float(down.mean()) / mean_all if len(down) else 0.0
        meter = 3 if scores[3] > scores[4] else 4
    ts = {"en": f"{meter}/4", "zh": f"{meter}/4 拍"}

    density = len(onset_times) / dur
    if len(beat_times) >= 3:
        ibi = np.diff(beat_times)
        stability = float(np.clip(1.0 - ibi.std() / (float(ibi.mean()) or 1.0), 0.0, 1.0))
    else:
        stability = 0.0
    sync = 0.0
    if len(beat_times) >= 2 and len(onset_times):
        ibi_med = float(np.median(np.diff(beat_times))) or 1.0
        idx = np.clip(np.searchsorted(beat_times, onset_times) - 1, 0, len(beat_times) - 1)
        frac = ((idx + (onset_times - beat_times[idx]) / ibi_med) % meter) / meter
        sync = float((((frac > 0.2) & (frac < 0.35)) | ((frac > 0.65) & (frac < 0.8))).mean())
    return {
        "bpm": bpm,
        "scale": scale,
        "timeSignature": ts,
        "onset_density": round(density, 2),
        "tempo_stability": round(stability, 2),
        "syncopation": round(sync, 2),
    }


async def run(job: dict, ctx) -> None:
    mode = (job.get("options") or {}).get("mode", "full")
    cfg = provider.load_ai_config("qwen")  # DNA 音频分析固定 Qwen（用户指令）
    if not cfg.get("api_key"):
        raise AppError("NO_AI_KEY", "DNA 音频分析使用 Qwen，请先到「设置」填写 Qwen API Key。")
    src = Path(job["input"])
    ctx.log("正在预处理参考音频（MP3 压缩）…")
    b64, fmt = await audio_prep.prepare_qwen_audio(src, ctx)
    ctx.check_cancel()
    ctx.log("正在本地分析速度/调性/拍号…")
    local = await asyncio.to_thread(local_dims, src)
    ctx.check_cancel()
    messages = prompts.build_dna_messages(mode)
    messages[-1]["content"] += prompts.rhythm_evidence_block(local)
    ctx.log("正在调用 Qwen 进行歌曲 DNA 综合分析…")
    raw = await provider.chat_json(cfg, messages, audio_b64=b64, audio_fmt=fmt)
    profile = merge(validate_llm(raw), local)
    ctx.log("正在整理分析结果…")
    ctx.check_cancel()

    result: dict = {}
    if mode in ("full", "style"):
        result["dnaProfile"] = profile
        result["sunoPrompt"] = await generate.run_suno_step(cfg, None, profile, ctx)
    if mode in ("full", "lyrics"):
        ctx.check_cancel()
        suno_en = result.get("sunoPrompt", {}).get("en", "")
        result["lyrics"] = await generate.run_lyrics_step(cfg, None, profile, suno_en, ctx)
    ctx.set_result(result)
    ctx.finish(result=result)
