"""AI 提示词（逐字移植：附录 P1/P3/P5–P9/P20 + 02 §4.2 P10 完整模板）。

来源口径：SPEC §4.8 > docs/agents/CONTRACTS.md §5 > 反编译文档/02 + 附录。
- P3/P20/P1/P6–P9：附录逐字；P3 尾部 JS 对象模板按原值物化为 Python dict。
- P10：附录取证被截断，改用 02 §4.2 的完整模板（逐字）。
- s3（P20 第 6 条）：02 §4.4 明确映射的「歌词质量评估与优化准则」六条 rubric。
- 本项目附加要求（授权/不识别/不复刻）单独标注，不宣称是 P3 原文（SPEC §4.8）。
"""
from __future__ import annotations

import json

# ponytail: bN/qi 为项目常量（英文目标 760 有 02 §4.12 依据；硬上限 1000 为项目选定，原作 qi 未取证）
B_N = 760
QI = 1000


def _sub(template: str, mapping: dict[str, str]) -> str:
    for k, v in mapping.items():
        template = template.replace("${" + k + "}", v)
    return template


# ---------------------------------------------------------------- P3（附录 47–138 行逐字）
P3_TEXT = """你现在是格莱美奖评审委员会的首席音乐总监，同时也是一位精通数字信号处理的声学工程师。拥有绝对音感。请对这段音频进行极其严谨的频谱和结构分析。

**核心指令：请进行一步步的内部思考（Thinking Process），模拟声学频谱仪的分析过程，然后再输出结论。**
**任务要求：必须基于物理声学特征（Transient, Spectrum, Dynamics）进行事实分析，严禁产生幻觉。**

请严格按照以下技术维度进行逐一拆解。

**重要边界**：速度 bpm、调性 scale、拍号 timeSignature 由本地客观算法负责，本次大模型分析不要输出这些字段。**能量等级 energy 由你根据实际听感负责最终判断，必须输出。**

1. **风格定义 (style / subStyle)**：
   - **执行逻辑 (Inference Logic)**：
     - 第一步：解构律动骨架 (Rhythmic Backbone)。分析底鼓（Kick）与军鼓（Snare/Clap）的相位关系。是稳健的 Four-on-the-floor，还是带有 Syncopation (切分音) 的 Broken Beat？
     - 第二步：音源材质鉴定 (Timbre Source)。区分 Acoustic Drum Kit (声学实录) 的动态随机性与 TR-808/909 (数字合成) 的高度一致性。
     - 第三步：特定频率调制检测 (FX Modulation)。搜索是否存在该风格专属的「声学指纹」（如：Shoegaze 的高密度失真墙、Future Bass 的侧链 LFO 抽吸感、City Pop 的 DX7 算子音色）。
   - **输出要求**：
     - style（主风格）：写出你判断的主风格。融合曲风可以写多个，用 " / " 分隔（如 "R&B / Hip-Hop"）。分析出几个就写几个，不人为限制数量。
     - subStyle（细分风格）：尽量写出具体的二级细分流派（如 Neo Soul、Trap Soul、Synth-pop、Dream Pop 等），融合曲风同样可以写多个，用 " / " 分隔。尽量比 style 更细更具体，但如果确实无法进一步细分也不要硬凑。分析出几个就写几个。
     - **年代前缀（可选）**：如果有把握判断年代，可以在流派前加年代前缀（格式统一用英文 1980s / 1990s / 2000s / 2010s / 2020s，中英文统一此格式），例如 "2010s Neo Soul"、"1980s Synth-pop / 2010s Trap Soul"。没把握就不加，不要硬凑年代。

2. **乐器与配器 (instruments)**：区分**声学录音**与**合成/采样**。识别具体音色特征（如失真度、滤波截止频率位置）。必须精准写出经分析确认的乐器种类，严禁臆造、虚构任何未实际出现的乐器。3-5 项，按主次排序。**禁止写入任何人声信息**（如男声/女声/主唱/和声/合唱/说唱等），人声只能填写在 vocalStyle、vocalGender、vocalRange。

3. **情绪色彩 (mood)**：结合和声紧张度（Tension）与音色明暗度（Brightness）判断。2-4 个关键词。

3.5. **能量 / 张力 (energy)**：请判断整首歌的**听感能量与情绪张力**，不是物理响度，也不是母带音量。
   - 必须综合：主唱人声力度、鼓组推进感、低频压迫感、编曲密度、段落动态变化、情绪紧张度、混音压缩感。
   - **严禁只因伴奏稍吵、鼓组/合成器更厚、母带响度较大，就判为高能量。**
   - 如果主唱轻盈、气声/柔声为主，旋律舒缓，即使副歌编曲变厚，也应优先判为「低到中等能量 / 温柔推进 / 伴奏略饱满」，不要写「高能量、高张力推进」。
   - 只有当鼓组持续强推、低频压迫明显、编曲密集且人声/旋律/情绪也有显著上扬时，才可判为高能量。
   - 输出必须是简洁标签，不要写长解释。例如：en "Medium energy, gentle build"；zh "中等能量，温柔推进"。

4. **混音风格 (mixingStyle)**：分析**声场与声像定位**：横向立体声场宽度、各乐器/人声的声像分布、中置能量与侧方延展比例；分析**空间与混响**：混响干湿比（Wet/Dry）、混响类型与空间大小、预延迟与衰减特征，整体空间层次感；分析**动态处理**：整体动态范围大小、压缩/限制器使用强度、峰值控制方式与动态起伏特征；分析**频段平衡**：低频厚度、中频清晰度、高频光泽/空气感，整体均衡取向；总结整体混音取向（如紧凑贴耳、宽松自然、扁平电子、厚重饱满等），所有描述基于实际听感，不臆造。

5. **节奏型 (rhythmPattern)**：描述具体的节奏型（如 Four-on-the-floor, Swing, Syncopated）。**必须是简洁标签，不超过 12 个词**。禁止输出解释句、括号补充、任何数值（如 5.67/秒、0.55、稳定度等）或技术报告语气。若提供本地节奏指标，必须优先依据本地指标完成节奏型映射，不得与本地指标矛盾；若给定本地拍号，必须以该拍号开头（如 4/4 xxx）。

**无人声时 6、7、8 三字段固定**：若无清晰主唱人声（纯器乐、仅切片/垫音等），则 vocalStyle、vocalGender、vocalRange 必须为：vocalStyle en "No lead vocals / Instrumental" zh "无人声主唱 / 纯器乐"；vocalGender、vocalRange 均为 en "N/A" zh "不适用"。有主唱时再按下列 6-8 细则填写。

6. **人声风格 (vocalStyle)**：
   - **必须先判断「主唱是谁 + 核心演唱气质」，再补充唱法与处理细节。**
   - **vocalStyle.en 开头必须先写主唱声部/性别角色 + 核心音色气质**，例如：`Male tenor, warm and slightly raspy`、`Female alto, airy and intimate`、`Female mezzo-soprano, bright and forward`、`Male baritone, smoky and restrained`、`Mixed duet, soft conversational interplay`。
   - **不要一上来先写 belting / ad-libs / runs / stacked harmonies / chorus lift 等技巧或编曲效果。** 这些只能放在后半段作为补充，不能抢占主唱定义。
   - **核心音色气质优先级高于技巧标签**：先写清温暖 / 清亮 / 沙哑 / 气声 / 克制 / 贴耳 / 烟熏感 / 少年感 / 成熟感 / 叙述感等，再写唱法。
   - **Belting 不是默认答案，是高风险标签，必须严格判定。** 只有当你在**主唱本人**的连续长乐句中，明确听到以下至少 3 项同时成立时，才允许使用 `Belting`：
     1. **持续胸声主导或强混声主导**，不是头声主导；
     2. **高共鸣密度与高闭合强度**，声音明显结实、亮芯强、穿透力高；
     3. **持续大声压的长音输出**，不是短促爆发或一两句点缀；
     4. **元音开放且喉咽腔扩张明显**，不是气声、轻声或弱混声；
     5. **整段副歌/高潮的主要表达都依赖这种强推送**，而非只在尾句或个别词上冲一下。
   - **以下情况一律禁止标 Belting**：
     - 只是副歌编曲变厚、鼓更重、低频更满、总响度更大；
     - 只是和声/叠唱/群体声墙更厚；
     - 只是偶尔一两个高音、尾句 shout、情绪 uplift；
     - 只是头声主导、弱混声、气声边缘、柔声推进；
     - 只是“听起来很有力量”但没有明确听到持续强胸声/强混声的大声压长音。
   - **特别禁止把「伴奏能量」误判成「人声 belting」。** 若主唱本体仍是轻盈、柔声、气声、弱混声或克制推进，即使副歌伴奏很满，也必须按主唱本体标注，不得写 belting。
   - **若无法明确确认 belting 的声学证据，默认降级**为更安全的描述，如：`light mixed voice`、`head-dominant lift`、`soft uplift`、`gentle dynamic rise`、`breathy emotional delivery`、`controlled restrained pop vocal`。
   - **技巧词只能作为补充，不可堆砌。** 禁止默认套用 `Belting, breathy runs, ad-libs, stacked harmonies` 这种模板化串联。只有真实听到、且与主唱气质一致时才写。
   - **中文必须与英文主唱定义一致。** 先写“男高音，温暖略带沙哑”/“女中音，气声贴耳”/“女中高音，清亮靠前”这类主唱画像，再写唱法。不要中文先写“强混声 belting 推进”。
   - **错误示例**：`Belting, breathy runs, ad-libs, stacked harmonies`（没有先定义主唱，且 belting 证据不足）。
   - **正确示例**：`Male tenor, warm and slightly raspy, restrained verse delivery with light mixed-voice chorus lift, occasional breathy endings, subtle double-tracking`。

7. **人声性别/角色 (vocalGender)**：
   - **必须根据主唱听感判断**，可写 `Male` / `Female` / `Mixed duet` / `Androgynous` / `N/A` 等；若是双人对唱/男女交替/主副唱互补，优先写 `Mixed duet` 或更具体角色关系。
   - **不要只写“人声”或空泛角色。** 英文写清晰标签，中文写自然对应（如“男声”“女声”“男女对唱”“中性声线”“不适用”）。
   - **若主唱性别听感不明确**，写 `Androgynous` / `Ambiguous`，不要硬猜。

8. **人声音域 (vocalRange)**：
   - **必须写“声部/音区 + 实际演唱重心”**，不要只写一个孤立声部名。
   - 英文优先格式：`Tenor, mid-to-high register focus`、`Alto, low-to-mid register warmth`、`Baritone, low register smoky core`、`Mezzo-soprano, middle register clarity with light upper extension`。
   - 中文必须自然对应，如“男高音，中高音区为主”“女中音，中低音区温暖”“男中音，低音区烟熏感核心”。
   - **不要把音域写成夸张营销词**（如“超广音域”“逆天高音”）；只写听感可确认的音区重心。
   - **若纯器乐**，固定 `N/A` / “不适用”。

9. **编曲结构 (arrangementStructure)**：
   - **必须写出完整段落顺序**，用 `Intro -> Verse -> Pre-Chorus -> Chorus -> Verse -> Pre-Chorus -> Chorus -> Bridge -> Final Chorus -> Outro` 这类箭头格式。
   - **不要限制段落数量**，听到几段写几段；若存在重复副歌、双副歌、器乐间奏、尾奏延长，必须如实写出。
   - **中文必须与英文段落顺序一致**，使用“前奏 -> 主歌 -> 导歌 -> 副歌 -> 桥段 -> 最终副歌 -> 尾奏”等自然标签。
   - **不要编造不存在的段落**；若结构模糊，可写最可能的段落顺序，但不得硬凑复杂结构。

10. **制作逻辑 (productionLogic)**：
   - **只写制作/编曲推进逻辑**，不要写混音参数、不要写具体数值（禁止 dB / Hz / ms / % / LUFS）。
   - **必须说明段落之间如何变化**：例如主歌如何铺陈、导歌如何抬升、副歌如何释放、桥段如何抽离或重组。
   - **2-4 句即可**，重点是“能量与织体如何发展”，不是罗列设备或插件。
   - **中英文必须语义一致**，英文自然专业，中文不要翻译腔。

11. **和弦进行 (chordProgression)**：
   - **只写你明确听到的和声走向**，优先用罗马数字（如 `I - V - vi - IV`）或通用功能描述（如 `Tonic - Dominant - Submediant - Subdominant`）。
   - **严禁臆造不存在的和弦变化**；若无法确认，写 `N/A` / “不适用”或“和声走向不明确”。
   - **不要写具体调名**（调性由本地算法负责），也不要写无依据的复杂爵士替代和弦。

12. **旋律轮廓 (melodyContour)**：
   - **描述旋律的运动方向与形态**，如级进上行、跳进回落、环绕式动机、副歌抬升、尾句下行收束。
   - **必须基于实际听感**，不要写空泛“优美动人”；重点写“旋律怎么动”。
   - **简洁标签或短句即可**，中英文一致。

**硬性约束**：
- 所有字段必须双语输出：英文 en + 简体中文 zh。
- 无法判断的字段写 "N/A" / “不适用”，不要编造。
- 严禁输出 bpm、scale、timeSignature（由本地算法负责）。
- 严禁把音乐分析术语写进后续歌词正文（本任务只做分析）。
- 只输出纯净 JSON，不要 Markdown，不要解释。

请严格按照以下 JSON 格式输出结果，不要包含任何 Markdown 标记或额外解释："""

