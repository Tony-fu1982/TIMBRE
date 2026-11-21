"""AI 模块自测（issues 12/13/14）：provider / audio_prep / dna / generate / prompts。

全部离线：httpx.MockTransport 或 monkeypatch 假 chat_json；不触网、不读真实 .env。
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app import config
from app.ai import audio_prep, dna, generate, prompts, provider
from app.errors import AppError


# ---------------------------------------------------------------- 公共桩
class Ctx:
    def __init__(self, workdir, settings=None):
        self.workdir = Path(workdir)
        self.logs: list[str] = []
        self.debug_logs: list[str] = []
        self.result = None
        self.finished = None
        self.db = SimpleNamespace(settings=lambda: settings or {})

    def log(self, msg):
        self.logs.append(msg)

    def debug(self, msg):
        self.debug_logs.append(msg)

    def stage(self, *a, **k):
        pass

    def check_cancel(self):
        pass

    def set_result(self, result):
        self.result = result

    def finish(self, output_path="", result=None):
        self.finished = result


def _ss(en, zh):
    return {"en": en, "zh": zh}


def _dna_obj():
    return {
        "style": _ss("Dream Pop", "梦幻流行"),
        "subStyle": _ss("Shoegaze", "自赏摇滚"),
        "mood": {"en": ["Dreamy", "Melancholic"], "zh": ["梦幻", "忧郁"]},
        "energy": _ss("Medium energy, gentle build", "中等能量，温柔推进"),
        "instruments": {"en": ["Electric Guitar", "Synth Pad"], "zh": ["电吉他", "合成器铺底"]},
        "mixingStyle": _ss("Wide stereo, wet reverb", "宽声场，混响偏湿"),
        "chordProgression": _ss("I - V - vi - IV", "I - V - vi - IV"),
        "rhythmPattern": _ss("4/4 Straight Backbeat", "4/4 直拍反拍"),
        "melodyContour": _ss("Stepwise rise in chorus", "副歌级进上行"),
        "productionLogic": _ss("Layered guitars build the chorus.", "叠层吉他推动副歌。"),
        "arrangementStructure": _ss("Intro -> Verse -> Chorus", "前奏 -> 主歌 -> 副歌"),
        "vocalStyle": _ss("Breathy female lead", "气声女主唱"),
        "vocalGender": _ss("Female", "女声"),
        "vocalRange": _ss("Mezzo range", "中音区"),
    }


_LOCAL = {
    "bpm": "120",
    "scale": _ss("C Major", "C 大调"),
    "timeSignature": _ss("4/4", "4/4 拍"),
    "onset_density": 2.0,
    "tempo_stability": 0.9,
    "syncopation": 0.1,
}

_SUNO_OK = {"sunoPrompt": _ss("Dream pop, 120 BPM, C Major // night drift", "梦幻流行，120 BPM，C 大调 // 夜色缓缓推进")}
_LY_ZH = {"songTitle": "《夜航》", "lyrics": "[主歌: 克制钢琴铺陈]\n夜色落在窗台\n风把灯吹开"}
_LY_EN = {"songTitle": "Night Flight", "lyrics": "[Verse: Restrained Piano]\nNight falls on the windowsill\nWind blows the lamp awake"}


class FakeChat:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, cfg, messages, **kw):
        self.calls.append((cfg, messages, kw))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _cfg(provider_name="deepseek"):
    return {"provider": provider_name, "model": "m", "base_url": "https://x/v1", "api_key": "test-key"}


def _patch_cfg(monkeypatch, name="deepseek"):
    monkeypatch.setattr(provider, "load_ai_config", lambda p=config.DEFAULT_PROVIDER: _cfg(name if name != "deepseek" else p))


def _mock_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def factory(**kw):
        kw.setdefault("transport", transport)
        return real(**kw)

    monkeypatch.setattr(provider.httpx, "AsyncClient", factory)


def _ok_response(obj):
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(obj, ensure_ascii=False)}}]})


# ---------------------------------------------------------------- provider：载荷与 URL
async def test_payload_and_url(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return _ok_response({"ok": 1})

    _mock_transport(monkeypatch, handler)
    out = await provider.chat_json(_cfg(), [{"role": "user", "content": "hi"}])
    assert out == {"ok": 1}
    req = seen[0]
    assert str(req.url) == "https://x/v1/chat/completions"
    payload = json.loads(req.content)
    assert payload["model"] == "m"
    assert payload["temperature"] == 0
    assert payload["stream"] is False
    assert payload["response_format"] == {"type": "json_object"}
    assert "modalities" not in payload
    assert req.headers["authorization"] == "Bearer test-key"


async def test_url_already_full(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return _ok_response({})

    _mock_transport(monkeypatch, handler)
    cfg = _cfg()
    cfg["base_url"] = "https://x/v1/chat/completions/"
    await provider.chat_json(cfg, [{"role": "user", "content": "hi"}])
    assert str(seen[0].url) == "https://x/v1/chat/completions"


async def test_qwen_extras_and_audio_parts(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return _ok_response({})

    _mock_transport(monkeypatch, handler)
    await provider.chat_json(
        _cfg("qwen"), [{"role": "user", "content": "analyze"}], audio_b64="ZmFrZQ==", audio_fmt="mp3"
    )
    payload = json.loads(seen[0].content)
    assert payload["modalities"] == ["text"]
    assert payload["reasoning_effort"] == "none"
    parts = payload["messages"][-1]["content"]
    assert parts[0] == {"type": "text", "text": "analyze"}
    assert parts[1] == {"type": "input_audio", "input_audio": {"data": "data:;base64,ZmFrZQ==", "format": "mp3"}}


async def test_missing_key():
    cfg = _cfg()
    cfg["api_key"] = ""
    with pytest.raises(AppError) as ei:
        await provider.chat_json(cfg, [{"role": "user", "content": "hi"}])
    assert ei.value.code == "NO_AI_KEY"


# ---------------------------------------------------------------- provider：错误映射
@pytest.mark.parametrize("status,code", [(401, "AI_AUTH_FAILED"), (403, "AI_AUTH_FAILED"), (429, "RATE_LIMITED"), (404, "AI_MODEL_UNAVAILABLE"), (400, "AI_MODEL_UNAVAILABLE"), (500, "NETWORK_ERROR")])
async def test_http_error_mapping(monkeypatch, status, code):
    _mock_transport(monkeypatch, lambda request: httpx.Response(status, text="boom"))
    with pytest.raises(AppError) as ei:
        await provider.chat_json(_cfg(), [{"role": "user", "content": "hi"}])
    assert ei.value.code == code


async def test_timeout_and_network(monkeypatch):
    def timeout_handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    _mock_transport(monkeypatch, timeout_handler)
    with pytest.raises(AppError) as ei:
        await provider.chat_json(_cfg(), [{"role": "user", "content": "hi"}])
    assert ei.value.code == "TIMEOUT"

    def conn_handler(request):
        raise httpx.ConnectError("down", request=request)

    _mock_transport(monkeypatch, conn_handler)
    with pytest.raises(AppError) as ei:
        await provider.chat_json(_cfg(), [{"role": "user", "content": "hi"}])
    assert ei.value.code == "NETWORK_ERROR"


async def test_response_format_fallback(monkeypatch):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            return httpx.Response(400, text="response_format is not supported")
        return _ok_response({"ok": 1})

    _mock_transport(monkeypatch, handler)
    out = await provider.chat_json(_cfg(), [{"role": "user", "content": "hi"}])
    assert out == {"ok": 1}
    assert len(seen) == 2
    assert "response_format" in seen[0]
    assert "response_format" not in seen[1]


async def test_key_never_leaked(monkeypatch):
    _mock_transport(monkeypatch, lambda request: httpx.Response(401, text="invalid key test-key"))
    with pytest.raises(AppError) as ei:
        await provider.chat_json(_cfg(), [{"role": "user", "content": "hi"}])
    assert "test-key" not in str(ei.value)
    assert "test-key" not in ei.value.message


# ---------------------------------------------------------------- provider：JSON 修复
async def test_fenced_json(monkeypatch):
    _mock_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": '```json\n{"a": 1}\n```'}}]}),
    )
    assert await provider.chat_json(_cfg(), [{"role": "user", "content": "hi"}]) == {"a": 1}


async def test_json_with_prose_prefix(monkeypatch):
    _mock_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": '好的：{"a": "x{y"} 结束'}}]}),
    )
    assert await provider.chat_json(_cfg(), [{"role": "user", "content": "hi"}]) == {"a": "x{y"}


@pytest.mark.parametrize("content", ["", "not json at all", "[1,2,3]"])
async def test_invalid_json(monkeypatch, content):
    _mock_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": content}}]}),
    )
    with pytest.raises(AppError) as ei:
        await provider.chat_json(_cfg(), [{"role": "user", "content": "hi"}])
    assert ei.value.code == "AI_RESULT_INVALID"


async def test_list_content_parts(monkeypatch):
    _mock_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"choices": [{"message": {"content": [{"type": "text", "text": '{"a":'}, {"type": "text", "text": "1}"}]}}]}
        ),
    )
    assert await provider.chat_json(_cfg(), [{"role": "user", "content": "hi"}]) == {"a": 1}


# ---------------------------------------------------------------- provider：配置读写
def test_config_roundtrip(isolate_env):
    provider.save_ai_config("qwen", "qwen3.8-omni-flash", api_key="k1", base_url="https://b/v1")
    cfg = provider.load_ai_config("qwen")
    assert cfg["model"] == "qwen3.8-omni-flash"
    assert cfg["api_key"] == "k1"
    assert cfg["base_url"] == "https://b/v1"
    default = provider.load_ai_config()
    assert default["provider"] == "deepseek"
    assert default["model"] == config.AI_DEFAULTS["deepseek"]["model"]


# ---------------------------------------------------------------- audio_prep
def test_bitrate_ladder():
    assert audio_prep.bitrate_ladder(60) == [192, 128, 96, 64, 48, 32]
    assert audio_prep.bitrate_ladder(200) == [128, 96, 64, 48, 32]
    assert audio_prep.bitrate_ladder(400) == [96, 64, 48, 32]
    assert audio_prep.bitrate_ladder(900) == [64, 48, 32]


async def test_prepare_ok_source_untouched(tone_wav, tmp_path):
    src = tone_wav()
    before = src.read_bytes()
    ctx = Ctx(tmp_path)
    b64, fmt = await audio_prep.prepare_qwen_audio(src, ctx)
    assert fmt == "mp3"
    assert base64.b64decode(b64)
    assert len(b64) <= audio_prep.MAX_B64_BYTES
    assert src.read_bytes() == before


async def test_prepare_all_over_limit(tone_wav, tmp_path, monkeypatch):
    monkeypatch.setattr(audio_prep, "MAX_B64_BYTES", 10)
    with pytest.raises(AppError) as ei:
        await audio_prep.prepare_qwen_audio(tone_wav(), Ctx(tmp_path))
    assert ei.value.code == "AUDIO_TOO_LARGE"
    assert "缩短音频" in ei.value.message


# ---------------------------------------------------------------- dna：本地维度
def test_local_dims_shape(tone_wav):
    out = dna.local_dims(tone_wav())
    assert isinstance(out["bpm"], str) and out["bpm"].isdigit()
    for k in ("scale", "timeSignature"):
        assert set(out[k]) == {"en", "zh"} and out[k]["en"] and out[k]["zh"]
    assert out["timeSignature"]["en"] in ("3/4", "4/4")
    assert 0.0 <= out["tempo_stability"] <= 1.0
    assert 0.0 <= out["syncopation"] <= 1.0
    assert out["onset_density"] >= 0.0


# ---------------------------------------------------------------- dna：校验与合并
def test_validate_ok():
    out = dna.validate_llm(_dna_obj())
    assert set(out) == set(dna._ALIASES)
    assert out["mood"]["en"] == ["Dreamy", "Melancholic"]


def test_validate_aliases():
    obj = _dna_obj()
    obj["sub_genre"] = obj.pop("subStyle")
    obj["vocals"] = obj.pop("vocalStyle")
    obj["intensity"] = obj.pop("energy")
    obj["groove"] = obj.pop("rhythmPattern")
    out = dna.validate_llm(obj)
    assert out["subStyle"]["en"] == "Shoegaze"
    assert out["vocalStyle"]["zh"] == "气声女主唱"
    assert out["energy"]["en"] == "Medium energy, gentle build"


def test_validate_missing_energy():
    obj = _dna_obj()
    del obj["energy"]
    with pytest.raises(AppError) as ei:
        dna.validate_llm(obj)
    assert ei.value.code == "AI_RESULT_INVALID"
    assert "energy" in ei.value.message


@pytest.mark.parametrize("mutate", [lambda o: o.update({"style": "Pop"}), lambda o: o["mood"].update({"en": "Dreamy"}), lambda o: o["style"].update({"zh": "  "})])
def test_validate_bad_types(mutate):
    obj = _dna_obj()
    mutate(obj)
    with pytest.raises(AppError) as ei:
        dna.validate_llm(obj)
    assert ei.value.code == "AI_RESULT_INVALID"


def test_merge_local_wins():
    llm = dna.validate_llm(_dna_obj())
    llm["bpm"] = "999"  # LLM 越权输出必须被本地覆盖
    llm["scale"] = _ss("Wrong", "错")
    out = dna.merge(llm, _LOCAL)
    assert len(out) == 17
    assert out["bpm"] == "120"
    assert out["scale"]["en"] == "C Major"
    assert out["timeSignature"]["en"] == "4/4"


# ---------------------------------------------------------------- dna.run / generate.run（全离线）
def _patch_dna_env(monkeypatch, tmp_path):
    _patch_cfg(monkeypatch, "qwen")
    monkeypatch.setattr(dna, "local_dims", lambda src: dict(_LOCAL))

    async def fake_prep(src, ctx):
        return "ZmFrZQ==", "mp3"

    monkeypatch.setattr(audio_prep, "prepare_qwen_audio", fake_prep)


async def test_dna_run_full(monkeypatch, tmp_path):
    _patch_dna_env(monkeypatch, tmp_path)
    fake = FakeChat([_dna_obj(), _SUNO_OK, _LY_ZH, _LY_EN])
    monkeypatch.setattr(provider, "chat_json", fake)
    ctx = Ctx(tmp_path)
    job = {"input": str(tmp_path / "a.wav"), "options": {"mode": "full"}}
    await dna.run(job, ctx)
    r = ctx.finished
    assert set(r) == {"dnaProfile", "sunoPrompt", "lyrics"}
    assert len(r["dnaProfile"]) == 17
    assert r["dnaProfile"]["bpm"] == "120"
    assert set(r["lyrics"]) == {"zh", "en"} and set(r["sunoPrompt"]) == {"zh", "en"}
    assert ctx.result is r
    # P3 带音频；P10 在 P20 之前
    assert fake.calls[0][2]["audio_b64"] == "ZmFrZQ=="
    assert "格莱美" in fake.calls[0][1][-1]["content"]
    assert "本地节奏客观指标" in fake.calls[0][1][-1]["content"]
    assert "sunoPrompt" in fake.calls[1][1][-1]["content"]
    assert "作词人" in fake.calls[2][1][-1]["content"]


async def test_dna_run_lyrics(monkeypatch, tmp_path):
    _patch_dna_env(monkeypatch, tmp_path)
    fake = FakeChat([_dna_obj(), _LY_ZH, _LY_EN])
    monkeypatch.setattr(provider, "chat_json", fake)
    ctx = Ctx(tmp_path)
    await dna.run({"input": "a.wav", "options": {"mode": "lyrics"}}, ctx)
    assert set(ctx.finished) == {"lyrics"}
    # lyrics 模式无 P10：${t} 为空
    assert '""' in fake.calls[1][1][-1]["content"]


async def test_dna_run_style(monkeypatch, tmp_path):
    _patch_dna_env(monkeypatch, tmp_path)
    fake = FakeChat([_dna_obj(), _SUNO_OK])
    monkeypatch.setattr(provider, "chat_json", fake)
    ctx = Ctx(tmp_path)
    await dna.run({"input": "a.wav", "options": {"mode": "style"}}, ctx)
    assert set(ctx.finished) == {"dnaProfile", "sunoPrompt"}


async def test_dna_no_qwen_key(monkeypatch, tmp_path):
    monkeypatch.setattr(provider, "load_ai_config", lambda p="deepseek": {"provider": "qwen", "model": "m", "base_url": "https://x/v1", "api_key": ""})
    with pytest.raises(AppError) as ei:
        await dna.run({"input": "a.wav", "options": {}}, Ctx(tmp_path))
    assert ei.value.code == "NO_AI_KEY"
    assert "Qwen" in ei.value.message


def _gen_job(theme="写一首关于夜航的歌", mode="full", dna_result=None):
    opts = {"mode": mode}
    if dna_result is not None:
        opts["dna"] = dna_result
    return {"input": theme, "options": opts}


async def test_generate_run_full(monkeypatch, tmp_path):
    _patch_cfg(monkeypatch)
    fake = FakeChat([_SUNO_OK, _LY_ZH, _LY_EN])
    monkeypatch.setattr(provider, "chat_json", fake)
    ctx = Ctx(tmp_path)
    await generate.run(_gen_job(), ctx)
    r = ctx.finished
    assert set(r) == {"lyrics", "sunoPrompt"}
    assert r["lyrics"]["zh"].startswith("[主歌")
    assert "《" not in r["lyrics"]["en"]
    # P10 先行，P20 的 ${t} 注入 sunoPrompt.en
    assert "sunoPrompt" in fake.calls[0][1][-1]["content"]
    assert _SUNO_OK["sunoPrompt"]["en"] in fake.calls[1][1][-1]["content"]


async def test_generate_run_lyrics_and_style(monkeypatch, tmp_path):
    _patch_cfg(monkeypatch)
    fake = FakeChat([_LY_ZH, _LY_EN, _SUNO_OK])
    monkeypatch.setattr(provider, "chat_json", fake)
    ctx = Ctx(tmp_path)
    await generate.run(_gen_job(mode="lyrics"), ctx)
    assert set(ctx.finished) == {"lyrics"}
    ctx2 = Ctx(tmp_path)
    await generate.run(_gen_job(mode="style"), ctx2)
    assert set(ctx2.finished) == {"sunoPrompt"}


async def test_generate_with_dna_snapshot(monkeypatch, tmp_path):
    _patch_cfg(monkeypatch)
    profile = dna.merge(dna.validate_llm(_dna_obj()), _LOCAL)
    fake = FakeChat([_SUNO_OK, _LY_ZH, _LY_EN])
    monkeypatch.setattr(provider, "chat_json", fake)
    ctx = Ctx(tmp_path)
    await generate.run(_gen_job(theme="", dna_result={"dnaProfile": profile}), ctx)
    assert set(ctx.finished) == {"lyrics", "sunoPrompt"}
    p10 = fake.calls[0][1][-1]["content"]
    assert "120" in p10 and "锁定事实清单" in p10
    p20 = fake.calls[1][1][-1]["content"]
    assert "音乐 DNA 背景" in p20 and "DNA 使用规则" in p20


async def test_generate_instrumental_profile(monkeypatch, tmp_path):
    _patch_cfg(monkeypatch)
    profile = dna.merge(dna.validate_llm(_dna_obj()), _LOCAL)
    profile["vocalStyle"] = _ss("No lead vocals / Instrumental", "无人声主唱 / 纯器乐")
    fake = FakeChat([_SUNO_OK])
    monkeypatch.setattr(provider, "chat_json", fake)
    await generate.run(_gen_job(mode="style", dna_result={"dnaProfile": profile}), Ctx(tmp_path))
    p10 = fake.calls[0][1][-1]["content"]
    assert "纯器乐模式" in p10
    assert "主奏乐器音色" in p10


@pytest.mark.parametrize("job", [{"input": "", "options": {}}, {"input": "  ", "options": {"dna": {"lyrics": {}}}}, {"input": "", "options": {"dna": {"dnaProfile": "bad"}}}])
async def test_generate_invalid_input(monkeypatch, tmp_path, job):
    _patch_cfg(monkeypatch)
    with pytest.raises(AppError) as ei:
        await generate.run(job, Ctx(tmp_path))
    assert ei.value.code == "INVALID_GENERATION_INPUT"


async def test_generate_no_key(monkeypatch, tmp_path):
    monkeypatch.setattr(provider, "load_ai_config", lambda p="deepseek": {"provider": "deepseek", "model": "m", "base_url": "https://x/v1", "api_key": ""})
    with pytest.raises(AppError) as ei:
        await generate.run(_gen_job(), Ctx(tmp_path))
    assert ei.value.code == "NO_AI_KEY"


# ---------------------------------------------------------------- 纯度修复
async def test_lyrics_zh_purity_retry_then_fail(monkeypatch, tmp_path):
    _patch_cfg(monkeypatch)
    bad = {"songTitle": "T", "lyrics": "[Verse]\nHello world"}
    fake = FakeChat([_SUNO_OK, bad, dict(bad)])
    monkeypatch.setattr(provider, "chat_json", fake)
    with pytest.raises(AppError) as ei:
        await generate.run(_gen_job(), Ctx(tmp_path))
    assert ei.value.code == "AI_RESULT_INVALID"
    assert len(fake.calls) == 3  # P10 + P20 + 一次修复


async def test_lyrics_en_purity_retry_success(monkeypatch, tmp_path):
    _patch_cfg(monkeypatch)
    bad_en = {"songTitle": "T", "lyrics": "[主歌]\n夜色"}
    fake = FakeChat([_SUNO_OK, _LY_ZH, bad_en, _LY_EN])
    monkeypatch.setattr(provider, "chat_json", fake)
    ctx = Ctx(tmp_path)
    await generate.run(_gen_job(), ctx)
    assert ctx.finished["lyrics"]["en"] == _LY_EN["lyrics"]
    assert len(fake.calls) == 4


async def test_suno_purity_repair(monkeypatch, tmp_path):
    _patch_cfg(monkeypatch)
    bad = {"sunoPrompt": _ss("梦幻流行 // 夜色", "Dream pop // night")}  # en/zh 语言互换
    fake = FakeChat([bad, {"text": "Dream pop // night drift"}, {"text": "梦幻流行 // 夜色推进"}, _LY_ZH, _LY_EN])
    monkeypatch.setattr(provider, "chat_json", fake)
    ctx = Ctx(tmp_path)
    await generate.run(_gen_job(), ctx)
    sp = ctx.finished["sunoPrompt"]
    assert sp["en"] == "Dream pop // night drift"
    assert sp["zh"] == "梦幻流行 // 夜色推进"


async def test_suno_purity_repair_fail(monkeypatch, tmp_path):
    _patch_cfg(monkeypatch)
    bad = {"sunoPrompt": _ss("只有中文", "只有中文")}
    fake = FakeChat([bad, {"text": "还是中文"}, {"text": "still Chinese"}])
    monkeypatch.setattr(provider, "chat_json", fake)
    with pytest.raises(AppError) as ei:
        await generate.run(_gen_job(mode="style"), Ctx(tmp_path))
    assert ei.value.code == "AI_RESULT_INVALID"


async def test_lyrics_missing_title(monkeypatch, tmp_path):
    _patch_cfg(monkeypatch)
    fake = FakeChat([_SUNO_OK, {"lyrics": "[主歌]\n夜色"}])
    monkeypatch.setattr(provider, "chat_json", fake)
    with pytest.raises(AppError) as ei:
        await generate.run(_gen_job(), Ctx(tmp_path))
    assert ei.value.code == "AI_RESULT_INVALID"
    assert "歌名" in ei.value.message


# ---------------------------------------------------------------- prompts 构建器
def test_builders_no_unresolved_placeholders():
    profile = dna.merge(dna.validate_llm(_dna_obj()), _LOCAL)
    for msgs in (
        prompts.build_dna_messages("full"),
        prompts.build_generate_messages("style", "夜航", profile),
        prompts.build_lyrics_messages("夜航", "prompt en", profile),
        prompts.build_lyrics_translate_messages(_LY_ZH),
        prompts.build_p1_repair_messages("some english"),
        prompts.build_e_repair_messages("一些中文"),
    ):
        content = msgs[-1]["content"]
        assert "${" not in content
        assert content.strip()


def test_p3_example_and_clause():
    content = prompts.build_dna_messages("full")[-1]["content"]
    assert "本项目附加要求" in content
    assert "严禁输出 bpm" in content
    assert set(prompts.P3_EXAMPLE) == set(dna._ALIASES)


def test_lyrics_messages_rubric_and_priority():
    content = prompts.build_lyrics_messages("夜航", "", None)[-1]["content"]
    assert "歌词质量评估与优化准则" in content
    assert "写作优先级规则" in content
    assert "元标签生成规则" in content
    assert "DNA 使用规则" not in content  # 无 DNA 不注入 P7
