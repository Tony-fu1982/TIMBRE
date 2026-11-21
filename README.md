# TIMBRE

跑在自己机器上的本地音乐工作台：以 Suno 为中心，覆盖下载、加密格式转换、切片变形、
Studio 自动还原导出与音频 DNA 分析；AI 能力接 DeepSeek / 千问（DashScope）。

## 功能

- 单曲 / 批量下载、全分轨 ZIP、人声伴奏对
- 加密格式转换：NCM / QMC / KGM / KGMA / VPR / KGG / KWM
- 上传变形：把本地音频交给 Suno 重新演绎
- Studio 还原：自动完成修复与成品导出
- 音频 DNA：参考音频分析与主题生成（DeepSeek / 千问）
- 任务队列：并发控制、取消、日志、重启恢复
- 下载目录等设置可自定义

## 技术栈

- 后端：FastAPI + SQLite（WAL）+ httpx + Playwright（CDP 连真实 Chrome）+ ffmpeg
- 前端：React 19 + Vite + TypeScript
- AI：DeepSeek / 千问（DashScope 标准接口）

## 快速开始

依赖：Python 3.11+、Node 18+、Chrome、ffmpeg（加入 PATH）。

```bash
# 后端
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 前端（另开一个终端）
cd frontend
npm install
npm run build
```

打开 http://127.0.0.1:8000 ，在页面里完成一次 Suno 登录即可使用。

## 目录

- `backend/app` 后端服务与管线
- `backend/tests` 测试
- `frontend/src` 前端
- `docs/agents` 协作与契约文档

## 说明

- 运行时数据（数据库、密钥、登录态、临时文件）全部本机生成，不入库。
- 仅供个人学习与研究使用；请遵守平台条款与音乐版权。
