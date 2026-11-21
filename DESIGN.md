---
version: alpha
name: TIMBRE Web 音乐工作台
description: Windows 本机使用的中文 Web 工作台，采用近黑深蓝表面、青色反馈和靛蓝操作色，支持上传、下载、DNA 创作、格式转换和设置。
colors:
  primary: "#2B6BCB"
  primary-hover: "#285FB7"
  primary-pressed: "#234F9D"
  on-primary: "#FFFFFF"
  accent: "#22D3EE"
  indigo: "#6366F1"
  violet: "#8B5CF6"
  canvas: "#020617"
  surface: "#0F172A"
  surface-raised: "#111C33"
  surface-active: "#142942"
  border-subtle: "#29364D"
  border-control: "#64748B"
  text: "#F1F5F9"
  text-muted: "#CBD5E1"
  text-disabled: "#94A3B8"
  success: "#10B981"
  warning: "#FBBF24"
  danger: "#F87171"
typography:
  page-title:
    fontFamily: "'Plus Jakarta Sans', 'Microsoft YaHei', 'PingFang SC', system-ui, sans-serif"
    fontSize: 24px
    fontWeight: 700
    lineHeight: 1.25
  section-title:
    fontFamily: "'Plus Jakarta Sans', 'Microsoft YaHei', 'PingFang SC', system-ui, sans-serif"
    fontSize: 16px
    fontWeight: 600
    lineHeight: 1.5
  body:
    fontFamily: "'Plus Jakarta Sans', 'Microsoft YaHei', 'PingFang SC', system-ui, sans-serif"
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "'Plus Jakarta Sans', 'Microsoft YaHei', 'PingFang SC', system-ui, sans-serif"
    fontSize: 14px
    fontWeight: 600
    lineHeight: 1.5
  caption:
    fontFamily: "'Plus Jakarta Sans', 'Microsoft YaHei', 'PingFang SC', system-ui, sans-serif"
    fontSize: 12px
    fontWeight: 500
    lineHeight: 1.5
  log:
    fontFamily: "ui-monospace, Consolas, 'Microsoft YaHei', monospace"
    fontSize: 13px
    fontWeight: 400
    lineHeight: 1.6
rounded:
  control: 8px
  panel: 12px
  dialog: 16px
  pill: 9999px
spacing:
  xxs: 4px
  xs: 8px
  sm: 12px
  md: 16px
  lg: 24px
  page: 28px
  xl: 32px
  xxl: 48px
  control-height: 40px
  compact-height: 32px
  touch-target: 44px
  topbar-height: 56px
  task-row-height: 48px
  content-max: 1440px
  drawer-width: 480px
components:
  app-shell:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
  topbar:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.text}"
    height: "{spacing.topbar-height}"
  tab-active:
    backgroundColor: "{colors.surface-active}"
    textColor: "{colors.accent}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    height: "{spacing.control-height}"
    padding: "{spacing.sm}"
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
    textColor: "{colors.on-primary}"
  button-primary-pressed:
    backgroundColor: "{colors.primary-pressed}"
    textColor: "{colors.on-primary}"
  button-secondary:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.text}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    height: "{spacing.control-height}"
    padding: "{spacing.sm}"
  button-danger:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.danger}"
  button-disabled:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.text-disabled}"
  field:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    height: "{spacing.control-height}"
    padding: "{spacing.sm}"
  panel:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.panel}"
    padding: "{spacing.md}"
  panel-divider:
    backgroundColor: "{colors.border-subtle}"
    height: 1px
  field-outline:
    backgroundColor: "{colors.border-control}"
    size: 1px
  task-row:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    height: "{spacing.task-row-height}"
  status-neutral:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.text-muted}"
    typography: "{typography.caption}"
    rounded: "{rounded.pill}"
  status-active:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.accent}"
  status-success:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.success}"
  status-warning:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.warning}"
  status-error:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.danger}"
  progress-fill:
    backgroundColor: "{colors.accent}"
    height: 4px
    rounded: "{rounded.pill}"
  progress-gradient-mid:
    backgroundColor: "{colors.indigo}"
  progress-gradient-end:
    backgroundColor: "{colors.violet}"
  log-panel:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.text-muted}"
    typography: "{typography.log}"
    rounded: "{rounded.control}"
    padding: "{spacing.sm}"
  drawer:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.text}"
    width: "{spacing.drawer-width}"
    padding: "{spacing.lg}"
  dialog:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.text}"
    rounded: "{rounded.dialog}"
    padding: "{spacing.lg}"
