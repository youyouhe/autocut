# AI 视频工作台 · 系统功能手册

> **版本**: 2026-08-14
> **代码根目录**: `C:\Users\Administrator\AppData\Local\JianyingPro\User Data\Projects\com.lveditor.draft\ym`
> **一句话定位**: 以剪映专业版 (JianyingPro 5.9.0) 为渲染后端,通过 Frida 注入 + 多桌面真后台,把"上传素材 → AI 看懂 → 模板/对话组装草稿 → 无人值守渲染出 mp4 → AI 质检"串成一条全自动生产线的本地 Web 服务。

---

## 目录

1. [系统总览](#1-系统总览)
2. [整体架构与数据流](#2-整体架构与数据流)
3. [运行环境与依赖](#3-运行环境与依赖)
4. [启动与停止](#4-启动与停止)
5. [核心服务:render_server.py](#5-核心服务render_serverpy)
6. [渲染驱动:render_driver.py](#6-渲染驱动render_driverpy)
7. [Frida 注入层:hook_focus.js](#7-frida-注入层hook_focusjs)
8. [完成检测:render_monitor.py](#8-完成检测render_monitorpy)
9. [视频感知:perceive.py](#9-视频感知perceivepy)
10. [内存缓存:memory_store.py](#10-内存缓存memory_storepy)
11. [LocalSend 接收端:localsend_recv.py](#11-localsend-接收端localsend_recvpy)
12. [模板引擎:template_engine.py](#12-模板引擎template_enginepy)
13. [VectCutAPI 编辑能力融合](#13-vectcutapi-编辑能力融合)
14. [MCP 工具服务:mcp_video_server.py](#14-mcp-工具服务mcp_video_serverpy)
15. [前端 Web GUI](#15-前端-web-gui)
16. [REST API 完整参考](#16-rest-api-完整参考)
17. [典型使用流程](#17-典型使用流程)
18. [配置文件与关键路径](#18-配置文件与关键路径)
19. [故障排查与已知坑](#19-故障排查与已知坑)
20. [逆向工程结论摘要](#20-逆向工程结论摘要)

---

## 1. 系统总览

本系统是一个**本地化的 AI 视频自动生产平台**,核心能力包括:

| 能力 | 实现方式 | 关键文件 |
|------|----------|----------|
| 素材接收 | LocalSend v2.2 协议接收端 + Web 上传 | `localsend_recv.py`, `/api/upload` |
| 素材理解 | VLM 画面分析 + ASR 语音转录 + FFmpeg 场景检测 | `perceive.py` |
| 草稿编辑 | VectCutAPI (pyJianYingDraft) 28 个 REST 端点 | `VectCutAPI/capcut_server.py` |
| 声明式生产 | YAML 模板 + 变量填充自动组装草稿 | `template_engine.py` |
| 对话式生产 | LLM function calling 驱动编辑/渲染 | `/api/chat` (SSE) |
| 无人值守渲染 | Frida 注入剪映 + 独立 Windows 桌面真后台 | `render_driver.py`, `hook_focus.js` |
| 多任务并行 | 2 桌面池 + 阻塞队列 worker | `render_server.py` |
| 渲染质检 | 渲染后抽帧让 VLM 打分 | `perceive_result` |
| 外部接入 | MCP Server 暴露为 AI 工具 | `mcp_video_server.py` |

**设计哲学**: 不改剪映二进制、不依赖剪映任何官方 API(剪映没有开放 API)。所有"自动化"都建立在对剪映 Qt6 内部函数的 Frida 逆向之上——通过合成鼠标事件直接驱动剪映 UI 完成导出,渲染过程在独立 Windows 桌面进行,用户在主桌面完全无感。

---

## 2. 整体架构与数据流

```
┌─────────────────────────────────────────────────────────────────────┐
│                         用户 / 外部 Agent                           │
│   (浏览器 Web GUI)   (Claude Code 等 MCP 客户端)   (curl/脚本)      │
└──────────┬──────────────────────┬──────────────────────┬───────────┘
           │ HTTP (port 9010)     │ MCP stdio            │
           ▼                      ▼                      ▼
┌──────────────────────────────────────────────────────────────────────┐
│  render_server.py  (Flask, 0.0.0.0:9010)                              │
│  ┌────────────┐ ┌──────────────┐ ┌────────────┐ ┌─────────────────┐  │
│  │ 渲染端点   │ │ 前端 API     │ │ 感知端点   │ │ LocalSend 端点  │  │
│  │ /render*   │ │ /api/perceive│ │ /perceive/*│ │ /api/localsend/*│  │
│  └─────┬──────┘ └──────┬───────┘ └─────┬──────┘ └────────┬────────┘  │
│        │               │               │                 │           │
│  ┌─────▼───────────────▼───────────────▼─────────────────▼─────┐     │
│  │  融合 VectCutAPI/capcut_server.py (create/add_*/save_draft)  │     │
│  └────────────────────────────┬────────────────────────────────┘     │
│                               │                                       │
│        ┌──────────────────────┼───────────────────┐                  │
│        ▼                      ▼                   ▼                  │
│  memory_store.py        perceive.py         localsend_recv.py        │
│  (分析+视频内存缓存)    (VLM/ASR/FFmpeg)    (UDP多播+HTTP接收)       │
└──────────────────────────────────────────────────────────────────────┘
                               │
                   渲染任务入队 (RENDER_QUEUE)
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│  render_pool_worker (N 个线程, 对应 N 个桌面)                          │
│  acquire_desktop() → subprocess render_driver.py render-draft         │
│                       --desktop --desktop-name <desk>                 │
└──────────────────────────────┬───────────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│  render_driver.py  (独立进程)                                          │
│  1. CreateDesktopW(JYRender_0) + CreateProcessW(剪映, lpDesktop)      │
│  2. frida.attach(剪映主PID) → load hook_focus.js                      │
│  3. inject_draft() 把草稿复制进剪映草稿根目录(改 id/name/time)         │
│  4. 程序化获取 dev = QPointingDevice::primaryPointingDevice()          │
│  5. click(card) → wait_editor_ready → click(export) → wait_modal      │
│     → clickmodal(confirm) → wait_render_done(轮询 Videos/*.mp4)       │
│  6. clickmodal(close_done) → click(close_editor) → 清理注入草稿        │
│  7. kill 自己启动的剪映 (不影响其他桌面)                               │
└──────────────────────────────┬───────────────────────────────────────┘
                               ▼
                   C:\Users\Administrator\Videos\<name>.mp4
                   (render_server 找到后登记为 task done)
```

**核心数据流(典型生产)**:
```
手机 LocalSend 发素材 → render_uploads/ → 前端扫到 → /api/perceive(VLM+ASR) → 缓存
  → /api/chat 对话("帮我把这个视频配个标题") → LLM function calling
  → create_draft → add_video → add_text → save_draft → render/draft/<id>
  → 桌面池 worker → render_driver --desktop → 剪映在 JYRender 桌面渲染
  → Videos/*.mp4 → /render/status 轮询 done → /render/download 下载
```

---

## 3. 运行环境与依赖

### 3.1 系统环境
- **OS**: Windows 10 Pro 19045(必须 Windows,依赖 Win32 API + 剪映 Windows 版)
- **剪映**: JianyingPro 5.9.0.11632(安装于 `C:\Users\Administrator\AppData\Local\JianyingPro\Apps\`)
- **Python**: 3.12(`C:\Users\Administrator\AppData\Local\Programs\Python\Python312\`)
- **Frida**: 用于注入剪映进程(`pip install frida frida-tools`)
- **FFmpeg/FFprobe**: 系统 PATH 中可用(抽帧、ASR 提取音频、场景检测)

### 3.2 Python 依赖
| 包 | 用途 |
|----|------|
| `flask` | Web 服务 + REST API |
| `frida` | 注入剪映进程、合成鼠标/键盘事件 |
| `openai` | 调用通义千问(Qwen3.7-Plus)兼容 OpenAI 接口 |
| `requests` | 内部 HTTP 调用、ASR 调用 |
| `pyyaml` | 模板引擎解析 YAML |
| `oss2` | VectCutAPI 顶层依赖(实际渲染不强制需要) |
| `json5` | VectCutAPI 配置解析 |

### 3.3 外部 AI 服务
| 服务 | 端点 | 用途 | 配置位置 |
|------|------|------|----------|
| 通义千问 VLM/LLM | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 画面分析、对话、质检 | `perceive.py`, `render_server.py:/api/chat` |
| 自建 ASR | `https://asr.smartbid.site/inference` | 语音转文字(词级时间戳) | `perceive.py` |

> ⚠️ API Key 硬编码在 `perceive.py` 和 `render_server.py` 中。生产部署应迁移到环境变量。

---

## 4. 启动与停止

### 4.1 一键启动(后台无窗口)
```bat
serve.bat
```
- 用 `pythonw.exe` 后台启动 `render_server.py`(无控制台窗口)
- 检测端口 9010 是否已占用,避免重复启动
- 写入 `serve.pid` 供 `stop.bat` 使用
- 启动后访问 **http://localhost:9010**

### 4.2 停止
```bat
stop.bat
```
- 优先按 `serve.pid` 杀进程;若无 PID 则按端口 9010 反查杀

### 4.3 前台调试启动
```bat
python render_server.py
```
- 直接前台运行,可看 stdout 日志,便于调试
- 启动时打印:融合状态、桌面池初始化、worker 数量、LocalSend 待命

### 4.4 前端开发模式
```bat
cd frontend
npm install
npm run dev      # Vite 开发服务器 (5173), 代理到 9010
npm run build    # 构建到 static/ 供 Flask 托管
```

### 4.5 MCP 接入(Claude Code 等)
项目根目录 `.mcp.json` 已配置 `video-tools` MCP Server,启动 Claude Code 时自动加载。

---

## 5. 核心服务:render_server.py

**Flask 单进程单端口(9010)统一服务**,融合 VectCutAPI 编辑端点 + 自研渲染/感知/LocalSend 端点。

### 5.1 融合机制
```python
VC_DIR = os.path.join(HERE, 'VectCutAPI')
sys.path.insert(0, VC_DIR)
from capcut_server import app   # 复用 VectCutAPI 的所有编辑路由
```
- 融合成功:`FUSED_VC = True`,VetCutAPI 的 26+ 路由直接挂载
- 融合失败(缺依赖):降级为独立模式,仅渲染功能

### 5.2 多桌面渲染池
| 组件 | 说明 |
|------|------|
| `DESKTOP_NAMES` | `['JYRender_0', 'JYRender_1']`(2 并行) |
| `desktop_pool` | `{desk_name: {busy: bool}}`,启动时初始化(不预启剪映) |
| `RENDER_QUEUE` | `queue.Queue()`,任务排队 |
| `render_pool_worker` | N 个 daemon 线程,各取任务 → `acquire_desktop` → 调 `render_driver.py render-draft --desktop --desktop-name <desk>` → 释放桌面 |
| `acquire_desktop(timeout=600)` | 阻塞等空闲桌面,加 `DESKTOP_LOCK` 防竞争 |

> 设计要点:render_driver 每次**自己 start + kill 剪映**(用完即弃),桌面池只做互斥锁,不常驻剪映实例。

### 5.3 任务生命周期
```
queued → rendering → done / error
                ↑
        task_id (uuid hex[:8])
```
- `tasks[task_id]` 字典 + `TASK_LOCK` 保护
- `done` 后记录 `mp4_path` / `mp4_name` / `duration`
- 渲染产物查找逻辑:先找 `draft_name*.mp4`,再退而找 `rd*.mp4`(注入名前缀)

### 5.4 端点分组
1. **渲染端点**:`/render`, `/render/status/<id>`, `/render/download/<id>`, `/render/list`, `/render/draft/<id>`
2. **前端 API**:`/api/perceive*`, `/api/drafts*`, `/api/cover`, `/api/memory/*`, `/api/video/serve`, `/api/upload`, `/api/assets`, `/api/chat`, `/api/templates*`, `/api/localsend/*`
3. **感知端点**:`/perceive/video`, `/perceive/result`
4. **静态托管**:Vue SPA catch-all
5. **健康检查**:`/health`

详见 [第 16 节](#16-rest-api-完整参考)。

---

## 6. 渲染驱动:render_driver.py

剪映端到端渲染闭环的命令行 driver,通过 Frida 注入 + Win32 多桌面实现真后台。

### 6.1 命令行用法
```bat
:: 1. 校准坐标(首次/窗口布局变化时)
python render_driver.py calibrate

:: 2. 渲染首页第 N 个草稿(单草稿闭环)
python render_driver.py run [N] [--close]

:: 3. 外部触发:渲染 1 个 + 关剪映
python render_driver.py render-once

:: 4. 渲染指定草稿(注入 + 打开 + 导出)★ 主要入口
python render_driver.py render-draft <草稿文件夹路径> [搜索名] [--desktop] [--desktop-name <desk>] [--close]

:: 5. 开/关剪映
python render_driver.py start | kill
```

**关键 flag**:
- `--desktop`:真后台模式,剪映跑在独立 Windows 桌面,用户在主桌面无感(★ 生产用)
- `--desktop-name <名>`:指定桌面名(多桌面池用,默认 `JYRender`)
- `--close`:渲染完成后关闭自己启动的剪映

### 6.2 草稿注入(`inject_draft`)
```
复制源草稿文件夹 → DRAFT_ROOT/<new_name>
  改 draft_meta_info.json: draft_id(新UUID) / draft_name / draft_fold_path / tm_draft_modified=now
  改 draft_content.json: id
```
- 剪映实时监视 `DRAFT_ROOT`,新文件夹 ~2s 内出现在首页
- `tm_draft_modified=now` 使注入草稿排首页第一 → 直接点 `card` 坐标即可打开,**无需搜索**(桌面模式关键)
- 默认 `draft_name = rd<毫秒时间戳>`(纯英文数字,避免 IME/QString 问题)

### 6.3 真后台原理(`--desktop`)
```
1. OpenDesktopW / CreateDesktopW 创建/打开独立桌面 (JYRender_0)
2. CreateProcessW + STARTUPINFO.lpDesktop = 桌面名  →  剪映在该桌面启动
3. STARTF_USESIZE 设 dwXSize/dwYSize = 1280×720 (与校准尺寸一致)
4. 剪映在该桌面 IS 前台 → focusWindow() 返回有效指针 → handleMouseEvent 可注入
5. Frida 从主桌面 Python 进程 attach(跨桌面,by PID),注入的事件到达 JYRender 桌面的剪映
6. 渲染完 kill 自己启动的剪映 PID (不影响其他桌面的剪映)
```

### 6.4 渲染流程(`render_draft` 方法)
```
[注入草稿] → 等 2s 剪映识别
[获取 dev] primaryPointingDevice() (程序化, 零人工)
[桌面模式] 点 card(首页第一) → wait_editor_ready(5 次重试, 等 showCount 增 + 稳定 1.5s)
[前台模式] 点 search_btn → search_box → type_text(草稿名) → result_card → wait_editor_ready
[导出循环 4 次]
  base_show = get_shown_count()
  click(export) → wait_new_window(导出 modal 出现)
  clickmodal(confirm)  ← confirm 在 modal, 用 lastShownWin
  wait_render_done(draft_name, 20s) → 成功则 break
[关闭] clickmodal(close_done) → click(close_editor) → 回首页
[清理] rmtree 注入草稿 + 同步 root_meta_info.json(移除条目, 避免下次弹"丢失"对话框)
```

### 6.5 完成检测(`wait_render_done`)
- 轮询 `C:\Users\Administrator\Videos\` 找 `<draft_name>*.mp4`
- 文件 >100KB 且大小稳定 2 次(~3s)= 完成
- **不用 temp 文件夹检测**(快速渲染时 temp 闪现太快,易误判)

### 6.6 校准(`calibrate`)
依次引导点击 4 个位置,记录 local 坐标到 `calib.json`:
1. `card` — 草稿卡片
2. `export` — 导出按钮
3. `confirm` — 导出确认
4. `home` — 返回首页

当前 `calib.json`(1280×720 local 坐标):
```json
{
  "card":         {"lx": 275,  "ly": 336},
  "export":       {"lx": 1136, "ly": 18},
  "confirm":      {"lx": 509,  "ly": 635},
  "close_done":   {"lx": 580,  "ly": 396},
  "close_editor": {"lx": 1263, "ly": 21},
  "search_btn":   {"lx": 799,  "ly": 260},
  "search_box":   {"lx": 660,  "ly": 262},
  "result_card":  {"lx": 277,  "ly": 342}
}
```
> `confirm` / `close_done` 在 modal 窗口(用 `clickmodal`);其余用 `click`(focusWindow)。

---

## 7. Frida 注入层:hook_focus.js

注入剪映主进程的 Frida 脚本,提供合成鼠标/键盘事件能力。**所有 Qt 函数地址动态查找**(按 mangled 导出名子串匹配),PID/基址变化都能用。

### 7.1 解析的关键 Qt6 导出
| Mangled 名子串 | 功能 |
|----------------|------|
| `?focusWindow@QGuiApplication@@SAPEAVQWindow@@XZ` | 取当前聚焦 QWindow(自动跟踪 home→editor→dialog 切换) |
| `?x@QWindow` / `?y@QWindow` | 取窗口原点坐标 |
| `?primaryPointingDevice@QPointingDevice` | 程序化获取鼠标设备指针(零人工种子) |
| `?winId@QWindow` | 取 HWND(桌面模式 typewm 用) |
| `?setGeometry@QWindow@@QEAAXHHHH@Z` | 固定窗口位置(避免 Windows 随机放置) |
| `handleMouseEvent@...DefaultDelivery...PEBVQPointingDevice` | 合成鼠标事件入口 |
| `handleKeyEvent@...DefaultDelivery` | 合成键盘事件 |
| `?setVisible@QWindow@@QEAAX_N@Z` | hook 窗口显示,记录 lastShownWin + showCount(检测 modal) |

### 7.2 核心 RPC(rpc.exports)
| RPC | 说明 |
|-----|------|
| `click(lx, ly)` | focusWindow + setGeometry + handleMouseEvent(按+释) |
| `clickmodal(lx, ly)` | 用 lastShownWin(最近显示的 modal)点击 |
| `clicklocal(lx, ly)` | local 坐标点击 |
| `clickglobal(gx, gy)` | global 坐标点击(旧,窗口移位会偏) |
| `typewm(text)` | 桌面模式:hook 内 PostMessageW(WM_CHAR) 跨桌面输入 |
| `status()` | 返回 curWin/curDev/showCount 等 |
| `lastshown()` | 返回最近显示窗口 + showCount |
| `windiff(bc, bs)` | 相对基线的窗口创建/显示增量 |

### 7.3 关键技术点
- **local 坐标优先**:global = origin + local,local 坐标随窗口移动稳定
- **dev 程序化获取**:`primaryPointingDevice` 静态调用,无需用户晃鼠标种子(剪映首页不发 mouse-move)
- **modal 检测**:hook `QWindow::setVisible`,导出弹窗是独立 QWindow(非编辑器窗口),confirm 必须用 `clickmodal`
- **winId 返回 pointer**(非 uint64),PostMessage 参数声明 pointer 须传 `ptr(value)`

---

## 8. 完成检测:render_monitor.py

独立的文件轮询器(后台进程),作为 `wait_render_done` 的补充手段。

- 监视 `C:\Users\Administrator\Videos\`(最终 mp4)和 `.__jianying_export_temp_folder__\`(临时渲染文件)
- 状态机:`idle → rendering → done`
- temp 文件出现 = RENDER STARTED;temp 清空 + 最新 mp4 大小稳定 = RENDER DONE
- 日志写入 `render_monitor.log`

> 注:快速渲染时 temp 闪现太快,主流程改用 `wait_render_done`(按输出名轮询)更可靠。monitor 主要用于调试观察。

---

## 9. 视频感知:perceive.py

让 AI"看懂"视频内容 + "听懂"音频 + "检查"渲染结果。

### 9.1 两个核心函数

#### `perceive_video(video_path, do_asr=True, frame_count=5)`
| 步骤 | 实现 | 输出字段 |
|------|------|----------|
| ① 元数据 | `ffprobe` | `meta`{duration,width,height,fps} |
| ② 场景检测 | FFmpeg `scene` 滤镜 | `scenes`(时间点列表) |
| ③ 抽帧+VLM | 均匀抽 N 帧 → Qwen3.7-Plus | `visual_analysis`(JSON:内容/情绪/质量/亮点/用途/帧内文字) |
| ④ ASR | 提取音频 mp3 → 自建 ASR | `audio`{segments[],full_text} |

#### `perceive_result(mp4_path, expectations=None)`
渲染质检:抽 8 帧 → VLM 打分,返回 `quality_score` / `issues` / `duration_ok` / `suggestions`

### 9.2 ASR 响应解析(`_parse_asr_response`)
兼容多种格式:JSON 列表 / SRT / `{"code","data":"<SRT>"}` 包装 / `{"segments":[]}`。自动归一化为 `{segments:[{start,end,text}], full_text}`。

### 9.3 VLM 提示词
画面分析返回结构化 JSON:
```json
{
  "content": "视频主要内容描述",
  "mood": "情绪氛围",
  "quality": "画面质量评估(1-10)+理由",
  "highlights": ["精彩时间区间"],
  "suitable_for": ["适合用途"],
  "text_in_frame": "画面文字提取"
}
```

---

## 10. 内存缓存:memory_store.py

两级内存缓存,优化重复查询性能。

### 10.1 分析元数据缓存
- 启动时全量载入 `analysis_cache/*.json` → `_analysis_store`(dict,key=path 的 md5)
- `get_analysis(path)` = O(1) dict 查询
- `save_analysis(path, result)` 写内存 + 落盘
- `list_all_analysis()` 返回所有摘要

### 10.2 视频字节缓存
- ≤50MB 视频 → `maybe_load_video` 按需载入内存(`_video_store`)
- `/api/video/serve` 命中缓存时零磁盘 IO,响应头 `X-From-RAM: true`
- 总内存上限 500MB,LRU 淘汰最久未访问(`_evict_oldest_video`)

### 10.3 统计
`/api/memory/stats` 返回 `videos_in_ram` / `ram_used_mb` / `ram_limit_mb` / `analysis_count`。

---

## 11. LocalSend 接收端:localsend_recv.py

实现 LocalSend v2.2 协议接收端,让手机/电脑用官方 LocalSend App 直接往本工具发素材。

### 11.1 协议实现
- **发现**:UDP 多播 `224.0.0.167:53317`,周期发 announce(启动连发 3 次,之后每 30s)
- **传输**:明文 HTTP API `:53317`
- **端点**:
  - `GET /api/localsend/v2/info` — 设备信息
  - `POST /api/localsend/v2/register` — 注册
  - `POST /api/localsend/v2/prepare-upload` — 准备上传(返回 sessionId + tokens)
  - `POST /api/localsend/v2/upload?sessionId=&fileId=&token=` — 上传文件(流式写盘)
  - `POST /api/localsend/v2/cancel` — 取消

### 11.2 多播网卡修复(关键)
**问题**:Windows 多网卡(WSL/Hyper-V/WLAN/蓝牙)时,多播 announce 默认发错网卡 → 手机发现不到。
**修复**:`IP_MULTICAST_IF` 显式指定出口 IP。
- `_detect_outbound_ip()` 探测并打分:优先 `192.168.x`(家庭/热点 LAN),降权 `172.x`(WSL/Docker)、`10.0.0.3`(WireGuard)
- 启动时 `set_multicast_interface` 设置出口

### 11.3 会话管理
- `_active_session`:同一时间只允许一个活跃会话(协议要求,否则 409)
- SHA256 校验(可选)
- 文件落盘到 `render_uploads/`,同名冲突自动加序号 `(1)` `(2)`
- `_received_log`:本次运行接收历史(供前端展示,`start_server` 时清空)

### 11.4 生命周期
- `start_server(save_dir)`:按需启动(前端"接收"按钮触发,**不随服务常驻**)
- `stop_server()`:停止,返回本次接收历史
- `status()`:查设备名/端口/活跃会话/待收文件/已收列表/本机 IP
- 端口 53317 被官方 LocalSend 占用时返回 409

---

## 12. 模板引擎:template_engine.py

声明式视频模板:YAML 模板 + 变量填充 → 自动组装草稿 → (可选)渲染。

### 12.1 模板格式
```yaml
name: 模板名称
description: 描述
canvas: {width: 1080, height: 1920}
scenes:
  - type: video / image / text / audio / subtitle
    source / content: "{{变量}}"
    start / end / duration: 3s
    style: {font_size, font_color, shadow_enabled, background_color, background_alpha}
    position: {x, y}        # -1 到 1
    animation: {intro, outro}
    transition: slide_left
    overlay: true           # 叠加层不推进时间游标
    track_name: title
render: {resolution, framerate}
```

### 12.2 Scene 处理器
| 类型 | 处理器 | 说明 |
|------|--------|------|
| `video` | `_scene_video` | 添加视频轨道,推进 cursor |
| `image` | `_scene_image` | 图片作背景/叠加层 |
| `text` | `_scene_text` | 文字 + 样式 + 位置 + 动画 |
| `audio` | `_scene_audio` | 音频轨道 |
| `subtitle` | `_scene_subtitle` | 从 items 列表批量加字幕 |

### 12.3 变量填充
`_fill` 递归替换 `{{var}}` 占位符(str/dict/list 皆可)。

### 12.4 执行流程
```
加载 YAML → 填充变量 → create_draft → 遍历 scenes 调对应 add_* API → save_draft → (可选) render/draft/<id>
```

### 12.5 内置模板(`templates/`)
- `product_intro.yaml` — 产品介绍(标题→演示→特点→CTA→BGM)
- `knowledge_short.yaml` — 知识短视频
- `emotional_quotes.yaml` — 情感语录

### 12.6 命令行
```bat
python template_engine.py list                              :: 列出模板
python template_engine.py render -t templates/xxx.yaml -v '{"k":"v"}' -r   :: 执行+渲染
```

---

## 13. VectCutAPI 编辑能力融合

`VectCutAPI/capcut_server.py`(基于 pyJianYingDraft)提供草稿编辑能力,被 render_server 融合挂载。

### 13.1 编辑端点(28 个 REST)
| 端点 | 方法 | 说明 |
|------|------|------|
| `/create_draft` | POST | 创建草稿(width/height),返回 draft_id |
| `/add_video` | POST | 添加视频轨道(转场/蒙版/变速/音量) |
| `/add_audio` | POST | 添加音频轨道 |
| `/add_image` | POST | 添加图片 |
| `/add_text` | POST | 添加文字(字体/颜色/阴影/背景/动画) |
| `/add_subtitle` | POST | 批量字幕 |
| `/add_effect` | POST | 添加特效 |
| `/add_sticker` | POST | 添加贴纸 |
| `/add_video_keyframe` | POST | 视频关键帧 |
| `/save_draft` | POST | 保存草稿到磁盘(生成草稿文件夹) |
| `/query_script` | POST | 查询草稿脚本 |
| `/query_draft_status` | POST | 查询草稿状态 |
| `/generate_draft_url` | POST | 生成草稿 URL |
| `/get_intro_animation_types` | GET | 入场动画列表 |
| `/get_outro_animation_types` | GET | 出场动画列表 |
| `/get_combo_animation_types` | GET | 组合动画列表 |
| `/get_transition_types` | GET | 转场列表 |
| `/get_mask_types` | GET | 蒙版列表 |
| `/get_audio_effect_types` | GET | 音效列表 |
| `/get_font_types` | GET | 字体列表 |
| `/get_text_intro_types` | GET | 文字入场 |
| `/get_text_outro_types` | GET | 文字出场 |
| `/get_text_loop_anim_types` | GET | 文字循环动画 |
| `/get_video_scene_effect_types` | GET | 视频场景特效 |
| `/get_video_character_effect_types` | GET | 视频人物特效 |

### 13.2 配置
`VectCutAPI/config.json`:
- `draft_profile`: `capcut_legacy` / `jianying_legacy` / `jianying_pro_10`(决定生成的 draft 格式)
- `IS_CAPCUT_ENV`:CapCut vs 剪映
- `PORT`:端口(融合时被 render_server 的 9010 覆盖)

> ⚠️ VectCutAPI 生成的 `jianying_pro_10`(10.2 格式)草稿与剪映 5.9.0 的兼容性未充分验证;`/render`(zip 直传)路径用 5.9.0 原生草稿已验证可用。

### 13.3 依赖
`pip install oss2 json5`;pyJianYingDraft 是本地包(`VectCutAPI/pyJianYingDraft/`)。

---

## 14. MCP 工具服务:mcp_video_server.py

把 render_server 的能力封装为 MCP 工具,供 Claude Code / Cursor 等 MCP 客户端直接调用。

### 14.1 暴露的 MCP 工具(11 个)
| 工具 | 说明 |
|------|------|
| `perceive_video` | 看懂视频(画面+ASR+场景) |
| `create_draft` | 创建草稿 |
| `add_video` | 添加视频轨道 |
| `add_text` | 添加文字 |
| `add_audio` | 添加音频 |
| `save_draft` | 保存草稿 |
| `render` | 渲染草稿(真后台) |
| `render_status` | 查渲染状态 |
| `perceive_result` | 渲染质检 |
| `get_animations` | 取动画/转场/特效列表(category:intro/outro/combo/transition/mask/font/effect) |

### 14.2 配置
`.mcp.json`:
```json
{
  "mcpServers": {
    "video-tools": {
      "command": "python",
      "args": ["mcp_video_server.py"],
      "cwd": ".../ym"
    }
  }
}
```
MCP Server 内部通过 `requests` 调 `http://localhost:9010` 的 REST API(stdio 传输)。

---

## 15. 前端 Web GUI

React 19 + Tailwind CSS + Vite,构建到 `static/` 由 Flask 托管 (源码在 `frontend-react/`,旧 Vue 工程位于 `frontend/` 已废弃)。

### 15.1 技术栈
- React 19、Tailwind CSS v4、TypeScript
- Vite 6 构建,产物 `outDir: ../static`

### 15.2 组件结构(`frontend-react/src/`)
| 文件 | 职责 |
|------|------|
| `App.tsx` | 主布局 (面板切换/全局状态) |
| `main.tsx` | 入口 |
| `api.ts` | 后端 API 封装 (全部端点, 同源 fetch) |
| `components/AssetPanel.tsx` | 素材面板(上传/LocalSend/分析/预览) |
| `components/DraftPanel.tsx` | 草稿管理(列表/封面/删除/渲染) |
| `components/ChatPanel.tsx` | 对话面板(SSE 流式) |
| `components/SettingsPanel.tsx` | 设置面板 |
| `components/TemplatesPanel.tsx` | 模板面板 |

### 15.3 构建与部署
```bat
cd frontend-react
npm install
npm run build    :: 产物到 ../static/, Flask 自动托管
```
开发模式 `npm run dev`(5173 端口,代理到 9010)。生产模式访问 `http://localhost:9010` 直接用 Flask 托管的 SPA。

---

## 16. REST API 完整参考

> 基址:`http://localhost:9010`

### 16.1 渲染
| 端点 | 方法 | 入参 | 说明 |
|------|------|------|------|
| `/render` | POST | multipart `draft=<zip>` + form `draft_name`(可选) | zip 内须含 `draft_content.json`,异步渲染返回 task_id |
| `/render/status/<task_id>` | GET | — | 查状态(queued/rendering/done/error) |
| `/render/download/<task_id>` | GET | — | 下载 mp4(done 后) |
| `/render/list` | GET | — | 列所有任务 |
| `/render/draft/<draft_id>` | POST | — | 按 VectCutAPI 生成的 draft_id 渲染 |

### 16.2 感知
| 端点 | 方法 | 入参 | 说明 |
|------|------|------|------|
| `/api/perceive` | POST | json `{path, force?, do_asr?, frames?}` | 按路径分析视频(内存缓存优先) |
| `/api/perceive/cached` | GET | query `path` | 仅查缓存(不读磁盘) |
| `/perceive/video` | POST | multipart `video=<文件>` + form `do_asr/frames` | 上传文件分析 |
| `/perceive/result` | POST | multipart `video=<mp4>` + form `expectations`(JSON) | 渲染质检 |

### 16.3 草稿管理
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/drafts` | GET | 列剪映草稿目录所有草稿(仿 CapCut 草稿管理,含封面/时长/创建/修改时间) |
| `/api/cover?folder=` | GET | 取草稿封面图 |
| `/api/drafts/<folder>` | DELETE | 删草稿(文件夹 + root_meta 同步) |

### 16.4 素材与上传
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/upload` | POST | multipart `files`/`file`,存到 `render_uploads/`,自动分类 video/image/audio |
| `/api/assets` | GET | 扫描 `render_uploads/` 返回所有素材 |
| `/api/video/serve?path=` | GET | 内存缓存提供视频字节(≤50MB 零磁盘 IO) |

### 16.5 内存与统计
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/memory/stats` | GET | 视频内存缓存统计 + 分析数 |
| `/api/memory/analysis` | GET | 列所有分析元数据 |

### 16.6 对话(SSE 流式)
| 端点 | 方法 | 入参 | 说明 |
|------|------|------|------|
| `/api/chat` | POST | json `{message, draft_id?, asset_paths?}` | LLM function calling 驱动编辑/渲染,流式返回 |

**对话工具(function calling)**:
- `list_resources` — 列已上传资源
- `get_resource_detail` — 查单资源完整分析(VLM+ASR+元数据)
- `get_transcript` — 取语音转录(含时间戳)
- `create_draft` / `add_video` / `add_text` / `save_draft` / `render` — 编辑+渲染

**SSE 事件格式**:`data: {"text": "..."}` / `data: {"tool": "...", "args": {...}, "result": {...}}` / `data: {"draft_id": "..."}` / `data: [DONE]`

### 16.7 模板
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/templates` | GET | 列可用模板 |
| `/api/templates/render` | POST | json `{template, variables, render?}` 执行模板 |

### 16.8 LocalSend
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/localsend/status` | GET | 接收端状态(设备名/端口/活跃会话/待收/已收/本机IP) |
| `/api/localsend/start` | POST | 按需启动接收端(端口 53317 被占返回 409) |
| `/api/localsend/stop` | POST | 停止,返回本次接收文件列表 |

### 16.9 编辑( VectCutAPI 融合)
见 [第 13 节](#13-vectcutapi-编辑能力融合),28 个端点(`/create_draft`, `/add_video`, `/add_text`, `/add_audio`, `/add_image`, `/add_subtitle`, `/add_effect`, `/add_sticker`, `/add_video_keyframe`, `/save_draft`, 各类动画列表 GET 等)。

### 16.10 系统
| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | `{ok, service, videos_dir}` |
| `/` | GET | Vue SPA(catch-all) |

---

## 17. 典型使用流程

### 流程 A:手机发素材 → 对话式生产
```
1. serve.bat 启动服务
2. 浏览器开 http://localhost:9010
3. 素材面板点"接收"启动 LocalSend
4. 手机 LocalSend App 搜到"AI 视频工作台",发视频
5. 素材自动落 render_uploads/,前端刷新显示
6. 点素材"分析"(VLM+ASR),结果入内存缓存
7. 对话面板输入:"帮我把这个视频配个标题并渲染"
   → LLM 调 create_draft → add_video → add_text(标题) → save_draft → render
8. 渲染面板看状态 → done 后下载 mp4
9. (可选) 质检:perceive_result
```

### 流程 B:模板批量生产
```
1. 编辑 templates/product_intro.yaml,填 {{product_name}} 等变量
2. python template_engine.py render -t templates/product_intro.yaml \
     -v '{"product_name":"AI助手","demo_video":"C:/path/demo.mp4"}' -r
3. 自动 create_draft → add_video → add_text → ... → save_draft → render
4. 轮询 /render/status/<task_id> → done → /render/download
```

### 流程 C:外部 Agent 接入(MCP)
```
1. Claude Code 加载 .mcp.json,获得 video-tools 工具
2. 对话:"看懂这个视频 C:/x.mp4" → perceive_video
3. "建草稿加上它配标题渲染" → create_draft → add_video → add_text → save_draft → render
4. "渲染好了吗" → render_status → "质检一下" → perceive_result
```

### 流程 D:zip 直传渲染(最快)
```
curl -F "draft=@mydraft.zip" http://localhost:9010/render
  → {"task_id": "...", "poll": "/render/status/..."}
curl http://localhost:9010/render/status/<task_id>   # 轮询
curl -O http://localhost:9010/render/download/<task_id>
```
zip 内须含 `draft_content.json` 的草稿文件夹。

---

## 18. 配置文件与关键路径

### 18.1 路径常量
| 用途 | 路径 |
|------|------|
| 项目根 | `C:\Users\Administrator\AppData\Local\JianyingPro\User Data\Projects\com.lveditor.draft\ym` |
| 剪映草稿根 | `C:\Users\Administrator\AppData\Local\JianyingPro\User Data\Projects\com.lveditor.draft` |
| 剪映 exe | `C:\Users\Administrator\AppData\Local\JianyingPro\Apps\<版本>\JianyingPro.exe` |
| 渲染输出 | `C:\Users\Administrator\Videos\` |
| 上传目录 | `ym\render_uploads\` |
| 分析缓存 | `ym\analysis_cache\*.json` |
| 视频内存缓存源 | `ym\gui_uploads\` |
| 前端构建产物 | `ym\static\` |

### 18.2 配置文件
| 文件 | 内容 |
|------|------|
| `calib.json` | 校准坐标(1280×720 local) |
| `.mcp.json` | MCP Server 配置 |
| `serve.bat` / `stop.bat` | 启停脚本 |
| `serve.pid` | 运行中进程 PID |
| `VectCutAPI/config.json` | VectCutAPI 草稿 profile 配置 |

### 18.3 日志文件
| 文件 | 内容 |
|------|------|
| `server.log` / `server.err` | render_server 运行日志 |
| `render_server.log` | 旧日志 |
| `render_monitor.log` | 渲染完成检测日志 |
| `gui.log` | GUI 日志 |
| `focus_<pid>.txt` | Frida hook 调试日志(每进程一个) |

### 18.4 API Key / 端点配置 (通过 .env 环境变量管理)
| 项 | 环境变量名 | 默认值 / 说明 | 位置 |
|----|------------|---------------|------|
| Qwen API Key | `QWEN_API_KEY` / `DASHSCOPE_API_KEY` | 无 (需配置) | `.env`, `perceive.py` |
| Qwen Base URL | `QWEN_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 同上 |
| Qwen Model | `QWEN_MODEL` | `qwen3.7-plus` | 同上 |
| ASR Endpoint | `ASR_ENDPOINT` | `https://asr.smartbid.site/inference` | `perceive.py` |
| ASR API Key | `ASR_API_KEY` | 无 (需配置) | `perceive.py` |

---

## 19. 故障排查与已知坑

### 19.1 服务启动
- **端口 9010 被占/404**:旧进程残留,先 `stop.bat` 或 `Get-NetTCPConnection -LocalPort 9010 | Stop-Process`,再启动。日志显示 fusion 成功但端点 404 = 旧进程在服务。
- **VectCutAPI 融合失败**:缺 `oss2`/`json5`,降级为纯渲染模式。`pip install oss2 json5`。
- **LocalSend 端口 53317 冲突**:关闭官方 LocalSend 或其他占用程序。

### 19.2 渲染
- **calib 缺坐标**:首次必须 `calibrate`;窗口布局变化(分辨率/DPI)需重新校准。
- **草稿卡片点不开**:首页加载时序不稳,render_driver 已内置 5 次重试 + `wait_editor_ready`。
- **confirm 未触发渲染**:导出 modal 时序问题,已内置 4 次重试(每次重新点导出刷 modal)。
- **下次启动弹"草稿丢失"对话框**:`render_draft` 末尾会清理注入草稿 + 同步 root_meta;若异常退出未清理,手动删 `DRAFT_ROOT/rd*` 文件夹并从 `root_meta_info.json` 的 `all_draft_store` 移除对应条目。
- **窗口位置随机偏移导致坐标偏**:`--desktop` 模式 hook 内 `setGeometry(403,108,1280,720)` 每次点击前固定;前台模式 `resize_jianying()` 调到 PRESET 1280×720。
- **dev 未获取**:`primaryPointingDevice` 失败时,任意真实点击可种子(handleMouseEvent hook 抓 dev)。

### 19.3 LocalSend
- **手机发现不到设备**:多播发错网卡。`_detect_outbound_ip` 自动选 192.168.x;仍失败时手动 `set_multicast_interface(电脑IP)`。手机连电脑热点时,出口 IP = 电脑在热点的 IP。
- **多播监听 bind 53317 报 10013**:Windows 端口保留/Hyper-V 占用,不影响主流程(HTTP 接收+announce 是核心)。

### 19.4 感知
- **VLM/ASR 超时**:大视频抽帧多 → token 大;调小 `frame_count`,或 `do_asr=false` 跳过转录。
- **ASR 格式不识别**:`_parse_asr_response` 兼容 JSON/SRT/包装格式;自建 ASR 返回格式变化时需扩展解析。

### 19.5 Frida
- **Module.getExportByName(null,...) 不支持**:改 `Process.getModuleByName('user32.dll').getExportByName('PostMessageW')`。
- **NativeFunction 类型名**:`uint`/`pointer`,**不是** `uint32`/`uintptr`(会抛 invalid type)。
- **PostMessage 参数声明 pointer 须传 `ptr(value)`**,不能传裸数字。
- **winId 返回 pointer 非 uint64**,勿转 uint64。
- **handleMouseEvent send() 复杂 payload 崩溃**:用 File API 写文件替代 send。
- **变量勿名 `ptr`**(遮蔽全局 `ptr()` 函数)。
- **不要缓存窗口指针跨 UI 导航**:QWindow 会重建(focusWindow 自动跟踪是正解)。

### 19.6 前端
- **前端未构建**:`/` 返回 `Frontend not built`。`cd frontend && npm install && npm run build`。
- **dev 模式 CORS**:render_server 已加 `Access-Control-Allow-Origin: *`。

---

## 20. 逆向工程结论摘要

> 详见记忆文件 `jianying-export-frida-findings.md` / `jianying-render-closed-loop.md`。

### 20.1 为什么用 UI 自动化而非 API
- 剪映**无开放 API**;`videoeditor.dll!ExportClient::exportStart`(RVA `0xd05e0`)导出的是**当前已加载**的 VE 草稿,ReqStruct **不含草稿路径**(325 堆块 288KB 全 dumped,只有 RPC 方法名/输出 temp 路径/配置 KEY,无 draft_content.json 路径/分辨率/帧率)。
- 无 `loadDraft`/`openDraft` RPC(videoeditor.dll 只有 CloudDraftClient 云同步);草稿加载在 VMProtect 保护的 VECreator.dll。
- **结论**:Frida API 触发渲染任意草稿被架构阻断,UI 自动化是必由之路。

### 20.2 关键突破
1. **handleMouseEvent 合成点击成立**:Frida NativeFunction 调 `QWindowSystemInterface::handleMouseEvent<DefaultDelivery>+PointingDevice` 变体,从 agent 线程调用无线程断言,Qt 接受事件直达真实按钮。
2. **focusWindow() 在 agent 线程可用**:返回当前聚焦 QWindow,自动跟踪 home→editor→dialog 切换(无需 GUI 线程队列、无需用户晃鼠标)。
3. **dev 程序化获取**:`QPointingDevice::primaryPointingDevice(QString&)` 静态调用,零人工种子。
4. **草稿注入**:剪映实时监视草稿根目录,复制文件夹进去(改 id/name/tm_draft_modified=now)~2s 出现在首页第一,无需 open-draft API。
5. **modal 是独立 QWindow**:hook `QWindow::setVisible` 记录 lastShownWin,confirm/close_done 用 `clickmodal`。
6. **真后台**:`CreateDesktopW` + `CreateProcessW(lpDesktop)` 让剪映在独立桌面是前台 → focusWindow 有效 → 跨桌面注入成立。用户在主桌面无感。
7. **完成检测**:轮询 `Videos\<draft_name>*.mp4` 大小稳定(非 temp 文件夹,避免快速渲染误判)。

### 20.3 稳定性
- `--desktop` 模式 5/5 连续成功,每次 ~35-60s,exit 0。
- 三大根因修复:(a) hook 内 setGeometry 固定窗口位置;(b) 注入草稿清理时同步 root_meta_info.json(避免"草稿丢失"对话框阻塞);(c) 桌面模式跳过搜索直接点首页第一卡片(注入草稿排第一)。

---

*本手册基于 2026-08-14 代码状态整理。系统仍在迭代,以代码实际行为为准。*
