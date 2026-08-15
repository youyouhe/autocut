# AutoCut — 剪映 AI 自动化视频生产与渲染工作台

[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6.svg)](https://www.microsoft.com/)
[![React](https://img.shields.io/badge/react-19.0-61DAFB.svg)](https://react.dev/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**AutoCut** 是一个面向**剪映专业版（JianYing Pro / CapCut）**的端到端 AI 自动化视频生产工作台与无头渲染引擎。它突破了剪映没有官方 Headless 渲染 API 的限制，打通了“**素材跨端流转 → 多模态 AI 感知理解 → 智能编排与草稿构建 → Windows 多桌面后台隔离渲染 → 自动化质检**”的全流程闭环。

---

## 🌟 核心特性

- 🖥️ **Windows 隔离桌面真后台渲染（核心黑科技）**
  - 基于 Windows `CreateDesktop` API 与 Frida 进程 Hook，在独立的虚拟桌面（`JYRender_0`）中拉起剪映实例并自动化执行导出。
  - **完全不抢占前台鼠标、键盘与窗口焦点**，后台默默极速出片，不干扰日常办公娱乐。
- 👁️ **全模态 AI 视频感知与质检（Perceive 模块）**
  - 集成 **Qwen3.7-Plus 视觉大模型** 进行视频均匀抽帧理解、画面描述、精彩片段识别与情绪分析。
  - 内置 ASR 语音识别，快速提取视频口播文案与逐字时间戳（支持 `remote` 第三方 / `local` faster-whisper 离线两种后端）。
  - 利用 FFmpeg 进行镜头转场/场景分割检测，支持成片质量自动质检。
- ⚡ **声明式 YAML 视频模板引擎**
  - 支持通过 YAML 声明式配置视频时间轴、图文轨道、转场动画与音频背景，一键参数替换并批量渲染。
  - 内置多种预设模板（情感语录、产品介绍、知识短视频等）。
- 📲 **LocalSend 原生协议跨端素材接收**
  - 内置 LocalSend v2.2 UDP 广播与 HTTP 服务，局域网内的手机（iOS/Android）或电脑打开官方 LocalSend App 即可秒传素材至工作台，自动归档并（可选）自动触发 AI 分析。
- 🤖 **MCP (Model Context Protocol) 标准工具支持**
  - 完整实现 MCP Video Server（`mcp_video_server.py`），支持 Claude Code、Cursor、Agent 直接调用工具进行素材感知、草稿创建与自动出片。
- 📊 **渲染实时进度与任务持久化**
  - 渲染过程实时回传阶段（注入/打开/导出/渲染中）与导出字节数，前端进度条展示。
  - 任务历史落盘 SQLite（`tasks.db`），服务重启后结果可追溯。
- 🎨 **双模式交互工作台**
  - **现代化 React 19 SPA**：基于 Vite + Tailwind CSS，直接由后端 Flask（9002 端口）一体化托管。
  - **Gradio 快速原型台**（`gui.py`）：适合交互式调试与单步验证。

---

## 🏗️ 架构全景

```text
┌────────────────────────────────────────────────────────────────────────┐
│                        用户交互与接入层 (Frontends & MCP)                │
│  • React Web SPA (frontend-react)     • Gradio 工作台 (gui.py)         │
│  • LocalSend 局域网投送 (localsend_recv) • MCP Server (mcp_video_server) │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │ HTTP / SSE / MCP
┌──────────────────────────────────▼─────────────────────────────────────┐
│                       业务调度与能力层 (Core Services)                   │
│  • REST 服务核心 (render_server.py:9002) - 路由融合与任务管理           │
│  • 声明式模板引擎 (template_engine.py)   - YAML 驱动视频批量生成          │
│  • 视频感知分析 (perceive.py)            - Qwen VLM + ASR + 场景分割      │
│  • 内存状态缓存 (memory_store.py)        - 视频元数据与文件热缓存        │
│  • 草稿构建引擎 (VectCutAPI / pyJianYingDraft) - 轨道/文字/特效拼装    │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │ Win32 API / Frida Hook
┌──────────────────────────────────▼─────────────────────────────────────┐
│                    底层执行与无头渲染层 (Render Driver)                 │
│  • 独立桌面隔离 (CreateDesktop: JYRender_0)                            │
│  • 进程注入与事件驱动 (hook_focus.js / render_driver.py)               │
│  • 导出看门狗与文件监听 (render_monitor.py)                            │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 📁 目录结构

```text
autocut/
├── render_server.py        # 主服务 (Flask REST API + 融合 VectCut + 前端托管 :9002)
├── render_driver.py        # 剪映 UI 自动化驱动与渲染闭环 (Frida Hook + Win32)
├── render_monitor.py       # 导出监听与状态检测模块
├── hook_focus.js           # 注入剪映主进程的 Frida 脚本 (坐标点击/窗口激活)
├── perceive.py             # 视频感知模块 (Qwen VLM + ASR + FFprobe)
├── memory_store.py         # 内存缓存层 (分析元数据热加载与小文件缓存)
├── template_engine.py      # YAML 声明式视频模板引擎
├── localsend_recv.py       # LocalSend v2.2 协议局域网素材接收服务
├── mcp_video_server.py     # MCP Server 标准工具服务端
├── gui.py                  # Gradio 交互工作台
├── serve.bat               # Windows 一键无窗口静默启动脚本
├── templates/              # 预设 YAML 视频模板
│   ├── emotional_quotes.yaml
│   ├── knowledge_short.yaml
│   └── product_intro.yaml
├── frontend-react/         # 现代化 React 前端源码 (Vite + Tailwind CSS)
├── static/                 # 前端编译构建产物 (由 render_server 托管)
├── VectCutAPI/             # 剪映草稿协议解析与时间线操作核心库
├── requirements.txt        # Python 依赖清单
├── .env.example            # 环境变量配置模板
└── .mcp.json               # MCP 服务配置文件
```

---

## 🚀 快速开始

### 1. 环境准备
- **操作系统**：Windows 10 / 11 (64-bit)
- **剪映版本**：剪映专业版（JianYing Pro）
- **Python**：Python 3.10+
- **多媒体工具**：[FFmpeg](https://ffmpeg.org/)（需加入系统环境变量 PATH）
- **Node.js**（可选，仅二次开发前端时需要）：Node.js 18+

### 2. 安装 Python 依赖
```bash
pip install -r requirements.txt
```

### 3. 配置环境变量
复制 `.env.example` 为 `.env` 并填入相关配置：
```bash
cp .env.example .env
```
编辑 `.env`：
```ini
# 阿里云 DashScope (通义千问大模型)
QWEN_API_KEY=your_dashscope_api_key_here
QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
QWEN_MODEL=qwen3.7-plus

# 语音转文字 ASR (可选; ASR_BACKEND=remote 音频上传第三方 / =local 本地 faster-whisper)
ASR_ENDPOINT=https://asr.smartbid.site/inference
ASR_API_KEY=your_asr_api_key_here
# ASR_BACKEND=local      # 本地离线识别 (pip install faster-whisper)
# AUTO_PERCEIVE=1        # 素材收件后自动后台分析 (消耗 VLM token)

# 服务端口
RENDER_SERVER_PORT=9002
```

> 所有路径/端口/渲染参数集中在 `config.py`，由环境变量驱动。剪映安装目录、草稿目录、导出目录默认按当前 Windows 用户自动推导，特殊安装才需在 `.env` 中覆盖 `JY_APP_BASE` / `JY_DRAFT_ROOT` / `VIDEOS_DIR`。
>
> **安全提示**：服务默认监听 `0.0.0.0` 以便局域网 LocalSend/手机访问。文件上传、草稿删除、视频下发等接口已做路径净化与白名单校验；若暴露到不可信网络，请自行加反向代理鉴权或改 `RENDER_SERVER_HOST=127.0.0.1`。

### 4. 首次校准（重要）
初次运行前，需对本机分辨率及剪映 UI 关键按钮坐标进行校准：
```bash
python render_driver.py calibrate
```
> 按照终端提示依次点击剪映界面上的：① 草稿卡片、② 导出按钮、③ 确认导出、④ 返回首页，坐标将自动保存至 `calib.json`（该文件为本机数据，已被 gitignore，可参考 `calib.json.example`）。

### 5. 启动服务
- **方式一：主服务（推荐）**
  ```bash
  python render_server.py
  ```
  打开浏览器访问 `http://localhost:9002` 即可使用一体化 Web 工作台。

- **方式二：Windows 后台静默启动**
  双击运行 `serve.bat`。

- **方式三：Gradio 调试台**
  ```bash
  python gui.py
  ```
  访问 `http://localhost:7860`。

---

## 🛠️ 功能使用指引

### 1. 声明式模板生成视频
利用预设 YAML 模板快速渲染视频：
```bash
python template_engine.py render \
  -t templates/emotional_quotes.yaml \
  -v '{"title":"旅行的意义","sentences":["不是所有的路都有终点","最美的风景在路上","出发本身就是到达"],"bg_video":"C:/path/to/video.mp4"}'
```

### 2. 跨端素材投放 (LocalSend)
启动主服务后，同一局域网下的手机或电脑打开 **LocalSend App**，搜索设备将发现 `AI 视频工作台`，直接选择图片/视频发送即可自动进入待选素材池。

### 3. 作为 MCP 工具集成 (Claude Code / Cursor)
在 Claude Desktop 或 Claude Code 配置中添加 `.mcp.json`：
```json
{
  "mcpServers": {
    "video-tools": {
      "command": "python",
      "args": ["mcp_video_server.py"],
      "env": {}
    }
  }
}
```
可供 AI Agent 调用的工具列表：
- `perceive_video`：多模态分析视频内容与精彩片段
- `create_draft`：初始化剪映工程草稿
- `add_video` / `add_text` / `add_audio`：添加视频片段、文字花字、背景音乐
- `save_draft`：保存草稿
- `render`：提交后台无头渲染并返回 MP4
- `perceive_result`：对渲染出的成片进行视觉质检

---

## ⚙️ 前端重新编译（开发者）

如需修改 `frontend-react` 前端界面：
```bash
cd frontend-react
npm install
npm run build
```
编译产物将自动输出至 `../static/` 目录，供 `render_server.py` 直接分发。

---

## 🧪 开发与测试

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v     # 纯逻辑单元测试 (不依赖 Windows/剪映)
```

生产部署建议：`pip install waitress`（`render_server.py` 检测到后自动启用，替代 Flask 开发服务器）。

---

## 📄 开源许可证

本项目基于 [MIT License](LICENSE) 开源。