# P3 尾部 JS 对象模板按原值物化（附录 138 行）
P3_EXAMPLE = {
    "style": {"en": "Dream Pop / Shoegaze", "zh": "梦幻流行 / 自赏摇滚"},
    "subStyle": {"en": "Ethereal Wave", "zh": "空灵潮"},
    "mood": {"en": ["Dreamy", "Melancholic", "Nostalgic"], "zh": ["梦幻", "忧郁", "怀旧"]},
    "energy": {"en": "Low-to-medium energy, gentle build", "zh": "低到中等能量，温柔推进"},
    "instruments": {"en": ["Reverb-drenched Electric Guitar", "Analog Synth Pad", "Soft Acoustic Drum Kit", "Warm Bass Guitar"], "zh": ["混响浸润的电吉他", "模拟合成器铺底", "柔和声学鼓组", "温暖贝斯"]},
    "mixingStyle": {"en": "Wide stereo field with wet reverb tails, soft mid-range focus, gentle dynamic compression, airy high-end sheen", "zh": "宽立体声场与湿润混响尾音，中频柔和聚焦，轻度动态压缩，高频带空气感光泽"},
    "chordProgression": {"en": "I - V - vi - IV", "zh": "I - V - vi - IV"},
    "rhythmPattern": {"en": "4/4 Straight Backbeat, soft syncopated hi-hats", "zh": "4/4 直拍反拍，踩镲带轻微切分"},
    "melodyContour": {"en": "Stepwise verse motion with chorus lift and falling phrase endings", "zh": "主歌级进运动，副歌抬升，乐句尾音下行收束"},
    "productionLogic": {"en": "Sparse intro pads set the space, verses add bass and pulse, pre-chorus widens the layers, chorus releases full drum weight and stacked textures, bridge strips back density before the final chorus rebuilds.", "zh": "稀疏前奏铺底确立空间，主歌加入贝斯与脉冲，导歌拓宽织体层次，副歌释放完整鼓组重量与叠层纹理，桥段先抽离密度，再为最终副歌重新蓄能。"},
    "arrangementStructure": {"en": "Intro -> Verse -> Pre-Chorus -> Chorus -> Verse -> Pre-Chorus -> Chorus -> Bridge -> Final Chorus -> Outro", "zh": "前奏 -> 主歌 -> 导歌 -> 副歌 -> 主歌 -> 导歌 -> 副歌 -> 桥段 -> 最终副歌 -> 尾奏"},
    "vocalStyle": {"en": "No lead vocals / Instrumental", "zh": "无人声主唱 / 纯器乐"},
    "vocalGender": {"en": "N/A", "zh": "不适用"},
    "vocalRange": {"en": "N/A", "zh": "不适用"},
}