---

# TIMBRE Web 设计系统

## Overview

面向 Windows 桌面浏览器的本机音乐工作台。网页承担任务操作与结果展示，受控 Chrome 窗口承担 Suno 手动登录与会话。应用配色依据 [GROW MUSIC 原软件视觉记录](UI-DESIGN.md) §2，间距刻度、控件尺寸与状态映射的组织方式参考 `ContractManagement/DESIGN.md`。应用界面采用上方导航、实体深色面板和紧凑任务列表，保留音乐工具的青靛紫色彩。功能边界及任务语义以 [SPEC.md](SPEC.md) 为准，本文负责网页视觉和交互呈现。

## Colors

- `{colors.canvas}`、`{colors.surface}`、`{colors.surface-raised}` 依次区分页面、工作区和浮层。常设面板以轻描边分组，内容区使用稳定的深色底。
- 主要按钮使用 GROW MUSIC 原主按钮兜底蓝 `{colors.primary}` 配白字。`{colors.accent}` 用于当前导航、链接、焦点和进行中。青 → 靛 → 紫渐变仅用于进度填充，可回退为青色实色。
- 状态用文字和颜色共同表达。等待与已取消为中性色，进行中为青色，完成和已登录为绿，待确认及需要用户操作为黄，失败及未登录为红。状态徽章使用深底和有色文字。
- 普通文字对比度至少 4.5:1。输入边界、焦点指示和有意义的图形状态与相邻背景至少 3:1。`{colors.border-subtle}` 用于装饰性分隔，输入边界使用 `{colors.border-control}`。

## Typography

- 页面标题使用 `{typography.page-title}`，区块标题使用 `{typography.section-title}`。正文、表单和操作文案采用 14px。12px 用于辅助说明、文件格式和状态标签。
- 优先本地提供 Plus Jakarta Sans，资源不可用时使用系统字体。Windows 中文回退到微软雅黑。页面加载无需外部字体 CDN。
- 百分比、时长、字节数和队列计数使用等宽数字。日志采用 `{typography.log}` 并允许长链接换行。歌词保留原有换行，长标签在窄窗允许折行。

## Layout

- 顶栏基准高 `{spacing.topbar-height}`。左侧为应用名，中间按「上传、下载、音乐 DNA、格式转换、设置」排列页签，右侧常驻 Suno 登录状态。页签在单一 Web 应用内切换，任务执行期间仍可浏览其他页面。
- 内容区最大宽 `{spacing.content-max}`，水平居中。宽桌面左右边距 `{spacing.page}`，顶部留白 `{spacing.lg}`。以 `{spacing.md}` 分隔区块，卡内通用间距 `{spacing.sm}`。同一页面共用一条左对齐线。
- 视口宽度 ≥1200px 时，上传、下载和转换页以左侧输入区、右侧任务区并排为主，列间距 `{spacing.lg}`，任务列至少 360px。DNA 页左侧放输入，右侧放结果。设置以单列分组为主。
- 800–1199px 时主内容改为单列，输入在前，任务或结果在后，边距 `{spacing.md}`。小于 800px 时顶栏分两行，应用名与登录态在上，页签在下。页签区域可独立横向滚动并让当前页签可见，页面正文保持纵向滚动。队列抽屉在窄窗占满可用宽度。
- 桌面优先检查 1280px 和 1440px 两档，再检查窄窗与浏览器 200% 缩放。长文件名和链接使用可换行或可查看全文的设计。浮层超高时只滚动浮层主体，标题和关闭按钮始终可访问。

## Elevation & Depth

常设工作区用面板色和细描边表达层级。仅弹窗、菜单和抽屉使用明显投影，浮层叠在遮罩上。青靛紫色用于有限的界面高亮，日志、表单和文字区域保持稳定背景。

## Shapes

按钮、输入、页签统一 `{rounded.control}`。工作面板使用 `{rounded.panel}`，确认弹窗使用 `{rounded.dialog}`。全圆角留给状态徽章和进度条。卡片内的分组可用细线或标题区隔。

## Motion & Micro-interaction

控件状态切换使用 `160ms ease-out`。抽屉和对话框使用 `200ms ease-out`，内容定位到位后即可操作。尊重 `prefers-reduced-motion`，偏好减少动画时取消位移、呼吸和循环进度动效，保留静态阶段与状态说明。

## Voice & Content Strategy

