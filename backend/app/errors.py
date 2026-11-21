"""统一错误：AppError(code, 中文 message)。HTTP→{"detail":{"code","message"}}；任务内→error_code/error_msg。"""
from __future__ import annotations

DEFAULT_MESSAGES = {
    "NOT_LOGGED_IN": "未登录 Suno。请先在顶栏点「去登录」完成手动登录。",
    "BUSY": "当前有任务在排队或执行，请等它完成后再提交。",
    "INVALID_URL": "链接无效：仅支持 https://suno.com 或 https://www.suno.com 的歌曲链接。",
    "BAD_AUDIO": "音频无法读取或时长过短，请换一个常见格式（MP3/WAV/FLAC/M4A/OGG）的文件。",
    "NO_AI_KEY": "未配置 AI 密钥。请到「设置」页填写对应 Provider 的 API Key。",
    "INVALID_PROVIDER": "不支持的 Provider，仅支持 deepseek / qwen。",
    "INVALID_GENERATION_INPUT": "生成输入无效：请填写主题，或选择一个已完成且含 DNA 档案的分析任务。",
    "UNSUPPORTED_FORMAT": "不支持的文件格式。",
    "KEY_DB_UNAVAILABLE": "需要酷狗本地密钥库但不可用：请先用酷狗客户端下载过这首歌，并完全关闭酷狗后重试。",
    "DECRYPT_FAILED": "解密失败：文件可能损坏或版本暂不支持。",
    "NOT_FOUND": "任务不存在。",
    "OUTPUT_UNAVAILABLE": "产物文件已不存在或被移动，无法打开所在文件夹。",
    "OPEN_FOLDER_FAILED": "打开文件夹失败，请手动前往下载目录查看。",
    "DIR_INVALID": "目录无效或不可写，请换一个目录。",
    "CHROME_NOT_FOUND": "未找到 Chrome。请安装 Google Chrome 后重试。",
    "PORT_TIMEOUT": "Chrome 调试端口 15 秒内未就绪，请关闭残留 Chrome 窗口后重试。",
    "PORT_IN_USE": "9222 端口被其他程序或其他 Chrome 配置占用，请关闭后重试。",
    "PAGE_OFF_SITE": "工作页已离开 Suno 站点，任务中止。请重新登录后重试。",
    "CANCELLED": "任务已取消。",
    "NETWORK_RESULT_UNKNOWN": "请求已发出但结果不确定。为避免重复消耗，不会自动重发，请用「继续查询」或核对后再试。",
    "GENERATION_UNCERTAIN": "生成请求结果待确认：请先在 Suno 核对是否已产生分轨，再决定是否重试。",
    "GENERATION_FAILED": "Suno 分轨生成未成功。",
    "SONG_SYNC_TIMEOUT": "歌曲同步超时，可在剩余时间内重试同步。",
    "RENDER_TIMEOUT": "Suno 渲染超时。可用「继续查询」跟踪同一渲染任务。",
    "QUEUED_TIMEOUT": "Suno 渲染仍在排队，本次查询超时。可用「继续查询」跟踪同一渲染任务。",
    "RENDER_ERROR": "Suno 渲染失败。",
    "AUDIO_SILENT": "渲染结果为静音，已判定失败。",
    "AUDIO_TOO_LARGE": "参考音频过大：压缩后仍超出 Qwen 10MB 限制，请缩短音频后重试。",
    "AI_AUTH_FAILED": "AI 密钥无效或无权限（401/403），请到「设置」检查 Key 与 base_url 是否同区域。",
    "AI_MODEL_UNAVAILABLE": "模型不可用（不存在或无权限），请到「设置」检查模型名。",
    "AI_RESULT_INVALID": "AI 返回结果缺字段或类型错误，请重试。",
    "RATE_LIMITED": "Suno 限流（429），请稍后重试。",
    "DOWNLOAD_FAILED": "下载失败，可重试。",
    "MANGO_DECRYPT_FAILED": "音频解密失败（详见日志）。",
    "VERIFICATION_REQUIRED": "请在 Chrome 窗口完成 Suno 人机验证后重试。",
    "INSUFFICIENT_CREDITS": "Suno 账号积分/额度不足。",
    "AUDIO_TOO_SHORT": "音频太短，请换一个文件。",
    "EXPORT_RESUME_UNAVAILABLE": "此任务没有待查询的渲染。",
    "SUNO_HTTP_ERROR": "Suno 接口返回错误，请查看任务日志。",
    "NETWORK_ERROR": "网络请求失败，请检查网络后重试。",
    "TIMEOUT": "处理超时，请重试。",
    "INTERNAL_ERROR": "内部错误，请查看日志后重试。",
    "INVALID_INPUT": "请求参数无效。",
    "NOT_IMPLEMENTED": "模块尚未完成。",
}


class AppError(Exception):
    def __init__(self, code: str, message: str = "", status: int = 0):
        self.code = code
        self.message = message or DEFAULT_MESSAGES.get(code, code)
        self.status = status  # 上游 HTTP 状态（如有）
        super().__init__(f"{code}: {self.message}")


class SunoAPIError(AppError):
    """页内请求失败；status=上游 HTTP 状态，0=传输层。handler 据此决定重试/不确定语义。"""

    def __init__(self, code: str, status: int = 0, detail: str = ""):
        msg = DEFAULT_MESSAGES.get(code, code)
        if detail:
            msg = f"{msg}（{detail}）"
        super().__init__(code, msg, status)