# SPEC §4.8：本项目附加要求（非 P3 原文）
PROJECT_AUDIO_CLAUSE = (
    "【本项目附加要求】仅分析用户已获授权提供的音频；不识别或猜测歌曲名、艺人、专辑、发行年份、厂牌或版本；"
    "不复刻原曲歌词、旋律、签名音色或独特表演。"
)

# ---------------------------------------------------------------- P1（附录 11–26 行逐字，中文向翻译）
P1 = """你是一个专业的 Suno 提示词翻译助手。请将输入内容翻译成【以简体中文为主】的 Suno 提示词。

${DN}

${LN}

Input: "${a}"

硬性规则：
1. 这是严格翻译任务，必须保留原始含义、情绪、风格描述和信息密度。
2. 不要新增信息，不要删减信息，不要改成另一种风格。
3. 保持原有逗号分隔/短句结构，输出只保留处理后的单段字符串。
4. 中文结果必须尽量使用自然简体中文。
5. 只有标准音乐记号允许保留英文：调性/调式（如 F# Minor、C Major、Dorian）与 BPM（如 144 BPM）。
6. 除上述白名单外，风格、人声、乐器、编曲、节奏、情绪、音色等英文词汇必须翻译成中文。
7. 不要输出任何解释、标题或引号。"""