- 使用简短中文动词描述下一步，例如「选择音频」「加入下载队列」「打开所在文件夹」「继续查询」。Suno、Chrome、MP3、WAV、DNA 保留原术语。
- 任务失败时先说明用户能判断的原因，再给可执行操作。提交结果未确认时提示核对已有结果。已有渲染 ID 的超时用「继续查询」，指向同一个远端结果。
- 进度未知时显示阶段，例如「等待 Suno 渲染」。有可靠的字节数、片数或百分比时显示实际值。后台错误码及技术诊断放入任务详情。

## Components

### 全局组件

| 组件 | 网页行为 |
|---|---|
| 页签与登录状态 | `topbar` 与 `tab-active` 共用。登录态显示「去登录」「等待手动登录」「正在确认」或账号。点击「去登录」打开受控 Chrome，网页保留登录进度。 |
| 按钮 | 默认高 `{spacing.control-height}`，任务行内可以使用 `{spacing.compact-height}`。同一任务区只有一个主要动作。次要操作使用浅描边，取消类操作用红字及明确名称。图标按钮提供可访问名称，触控布局命中区至少 `{spacing.touch-target}`。 |
| 字段与上传区 | 输入、选择器和文件选择共用 `field` 样式。字段标题在上，帮助与错误在下，错误和字段关联。拖放区同时提供可键盘操作的「选择文件」。 |
| 任务行与徽章 | `task-row` 高度为最小值，长内容允许增高。顺序为名称、阶段或进度、文字徽章、当前可用操作。后端状态映射为「等待中、进行中、已完成、失败、已取消」。待确认作为任务结果提示显示。 |
| 日志 | `log-panel` 默认跟随尾部。用户向上查看时暂停自动滚动，显示「跳到最新」。切页或重连后按日志序号补齐并去重。读屏只播报任务状态变化及关键错误。 |
| 抽屉与确认弹窗 | 队列从右侧打开。模态层支持 Escape、焦点限制和关闭后焦点归还。仅对会丢失进度的取消操作二次确认。 |

### 页面与结果

| 页面 | 输入与操作 | 状态与结果 |
|---|---|---|
| 上传 | 本地音频选择或拖入，提交后显示变形、上传、还原、渲染、导出。 | 展示步骤及日志，成功提供成品链接。渲染结果待确认且已有 ID 时提供「继续查询」。提交结果不明时提示核对 Suno 中的结果。 |
| 下载 | 分享链接、MP3/WAV、歌曲/分轨/人声与伴奏。批量链接逐条入队。 | 展示后端队列的顺序和各项进度，等待项与运行项可取消。完成后提供「打开所在文件夹」，分轨显示 ZIP 产物。队列抽屉提示「应用退出后，等待任务将取消」。 |
| 音乐 DNA | 「分析参考音频」和「生成」两组操作。分析接收参考音频，生成接收主题或已完成的 DNA。两组均有完整、仅歌词、仅风格三个模式。 | 分析完整模式展示 DNA、歌词、Suno 提示词，分析仅风格展示 DNA 与提示词。生成完整模式展示歌词和提示词，生成仅风格展示提示词。仅歌词模式展示歌词。结果可逐项复制及导出 UTF-8 TXT。 |
| 格式转换 | 选择一个音频文件和 MP3/WAV 输出格式。 | 展示识别的源格式、转换状态和保存结果。仅当前输入确需酷狗密钥库时显示对应指引。KGG 标明部分支持。 |
| 设置 | 下载目录、AI Provider、模型、密钥、适用时的 API 地址及调试日志分组。 | 分组内就地显示保存结果或字段错误。密钥以遮蔽输入呈现，已配置时仅显示状态。 |

## Do's and Don'ts

- 组件复用 YAML token 和已有变体。新增颜色、字级或尺寸时先判断现有 token 是否适用，再统一补到定义处。
- 页面允许完整键盘操作。交互控件在 `:focus-visible` 状态使用至少 2px 的 `{colors.accent}` 焦点环。错误、进行中和禁用状态均带文字。窄窗保留核心操作及必要说明。
- 导航保留五个入口。登录从全局状态进入，批量队列从下载页进入。每个任务动作按 SPEC 的状态与错误规则展示，区分取消、重试、继续查询和打开产物目录。
- 页面空间优先给表单、队列、步骤和结果。避免重复标题、常驻装饰动画、多层内嵌卡片及纯装饰徽章。

格式依据 [Google Labs DESIGN.md 规范](https://github.com/google-labs-code/design.md/blob/main/docs/spec.md)。
