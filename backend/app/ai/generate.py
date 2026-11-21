"""主题/DNA 生成（SPEC §4.8 结果表；02 §6.2：SUNO 先行 → 歌词）。

结果只含模式字段：full→lyrics+sunoPrompt；lyrics→lyrics；style→sunoPrompt。
lyrics/sunoPrompt 均为 {zh, en}。dna.run 复用本模块的 run_suno_step / run_lyrics_step。
"""
from __future__ import annotations

import re

from .. import config
from ..errors import AppError
from . import prompts, provider

_CJK = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[a-z]", re.I)
_RESIDUAL_EN = re.compile(r"[A-Za-z]{2,}")

# ponytail: 纯度修复一次重试即止（不复刻原作 xN/CN ×3 循环）
_RETRY_ZH = {
    "role": "user",
    "content": "上一次输出不符合语言要求：中文歌词必须包含汉字。请重新创作并输出完整结果，仍按原 JSON 格式（songTitle、lyrics）。",
}
_RETRY_EN = {
    "role": "user",
    "content": (
        "The previous output failed the language check: English lyrics must contain Latin letters "
        "and NO Chinese characters. Regenerate the full result in the same JSON format (songTitle, lyrics)."
    ),
}


def _pure(text: str, lang: str) -> bool:
    """§5.4 pi：en 须含拉丁字母且无 CJK；zh 须含汉字。"""
    if lang == "zh":
        return bool(_CJK.search(text))
    return bool(_LATIN.search(text)) and not _CJK.search(text)


def _norm_title(t) -> str:
    """NN：去书名号/引号/换行，≤60 字符。"""
    if not isinstance(t, str):
        return ""
    t = t.strip().strip("《》「」\"'“”‘’").replace("\n", " ").replace("\r", " ")
    return re.sub(r"\s+", " ", t).strip()[:60]


def _lyrics_fields(obj: dict) -> tuple[str, str]:
    title = _norm_title(obj.get("songTitle"))
    lyrics = obj.get("lyrics")
    if not title:  # t3
        raise AppError("AI_RESULT_INVALID", "模型未返回有效歌名，请重试。")
    if not isinstance(lyrics, str) or not lyrics.strip():
        raise AppError("AI_RESULT_INVALID", "模型未返回有效歌词，请重试。")
    return title, lyrics.strip()


async def _plain(cfg: dict, messages: list[dict]) -> str:
    obj = await provider.chat_json(cfg, messages)
    t = obj.get("text")
    if not isinstance(t, str) or not t.strip():
        raise AppError("AI_RESULT_INVALID", "模型没有返回目标语言的 Suno 提示词，请重试。")
    return t.strip()


async def run_suno_step(cfg: dict, theme: str | None, profile: dict | None, ctx) -> dict:
    """P10 单调用双语输出；纯度不达标时用 E（中→英）/ P1（英→中）各修复一次。"""
    msgs = prompts.build_generate_messages("style", theme, profile)
    obj = await provider.chat_json(cfg, msgs)
    sp = obj.get("sunoPrompt")
    if (
        not isinstance(sp, dict)
        or not isinstance(sp.get("en"), str)
        or not isinstance(sp.get("zh"), str)
        or not sp["en"].strip()
        or not sp["zh"].strip()
    ):
        raise AppError("AI_RESULT_INVALID", "模型未返回有效的 Suno 提示词，请重试。")
    en, zh = sp["en"].strip(), sp["zh"].strip()
    if not _pure(en, "en"):
        en = await _plain(cfg, prompts.build_e_repair_messages(zh))
    if not _pure(zh, "zh"):
        zh = await _plain(cfg, prompts.build_p1_repair_messages(en))
    if not _pure(en, "en") or not _pure(zh, "zh"):
        raise AppError("AI_RESULT_INVALID", "模型没有返回目标语言的 Suno 提示词，请重试。")
    if _RESIDUAL_EN.search(zh):  # zi best-effort：仅告警（音乐记号白名单除外）
        ctx.log("中文提示词含少量英文残留（标准音乐记号除外），已保留。")
    return {"zh": zh, "en": en}


async def run_lyrics_step(cfg: dict, theme: str | None, profile: dict | None, suno_en: str, ctx) -> dict:
    """P20 中文创作 → 项目编写的严格英文翻译（原作 §6.3 双次创作的替代，见报告）。

    songTitle 按 NN/t3 校验后丢弃：SPEC 结果表无歌名槽位（契约缺口，见报告）。
    """
    msgs = prompts.build_lyrics_messages(theme, suno_en, profile)
    obj = await provider.chat_json(cfg, msgs)
    title, zh = _lyrics_fields(obj)
    if not _pure(zh, "zh"):
        obj = await provider.chat_json(cfg, msgs + [_RETRY_ZH])
        title, zh = _lyrics_fields(obj)
        if not _pure(zh, "zh"):
            raise AppError("AI_RESULT_INVALID", "模型未返回有效的中文歌词，请重试。")
    if _RESIDUAL_EN.search(zh):
        ctx.log("中文歌词含少量英文残留，已按 best-effort 保留。")
    ctx.check_cancel()
    tmsgs = prompts.build_lyrics_translate_messages({"songTitle": title, "lyrics": zh})
    tobj = await provider.chat_json(cfg, tmsgs)
    _, en = _lyrics_fields(tobj)
    if not _pure(en, "en"):
        tobj = await provider.chat_json(cfg, tmsgs + [_RETRY_EN])
        _, en = _lyrics_fields(tobj)
        if not _pure(en, "en"):
            raise AppError("AI_RESULT_INVALID", "模型未返回有效的英文歌词，请重试。")
    return {"zh": zh, "en": en}


async def run(job: dict, ctx) -> None:
    opts = job.get("options") or {}
    mode = opts.get("mode", "full")
    theme = (job.get("input") or "").strip()
    dna = opts.get("dna")
    profile = dna.get("dnaProfile") if isinstance(dna, dict) else None
    if profile is not None and not isinstance(profile, dict):
        raise AppError("INVALID_GENERATION_INPUT")
    if not theme and not profile:
        raise AppError("INVALID_GENERATION_INPUT")

    provider_name = ctx.db.settings().get("ai_provider") or config.DEFAULT_PROVIDER
    cfg = provider.load_ai_config(provider_name)
    if not cfg.get("api_key"):
        raise AppError("NO_AI_KEY")

    result: dict = {}
    suno_en = ""
    if mode in ("full", "style"):
        ctx.log("正在生成 SUNO 提示词…")
        sp = await run_suno_step(cfg, theme or None, profile, ctx)
        result["sunoPrompt"] = sp
        suno_en = sp["en"]
    if mode in ("full", "lyrics"):
        ctx.check_cancel()
        ctx.log("正在创作歌词…")
        result["lyrics"] = await run_lyrics_step(cfg, theme or None, profile, suno_en, ctx)
    ctx.set_result(result)
    ctx.finish(result=result)