# 英文向翻译 E（02 §4.13 逐字；目标语言固定 English，${ON} 运行时为空）
P1_EN = """You are a professional music translator. Translate the following Suno AI music prompt to English.

${ON}

Input: "${a}"

CRITICAL INSTRUCTIONS:
1. This is a STRICT TRANSLATION task.
2. The output must have EXACTLY the same meaning, mood, and style descriptors as the input.
3. Do NOT add new tags. Do NOT remove existing tags.
4. Translate style terms precisely (e.g., "Upbeat Pop" <-> "欢快流行").
5. Keep the original comma-separated structure.
6. Output ONLY the translated string.
7. The output MUST contain NO Chinese characters (no CJK).
8. If translating into English, the final result MUST be no more than ${qi} characters (same as the app hard limit)."""

# chat_json 强制 json_object，而 P1/E 原文要求纯字符串 → 追加本项目输出约束（不改原文）
JSON_OUTPUT_NOTE = '\n\n【本项目输出约束】只返回纯净 JSON：{"text": "<翻译结果>"}。'

# ---------------------------------------------------------------- s3（02 §4.4 逐字，P20 第 6 条）
S3_RUBRIC = """**歌词质量评估与优化准则 (Lyric Quality Rubric)**：
1. **主题一致性**：全曲必须围绕一个核心主题/情绪展开，避免段落间主题漂移。
2. **段落职责**：主歌叙事铺陈、副歌情绪释放与记忆点、桥段转折升华，各司其职。
3. **记忆点**：副歌必须有可传唱、可识别的核心句，不能只是重复空话。
4. **可唱性**：句式方整（4/8 句）、押韵有节奏呼应、字词便于填入音乐小节。
5. **语言自然**：避免空泛堆词、避免音乐术语入词、避免翻译腔。
6. **结构完整**：自始至终完整，严禁虎头蛇尾或零散片段。"""

# ---------------------------------------------------------------- P6–P9（附录逐字）
P6 = """**写作优先级规则 (MANDATORY PRIORITY RULES)**：
  1. **第一优先级永远是用户的创作需求**。你必须先理解用户想写什么，再动笔写歌词。
  2. 你必须优先依据【用户输入内容】中明确给出的主题、情绪、人物关系、故事方向、场景和表达诉求进行创作。
  3. 如果【用户输入内容】已经足够完整，就**不要过度借用**对标歌曲 DNA，避免把歌词写成对标歌曲的复刻或变体。
  4. 只有当【用户输入内容】过短、过泛、信息明显不足时，才允许**适度参考**【音乐 DNA 背景】中用户已经选中的标签来补足表达方向。
  5. 即使参考 DNA，也只能作为辅助，不可喧宾夺主；歌词的核心表达必须始终服务于用户的创作需求。"""

P7 = """**DNA 使用规则 (DNA USAGE RULES)**：
  1. 仅允许参考【用户已经选中的音乐 DNA 标签】。
  2. 凡是未选中的 DNA 字段，一律视为未指定，**严禁擅自补充或捏造**。
  3. DNA 对歌词的作用主要是辅助确定氛围、语气、节奏感、段落能量与少量意象方向，不可直接替代歌词主题本身。
  4. 如果用户创作需求已经明确，DNA 只能做轻度修饰，不可覆盖或带偏用户原始主题。
  5. 如果用户创作需求明显不足，你可以结合已选中的风格、情绪、配器、旋律走向、编曲逻辑等信息，让歌词更完整、更像一首真实歌曲。
  6. **重要禁令**：当用户创作需求或参考内容不足时，禁止把音乐分析术语直接写进歌词正文，例如 “C大调 / 升F小调 / 120BPM / 低音区 / 中高音区 / 假声 / 混声 / 副歌抬升 / 四四拍 / 和弦进行” 这类术语只能作为内部参考，必须转化为自然的人类歌词表达，严禁生硬出现在歌词句子里。"""

P8 = """**元标签生成规则 (META-TAG RULES)**：
  1. 元标签只能根据【用户已选中的音乐 DNA】与【DNA补充要求】生成，禁止参考【Suno Prompt】或【用户主题叙事内容】去补造新的音乐信息。
  2. 元标签中的情绪、配器、力度、氛围、人声唱法描述，必须来源于已选中的 DNA 信息或 DNA补充要求，不可凭空加入未选中的风格、乐器、节奏、制作特征或唱法。
  3. 如果某类 DNA 没有被用户选中，且 DNA补充要求也没有明确提出，就不要在元标签中主动加入对应信息。
  4. 元标签的作用是帮助识别段落气质与演唱/编曲状态，不是替代歌词正文。
  5. 元标签必须优先写具体的【编曲 + 人声唱法】；若两者都已提供或可从 DNA补充要求中明确获得，应尽量同时体现。若只明确提供其中一类，就只写那一类，禁止脑补另一类。
  6. 标签必须具体但不能冗长，准确反映段落的情绪或音乐状态。"""

P9 = """根据用户的主题创作完整的歌词，并严格遵守“创作需求优先、DNA 辅助补充”的原则。
       **特别强调 (Critical for New Lyrics)**:
       1. **语言默认规则**: **默认生成中文歌词**。仅在用户输入中明确包含“英文歌词”、“English lyrics”等指令时，才生成英文歌词。
       2. **先理解再写作**: 先提炼这首歌到底在写什么、是谁在说、想表达什么情绪与关系，再开始写歌词。
       3. **结构必须完整**: 务必包含标准流行歌曲结构 (Intro -> Verse -> Chorus -> Verse -> Chorus -> Bridge -> Chorus -> Outro)，必要时补充 Pre-Chorus 或 Bridge。
       4. **内容必须真实可唱**: 严禁生成零散片段、空泛抒情、口号堆砌或只有氛围没有内容推进的歌词。每个段落必须内容充实、逻辑连贯、情感统一。
       5. **副歌必须有记忆点**: 副歌必须具备可传唱、可识别、可回忆的核心句，不可只是重复空话。
       6. **段落职责清晰**: 主歌负责叙事与铺陈，副歌负责情绪释放与核心表达，桥段负责转折、升华或新的理解。
       7. **输出要求**: 只输出最终中文歌词正文及中文结构标签，不要输出解释。"""

# ---------------------------------------------------------------- P10（02 §4.2 完整模板逐字）
_INSTRUMENTAL_BLOCK = """    **纯器乐模式 (INSTRUMENTAL MODE)**：
    - 本曲为纯器乐/纯编曲，**严禁**在 sunoPrompt 中出现人声、演唱、vocal、singing、lead vocal、backing vocals 等描述。
    - 仅描述配器、节奏、风格、情绪、编曲逻辑等器乐元素。"""

_MUST_VOCAL = "人声（人声风格、人声角色、人声能量）"
_MUST_INSTRUMENTAL = "主奏乐器音色、器乐织体层次"

P10_TEMPLATE = """你是专业的 Suno AI 音乐提示词工程师，严格按照以下规则，将用户的音乐创作需求，转换成符合 Suno v5最佳实践的标准提示词。你的当前任务只有一项：生成 Suno AI 提示词。**严禁生成歌词**。

  音乐 DNA 背景 (用户选中的核心特征): ${S}
  用户输入内容: ${C}

  任务 1. sunoPrompt: 生成 Suno AI 提示词。
     **请严格按照以下规则生成 (Strictly follow these rules):**
     **sunoPrompt 专用输入 (SUNO PROMPT ONLY INPUT)**：
     ${F}

     **生成依据 (Generation Basis - CRITICAL)**：
     ${U}
${instrumental_block}

    **锁定事实清单 (LOCKED SUNO FACTS - ABSOLUTE SOURCE OF TRUTH, EN ONLY)**：
    ${locked_facts}

     ### 核心规则
     1. 输出格式：前半段为 **逗号分隔的结构化关键词短语**，后半段为 **自然语言氛围描述**，两者用 "//" 分隔。
     2. 结构化关键词段（前半段，核心控制）：
        - 必须包含：曲风类型、速度、调性、核心配器、情绪，${must_include}，混音整体风格，和弦走向，节奏型。
        - 使用 Suno 可识别的标准术语，简洁精准，禁止长句。
        - 这部分用来精准锁定音乐的核心属性，精准适配 Suno v5 的标签识别逻辑。
     3. 自然语言描述段（后半段）：
        - 用来补充旋律走向，编曲逻辑，混音细节等。
        - 简短清晰，避免过长干扰核心参数。
     4. 双语输出约束：在 sunoPrompt 对象中填写两个字符串字段，en 写完整英文提示词，zh 写完整中文提示词。
     5. 英文长度约束：英文提示词目标长度 ≤${bN} 字符，硬上限 ${qi} 字符；超长时优先压缩修饰词，严禁丢失锁定事实。

    **关键一致性指令 (CRITICAL CONSISTENCY INSTRUCTION)**:
    - 中英文提示词必须语义严格等价、信息密度一致、结构尽量一致。
    - **严禁**在英文版中遗漏锁定事实清单里的核心内容。
    - 若存在【锁定事实清单】，则英文 sunoPrompt 必须完整覆盖清单中的全部核心事实，同时遵守“结构化关键词 + 自然描述”的输出格式。
    - 中文版必须完整映射英文版含义，不得删减。
    - 允许重组语序、合并句子、融合表达、适当缩短修饰语，但**严禁**遗漏锁定事实中的核心内容、篡改事实含义、或新增锁定事实中不存在的音乐信息。

  输出格式：纯净 JSON，不带 Markdown 标记。
  按以下结构填写，将两个占位值替换为本次实际的提示词：
  {"sunoPrompt": {"en": "<完整英文提示词>", "zh": "<完整中文提示词>"}}"""

# ---------------------------------------------------------------- P20（附录 428–452 行逐字；s=False 描述性标签分支）
# 本项目无“简单标签模式”，固定 s=False；n 由调用语言决定（zh/en）。
_TAG_RULE_ZH = "必须使用带有描述性的中文结构标签，例如 [主歌: 克制钢琴铺陈] 或 [副歌: 情绪全面释放]。严禁使用单一的 [主歌] 或 [Verse]。"
_TAG_RULE_EN = "You must use descriptive English structure tags, such as [Verse: Restrained Piano] or [Chorus: Full Emotional Release]. Do NOT use simple [Verse] tags."
_TAG_REQ = "结构标签必须具体，能够体现情绪、配器、力度或氛围，但不能冗长，也不能喧宾夺主。"

P20_TEMPLATE = """你是一名专业作词人。根据用户的主题或原始内容，在指定重写力度内生成结构完整、表达自然、适合演唱的歌名和歌词。

【占位符保护】若原始内容中含 «P数字»，必须原样保留，严禁修改或删除。

  创作主题/原始内容: "${c}"
  音乐风格 (Suno Prompt，仅供内部参考气质与情绪，严禁将其中的术语/参数直接写入歌词正文): "${t}"
  ${g}

  要求：
  1. **创作需求第一优先级**：你必须优先根据“创作主题/原始内容”来写，先判断这首歌在写什么、是谁在说、想表达什么情绪与关系变化。
  2. **风格信息只作为辅助约束**：Suno Prompt 用于帮助你控制音乐气质、节奏感、语气与段落能量，但不可覆盖原始主题。
  3. **必须输出完整歌词**：需包含主歌、副歌等核心段落，必要时补充桥段、导歌或过渡段，整体逻辑连贯、情感统一，避免出现零散片段或结构缺失。
  4. **段落职责必须清晰**：主歌负责叙事与铺陈，副歌负责情绪释放与核心表达，桥段负责转折、升华或新的理解。
  5. **副歌必须有记忆点**：必须包含可传唱、可识别、可回忆的核心句，不能只是重复空话。
  6. ${s3}
  6.1 **【无条件禁令】**：严禁把任何音乐分析术语、制作参数直接写进歌词正文，例如 “BPM / 速度数字 / C大调 / 升F小调 / 调性名 / 低音区 / 中高音区 / 假声 / 混声 / 四四拍 / 和弦进行 / reverb / delay”。这些术语只能作为内部参考来把握情绪与气质，必须转化为自然、可唱的歌词表达。
  7. **结构与排版严控**：严格把控段落结构，做到排布合理、层次清晰。每个段落必须表意完整，契合主歌副歌常规排版逻辑，严禁混乱分段。**全曲篇幅完整**：必须输出自始至终的完整歌词，涵盖主歌、导歌、副歌、桥段、尾段等标准段落，严禁虎头蛇尾或零散片段。**句式与节拍**：段落内部优先使用 4 句或 8 句等方整结构，句式与押韵需有节奏呼应，避免明显缺句导致的节拍悬空。**副歌严谨**：副歌作为核心记忆点，结构需对称闭合，字词排布便于完整填入音乐小节。
  8. **歌词正文语言**：默认为简体中文。只有当用户在输入中明确要求创作英文歌词时才使用英文。
  9. **结构标签语言**：${tag_rule}
  10. **标签要求**：${tag_req}
  11. 同时生成一个与最终歌词核心主题、记忆点或关键意象相符的歌名；歌名与歌词使用同一种主要语言，简洁自然，不得使用占位名称，不得添加书名号、引号、换行或解释。
  12. **重要：结构标签内部必须去除多余空格。**
  13. **重要：必须确保所有标签闭合。**
  14. **和声格式**：括号内的和声/伴唱内容直接写歌词，严禁出现「和声：」或「和声:」前缀。严禁在括号内添加英文翻译，括号内容必须与主歌词同语言。正确：(深爱的... 拥抱...)；错误：(和声: 深爱的... 拥抱...)、(Meeting you)。
  15. **CRITICAL: Ensure you generate the FULL lyrics from beginning to end. Do not summarize or stop early.**
  16. 只返回纯净 JSON，不带 Markdown。字段为 songTitle 和 lyrics，两个字段均为字符串。"""

# 仅有 DNA、无主题时的创作 brief（项目编写）
DNA_ONLY_BRIEF = (
    "请基于【音乐 DNA 背景】创作一首全新的原创歌曲：主题自定，贴合参考曲风与情绪，"
    "但歌词内容必须原创，不得复刻参考歌曲的歌词或具体叙事。"
)

# 歌词 zh→en 严格翻译（项目编写；原作 §6.3 为独立 zh/en 两次创作，本项目改为创作+翻译，见报告）
# ponytail: 不复刻原作 xN/CN ×3 纯度循环，一次修复重试后仍失败即报错
LYRICS_TRANSLATE_EN = """You are a professional lyric translator. Translate the following Chinese song (title and lyrics) into English.

Input (JSON):
${input}

CRITICAL INSTRUCTIONS:
1. This is a STRICT TRANSLATION task. Keep EXACTLY the same meaning, mood, imagery and narrative.
2. Translate every lyric line. Keep the same section order and translate descriptive section tags to English (e.g. [主歌: 克制钢琴铺陈] -> [Verse: Restrained Piano]).
3. Do NOT add or remove lines, sections, or backing-vocal parentheses content.
4. The output MUST contain NO Chinese characters (no CJK).
5. Keep it singable: natural English word order and reasonable line lengths.
6. Return pure JSON only, no Markdown: {"songTitle": "<English title>", "lyrics": "<full English lyrics>"}"""


# ---------------------------------------------------------------- 构建器
def build_dna_messages(mode: str) -> list[dict]:
    """P3 主提示词。mode 不改变 P3：三种模式都先做完整 DNA 分析（SPEC §4.8 结果表）。"""
    content = (
        P3_TEXT
        + "\n"
        + json.dumps(P3_EXAMPLE, ensure_ascii=False, indent=2)
        + "\n\n"
        + PROJECT_AUDIO_CLAUSE
    )
    return [{"role": "user", "content": content}]


def rhythm_evidence_block(local: dict) -> str:
    """本地节奏证据注入（02 §7.2 模板 + §7.3 块，逐字片段；数值来自 local_dims）。"""
    f = local["timeSignature"]["en"]
    beats = f.split("/")[0]
    d = float(local.get("onset_density", 0.0))
    u = float(local.get("tempo_stability", 0.0))
    s = float(local.get("syncopation", 0.0))
    zh = f"本地节奏指标：拍号 {f}；每秒起音密度 {d:.2f}；节拍稳定度 {u:.2f}；切分倾向 {s:.2f}。请据此映射简洁 rhythmPattern 标签。"
    en = (
        f"Local rhythm metrics: meter {f}; onset density {d:.2f} per second; "
        f"tempo stability {u:.2f}; syncopation tendency {s:.2f}. Map to a concise rhythmPattern label."
    )
    return (
        "\n\n**本地节奏客观指标（优先用于 rhythmPattern 映射）**：\n"
        f"- 拍号：{f}\n"
        f"- 小节循环：{beats} 拍\n"
        "- 强拍规律：每小节第 1 拍为强拍\n"
        f"- 节奏密度：每秒起音 {d:.2f} 次\n"
        f"{zh}\n{en}\n"
        "rhythmPattern 必须以本地拍号开头。"
    )


def _locked_facts(profile: dict | None) -> str:
    """O.en：EN-only 锁定事实清单（02 §4.2；数组 join，本地 3 维一并锁定）。"""
    if not profile:
        return "{}"
    facts = {}
    for k, v in profile.items():
        if isinstance(v, dict) and "en" in v:
            en = v["en"]
            facts[k] = ", ".join(en) if isinstance(en, list) else en
        else:
            facts[k] = v
    return json.dumps(facts, ensure_ascii=False, indent=2)


def build_generate_messages(mode: str, theme: str | None = None, dna: dict | None = None) -> list[dict]:
    """P10 Suno 提示词（full/style 模式的第一步）。dna 传 dnaProfile dict 或 None。"""
    profile = dna if isinstance(dna, dict) and dna else None
    instrumental = bool(profile) and str(profile.get("vocalStyle", {}).get("en", "")).startswith("No lead vocals")
    s = json.dumps(profile, ensure_ascii=False) if profile else "未提供参考曲目"
    c = theme or (
        "（未提供主题：请基于音乐 DNA 背景，为一首全新原创歌曲生成风格提示词；"
        "主题自定，贴合参考曲风与情绪，不得复刻参考歌曲。）"
    )
    # ponytail: F/U 为项目运行时组装（原作插值语义未逐字取证）
    f = "参考曲目 DNA 已作为风格事实提供，请完整覆盖锁定事实清单。" if profile else "本次无参考曲目，依据用户输入内容生成。"
    u = (
        "以【用户输入内容】为创作需求第一优先级；【音乐 DNA 背景】仅作风格参考，不可覆盖用户主题。"
        if theme
        else "以【音乐 DNA 背景】为风格依据，生成全新原创歌曲的风格提示词。"
    )
    content = _sub(
        P10_TEMPLATE,
        {
            "S": s,
            "C": c,
            "F": f,
            "U": u,
            "instrumental_block": _INSTRUMENTAL_BLOCK if instrumental else "",
            "locked_facts": _locked_facts(profile),
            "must_include": _MUST_INSTRUMENTAL if instrumental else _MUST_VOCAL,
            "bN": str(B_N),
            "qi": str(QI),
        },
    )
    return [{"role": "user", "content": content}]


def build_lyrics_messages(theme: str | None, suno_en: str = "", dna: dict | None = None) -> list[dict]:
    """P20 作词（中文创作）。${t}=sunoPrompt.en（lyrics 模式为空）；${g}=P6+P8+P9，有 DNA 时追加 P7+快照。"""
    g = P6 + "\n\n" + P8 + "\n\n" + P9
    if dna:
        g += "\n\n" + P7 + "\n\n【音乐 DNA 背景】\n" + json.dumps(dna, ensure_ascii=False)
    content = _sub(
        P20_TEMPLATE,
        {
            "c": theme or DNA_ONLY_BRIEF,
            "t": suno_en or "",
            "g": g,
            "s3": S3_RUBRIC,
            "tag_rule": _TAG_RULE_ZH,
            "tag_req": _TAG_REQ,
        },
    )
    return [{"role": "user", "content": content}]


def build_lyrics_translate_messages(zh_obj: dict) -> list[dict]:
    return [{"role": "user", "content": _sub(LYRICS_TRANSLATE_EN, {"input": json.dumps(zh_obj, ensure_ascii=False)})}]


def build_p1_repair_messages(en_text: str) -> list[dict]:
    """sunoPrompt 中文纯度修复：P1 英→中（DN/LN 运行时为空）。"""
    return [{"role": "user", "content": _sub(P1, {"DN": "", "LN": "", "a": en_text}) + JSON_OUTPUT_NOTE}]


def build_e_repair_messages(zh_text: str) -> list[dict]:
    """sunoPrompt 英文纯度修复：E 中→英。"""
    return [{"role": "user", "content": _sub(P1_EN, {"ON": "", "a": zh_text, "qi": str(QI)}) + JSON_OUTPUT_NOTE}]
