# S07 核心服务融合与前端 GUI — 需求规格说明书

## 文档信息

| 项目 | 内容 |
|------|------|
| 项目名称 | AI 视频工作台 |
| 所属分册 | 第四册 服务集成与交互 |
| 系统编号 | S07 |
| 系统名称 | 核心服务融合与前端 GUI |
| 功能点总数 | 10 |
| ▲标注数 | 0 |
| 编制日期 | 2026-08-14 |
| 文档版本 | V1.0 |

> 数据类型遵循 `_metadata.md` 数据类型约定（无关系型数据库，任务/状态为内存字典，配置为 JSON 文件，路径受 Windows MAX_PATH 260 约束）。
> 角色编码、外部系统编码、术语均引用 `_metadata.md`。

---

## 一、系统概述

### 1.1 系统定位

S07 是 AI 视频工作台的"中枢与门面"：向下将分散的能力模块（VectCutAPI 草稿编辑、无人值守渲染、视频感知、LocalSend 接收、对话式生产）融合为 Flask 单进程单端口（9002）统一服务 `render_server.py`；向上以 Vue 3 Web GUI 的形式向视频创作者提供浏览器操作界面，并通过静态托管、SPA 路由、CORS 与健康检查保障服务的可访问性与可运维性。

系统处于第四册"服务集成与交互"的核心位置：S01～S06 的全部能力经 S07 融合后对外暴露为一个 HTTP 入口，前端五个面板（素材、LocalSend 接收、对话、草稿管理、渲染任务）全部经 S07 的前端 API 间接调用各子系统端点。没有 S07，各子系统只能通过零散脚本独立运行，无法形成"上传素材 → AI 感知 → 对话组装草稿 → 无人值守渲染 → 下载成片"的完整生产线。

### 1.2 业务范围

| 功能域 | 功能点 | 内容 |
|--------|--------|------|
| 服务融合与运行管理 | S07-001、S07-002 | VectCutAPI sys.path 挂载融合与降级模式；serve.bat/stop.bat 启停、端口 9002 占用检测、serve.pid 进程管理 |
| 前端交互面板（Vue 3 + Element Plus + Pinia） | S07-003～S07-007 | 素材面板、LocalSend 接收对话框、对话面板（SSE）、草稿管理面板、渲染任务面板 |
| 服务端支撑 API | S07-008 | 草稿管理 API（列表/封面/删除 + root_meta 同步） |
| 服务可用性 | S07-009、S07-010 | 静态托管与 SPA 路由（catch-all + CORS）、健康检查 /health |

### 1.3 用户角色（引用 _metadata.md 角色定义）

| 角色编码 | 角色名称 | 在本系统中的职责 |
|---------|---------|----------------|
| R01 | 视频创作者 | 通过浏览器 http://localhost:9002 使用五个面板：上传/接收素材、发起 AI 分析、对话式生产、管理草稿、提交渲染并下载成片 |
| R03 | 脚本/程序调用方 | 通过 curl/HTTP 直接调用 /health、/api/drafts、/api/upload 等端点编排自动化流程 |
| R04 | 系统运维/开发者 | 使用 serve.bat/stop.bat 启停服务、前台调试排查融合降级与端口冲突（§19.1）、执行 npm run build 部署前端（§19.6） |

### 1.4 系统边界

**边界内**：render_server.py 进程本体（融合装载、前端 API、静态托管、SPA catch-all、CORS、/health）、frontend/ 源码与 static/ 构建产物（App.vue、5 个业务组件、4 个 Pinia stores、api/index.js axios 封装）、serve.bat/stop.bat/serve.pid 启停设施。

**边界外（经接口调用，不在本系统实现）**：
- S01 素材接入与缓存管理（/api/upload、/api/assets、/api/video/serve、/api/localsend/* 的服务端实现）
- S02 视频感知与质检（/api/perceive* 的分析执行）
- S04 对话式生产（/api/chat 的 LLM function calling 与 SSE 生成）
- S05 草稿编辑服务（VectCutAPI 28 个编辑端点的业务实现，S07 仅负责挂载融合）
- S06 无人值守渲染（/render* 端点的渲染执行）
- 外部系统：EXT-02 通义千问、EXT-04 LocalSend 客户端、EXT-05 FFmpeg/FFprobe、EXT-01 剪映（经 S05/S06 间接作用）

---

## 二、功能需求

### 2.1 服务融合与运行管理

#### S07-001 VectCutAPI 融合机制（sys.path 挂载 + 降级模式）

##### 一、功能综述

render_server.py 以 Flask 单进程单端口（9002）对外提供全部服务，其中草稿编辑能力（创建草稿、添加视频/音频/图片/文字/字幕/贴纸/特效、保存草稿、查询、资源类型字典等 26+ 路由）并非自研实现，而是来自独立的 VectCutAPI 子项目（`VectCutAPI/capcut_server.py`，基于本地包 pyJianYingDraft）。S07-001 定义该能力的融合装载机制：启动时将 VectCutAPI 目录插入 `sys.path`，直接导入其 Flask `app` 对象，复用其全部编辑路由，使 28 个编辑端点（详见 S05 需求规格）与自研渲染/感知/LocalSend 端点共存于同一端口。

融合是"尽力而为"的：VectCutAPI 顶层依赖 `oss2` 与 `json5`（实际渲染不强制需要）在部署机上可能缺失。系统必须在启动时探测依赖可用性——融合成功置 `FUSED_VC = True`，全量挂载编辑路由；融合失败（如 ImportError）则捕获异常、置 `FUSED_VC = False`，降级为纯渲染模式，服务仍可启动并提供渲染/感知/LocalSend/前端面板等其余全部功能，仅在调用编辑类端点时返回不可用。该降级策略保障了"缺依赖不宕机"，是 §19.1 故障排查条目"融合失败 → pip install oss2 json5"的机制前提。

该功能上游是 S05 草稿编辑服务的代码资产，下游是 S04 对话式生产、S08 MCP 工具服务对 create_draft/add_video 等编辑端点的调用，以及 S07-003～007 前端面板经编辑端点完成的草稿组装。

##### 二、业务对象

| 编码 | 业务对象 | 数据项 | 数据类型 | 长度 | 必填 | 编码引用说明 | 备注 |
|------|---------|--------|---------|------|------|------------|------|
| BO-07-001 | 融合状态 | fused_vc | 布尔(bool) | 1 | Y | 本系统运行时标志 | True=融合成功；False=降级纯渲染模式 |
| BO-07-001 | 融合状态 | mount_path | VARCHAR | 260 | Y | 项目根/VectCutAPI | sys.path.insert(0, VC_DIR) 挂载目录 |
| BO-07-001 | 融合状态 | route_count | INT | 4 | N | S05 编辑路由清单 | 挂载的编辑路由数量（26+） |
| BO-07-001 | 融合状态 | missing_deps | VARCHAR | 100 | N | oss2 / json5 | 缺失的 Python 依赖名列表 |
| BO-07-001 | 融合状态 | degraded_reason | VARCHAR | 255 | N | ImportError 异常信息 | 降级原因描述，写入启动日志 |
| BO-07-001 | 融合状态 | startup_log | TEXT | - | N | server.log / server.err | 启动时打印的融合状态、桌面池初始化、worker 数量、LocalSend 待命信息 |

##### 三、业务活动

1. **装载**：服务启动时将 `VectCutAPI` 目录插入 sys.path 首位，导入 `capcut_server.app`，复用其全部编辑路由（融合挂载，非反向代理）。
2. **探测与判定**：导入期间捕获依赖缺失异常（oss2/json5 等），判定融合或降级，设置 `FUSED_VC` 标志。
3. **状态通报**：启动日志打印融合状态（前台启动直接打印到 stdout，后台启动写 server.log/server.err），供运维确认。
4. **降级运行**：降级模式下继续初始化多桌面渲染池、worker 线程、LocalSend 待命与前端端点，服务不退出。
5. **恢复**：运维补装依赖（`pip install oss2 json5`）后重启服务，重新走装载流程恢复融合。

##### 四、用例描述

##### 用例 U-07-001-01

| 项目 | 内容 |
|------|------|
| 用例编号 | U-07-001-01 |
| 用例名称 | VectCutAPI 融合挂载 |
| 业务说明 | 部署机已安装 oss2/json5 依赖，服务启动时成功挂载 VectCutAPI 全部编辑路由，28 个编辑端点与自研端点同端口可用 |
| 规范引用 | _metadata.md 合规要求-版本锁定（jianying_pro_10 草稿与剪映 5.9.0 兼容性未充分验证，融合方须透传该限制） |
| 业务规则 | 1. 挂载方式必须为 sys.path.insert(0, VC_DIR) 后 `from capcut_server import app` 的进程内对象复用，禁止另起子进程/子端口代理。2. 融合成功必须置 FUSED_VC=True 并在启动日志中打印融合成功标志与挂载路由范围。3. VectCutAPI/config.json 的 PORT 配置在融合模式下被 render_server 的 9002 覆盖，不得另开监听端口。4. 编辑端点的草稿格式档位（draft_profile：capcut_legacy/jianying_legacy/jianying_pro_10）由 VectCutAPI/config.json 决定，融合层不得改写。 |
| 使用范围 | R04 系统运维/开发者（启动与确认）；R01/R03 作为下游受益方 |
| 先决条件 | 项目根存在 VectCutAPI/ 目录与本地包 VectCutAPI/pyJianYingDraft/；oss2、json5 已 pip 安装 |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 4 个基本功能点：|
| | 1. 启动时将 VectCutAPI 目录插入 sys.path 并导入 capcut_server.app |
| | 2. 复用其全部编辑路由（create_draft/add_video/add_text/add_audio/add_image/add_subtitle/add_effect/add_sticker/add_video_keyframe/save_draft/query_script/query_draft_status/generate_draft_url 及 12 个资源类型 GET 端点） |
| | 3. 设置并暴露 FUSED_VC 融合状态标志 |
| | 4. 启动日志打印融合状态 |
| 辅助功能 | 前台调试启动时 stdout 同步打印桌面池初始化、worker 数量、LocalSend 待命信息 |
| 提示信息 | 融合失败时日志提示缺失依赖清单及 `pip install oss2 json5` 修复指引 |

**处理逻辑：**
1. 计算 VC_DIR = 项目根/VectCutAPI；
2. sys.path.insert(0, VC_DIR)；
3. try: from capcut_server import app → FUSED_VC=True；
4. 挂载编辑路由到统一服务，与渲染/感知/LocalSend/前端端点同组注册；
5. 启动日志输出融合结果。

**状态流转：**

| 当前状态 | 事件 | 目标状态 | 前置校验 | 后置动作 |
|---------|------|---------|---------|---------|
| 未启动 | serve.bat / 前台启动且依赖齐备 | 融合模式（FUSED_VC=True） | oss2、json5 可导入 | 打印融合成功，全量端点可用 |
| 未启动 | 启动且依赖缺失（ImportError） | 降级模式（FUSED_VC=False） | 异常被捕获 | 打印降级原因与修复指引，继续启动其余功能 |
| 降级模式 | 补装依赖并重启服务 | 融合模式 | oss2、json5 可导入 | 重新挂载编辑路由 |

**约束条件：**
1. 融合必须不引入第二监听端口，全部端点统一在 9002。
2. 降级不得中断服务启动，也不得影响渲染、感知、LocalSend、前端面板与 /health 的可用性。
3. VectCutAPI 生成的 jianying_pro_10 草稿与剪映 5.9.0 的兼容性未充分验证，需求上须作为已知限制透传给 S05/S06。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | VectCutAPI 目录、capcut_server.app 对象、VectCutAPI/config.json（draft_profile/IS_CAPCUT_ENV/PORT） |
| 输出信息 | BO-07-001 融合状态（FUSED_VC/mount_path/route_count/startup_log） |

**业务表单：** 无（运行时机制，无用户表单）

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 融合挂载/状态确认/依赖修复重启 | R04 系统运维/开发者 |
| 编辑端点下游使用 | R01 视频创作者、R03 脚本/程序调用方（经 S04/S08/前端面板间接调用） |

##### 用例 U-07-001-02

| 项目 | 内容 |
|------|------|
| 用例编号 | U-07-001-02 |
| 用例名称 | 依赖缺失降级纯渲染模式 |
| 业务说明 | 部署机缺少 oss2/json5 时，服务以纯渲染模式启动：渲染、感知、LocalSend、前端面板、健康检查全部可用，编辑类端点不可用 |
| 规范引用 | 无 |
| 业务规则 | 1. 依赖导入异常必须被捕获，禁止因融合失败导致服务启动失败。2. 降级模式下 FUSED_VC=False，编辑类端点调用应返回不可用（非 500 崩溃），提示功能未融合。3. 降级事实与缺失依赖清单必须写入启动日志，作为 §19.1 排查依据。4. 渲染产物查找逻辑（先找 draft_name*.mp4 再退而找 rd*.mp4）与降级无关，须保持可用。 |
| 使用范围 | R04 系统运维/开发者（识别降级并修复）；R01/R03 在降级期间仅可使用非编辑功能 |
| 先决条件 | oss2 或 json5 未安装；VectCutAPI 目录存在 |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 3 个基本功能点：|
| | 1. 捕获融合导入异常并记录缺失依赖 |
| | 2. 置 FUSED_VC=False 继续启动（纯渲染模式） |
| | 3. 启动日志输出降级原因与 pip install oss2 json5 修复指引 |
| 辅助功能 | /health 可用于降级模式下确认服务仍存活 |
| 提示信息 | 日志：融合失败原因 + 修复命令 |

**处理逻辑：**
1. try 包裹 capcut_server 导入；
2. except 记录 missing_deps 与 degraded_reason；
3. FUSED_VC=False，跳过编辑路由注册；
4. 继续初始化多桌面渲染池（DESKTOP_NAMES=['JYRender_0','JYRender_1']）、RENDER_QUEUE、render_pool_worker、LocalSend 待命与前端端点。

**约束条件：**
1. 降级是静默能力收缩：不得弹出阻塞对话框或要求交互确认。
2. 降级期间 zip 直传渲染（/render，用 5.9.0 原生草稿，已验证可用）必须保持可用，作为降级模式的替代生产路径。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | ImportError 等导入异常信息 |
| 输出信息 | BO-07-001 融合状态（FUSED_VC=False / missing_deps / degraded_reason） |

**业务表单：** 无

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 降级识别、依赖补装、重启恢复 | R04 系统运维/开发者 |

##### 五、接口依赖

| 方向 | 对接系统 | 接口说明 | 数据格式 |
|------|---------|---------|---------|
| 输入 | S05 草稿编辑服务（VectCutAPI/capcut_server.app） | 进程内导入复用其 Flask app 与 28 个编辑路由 | Python 对象（sys.path 挂载） |
| 输出 | S04 对话式生产 / S08 MCP 工具服务 / S07-003～007 前端面板 | 编辑端点经统一 9002 端口暴露：POST /create_draft、/add_video、/add_text、/save_draft 等 | HTTP JSON |
| 输出 | R04 运维 | 启动日志（融合状态/降级原因） | 文本（server.log / server.err / stdout） |

##### 六、需求追溯

| 来源 | 引用 |
|------|------|
| 技术响应表 | 数据源为系统功能手册（无标书响应表），对应 _metadata.md S07 功能点索引 S07-001 |
| 技术方案 | SYSTEM_MANUAL.md §5.1 融合机制、§13 VectCutAPI 编辑能力融合、§13.3 依赖、§19.1 故障排查 |
| 优先级 | P1 |
| ▲标注 | 否 |

---

#### S07-002 服务启停管理（serve.bat/stop.bat/端口检测/PID）

##### 一、功能综述

AI 视频工作台以本地 Windows 服务形态运行，S07-002 定义其启停管理设施：`serve.bat` 一键后台启动、`stop.bat` 停止、前台调试启动与前端构建命令。一键启动使用 `pythonw.exe` 无控制台窗口地后台运行 render_server.py，适合日常使用（用户主桌面不出现黑框）；启动前检测端口 9002 是否已被占用以避免重复启动；启动成功后将进程 PID 写入 `serve.pid`，供 stop.bat 精确停止。停止时优先按 serve.pid 杀进程，若无 PID 文件则按端口 9002 反查杀掉监听进程——这覆盖了 PID 文件丢失（如异常断电）的场景。启动后用户以浏览器访问 http://localhost:9002 即可使用。

该功能是全系统的运行前提：S01～S08 全部能力均经此进程对外提供。§19.1 将"端口 9002 被占/404（旧进程残留）"列为首位故障，本功能的端口占用检测与 PID/端口双路停止策略正是其机制保障。

##### 二、业务对象

| 编码 | 业务对象 | 数据项 | 数据类型 | 长度 | 必填 | 编码引用说明 | 备注 |
|------|---------|--------|---------|------|------|------------|------|
| BO-07-002 | 服务进程 | pid | INT | 10 | Y | 操作系统进程标识 | pythonw 进程 PID，写入 serve.pid |
| BO-07-002 | 服务进程 | pid_file | VARCHAR | 260 | Y | 项目根/serve.pid | stop.bat 优先依据 |
| BO-07-002 | 服务进程 | port | INT | 4 | Y | 固定 9002 | _metadata.md 运行环境约定端口 |
| BO-07-002 | 服务进程 | port_occupied | 布尔 | 1 | Y | 启动前端口检测 | True 时拒绝重复启动 |
| BO-07-002 | 服务进程 | launch_mode | VARCHAR | 10 | Y | pythonw / foreground | 后台无窗口 / 前台调试 |
| BO-07-002 | 服务进程 | state | VARCHAR | 10 | Y | stopped/starting/running | 服务生命周期状态 |
| BO-07-002 | 服务进程 | log_files | VARCHAR | 260 | N | server.log / server.err | 后台模式日志输出 |

##### 三、业务活动

1. **一键启动**（serve.bat）：pythonw 后台启动 render_server.py → 端口 9002 占用检测 → 通过后写 serve.pid → 就绪后可访问 http://localhost:9002。
2. **停止**（stop.bat）：读 serve.pid 杀进程；PID 缺失/失效时按端口 9002 反查（Get-NetTCPConnection -LocalPort 9002 思路）杀掉监听进程。
3. **前台调试启动**（`python render_server.py`）：stdout 直读日志（融合状态、桌面池初始化、worker 数量、LocalSend 待命），用于排查。
4. **状态确认**：通过 /health（S07-010）或端口连通性确认服务存活；通过旧进程 404 症状识别"日志显示 fusion 成功但端点 404 = 旧进程在服务"的残留场景。
5. **重复启动拦截**：检测到 9002 已占用时提示已运行，不再启动第二实例。

##### 四、用例描述

##### 用例 U-07-002-01

| 项目 | 内容 |
|------|------|
| 用例编号 | U-07-002-01 |
| 用例名称 | 一键后台启动（pythonw 无窗口） |
| 业务说明 | 运维/创作者双击 serve.bat，服务以无控制台窗口方式后台启动，浏览器访问 http://localhost:9002 使用系统 |
| 规范引用 | _metadata.md 合规要求-运行环境（端口 9002 主服务）；操作系统约束（Windows 10 Pro 19045 + Python 3.12） |
| 业务规则 | 1. 必须使用 pythonw.exe 启动，不得弹出控制台窗口干扰用户主桌面。2. 启动前必须检测端口 9002 是否已占用；已占用时判定为服务已在运行（或旧进程残留），拒绝重复启动并提示。3. 启动后必须将 pythonw 进程 PID 写入 serve.pid，供 stop.bat 使用。4. 启动完成后访问入口固定为 http://localhost:9002。 |
| 使用范围 | R04 系统运维/开发者；R01 视频创作者（日常开机自用场景） |
| 先决条件 | Windows 环境且 Python 3.12 可用；9002 端口空闲；serve.bat 位于项目根 |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 4 个基本功能点：|
| | 1. pythonw.exe 后台启动 render_server.py（无控制台窗口） |
| | 2. 端口 9002 占用检测（占用则拒绝并提示） |
| | 3. 写入 serve.pid |
| | 4. 提示访问地址 http://localhost:9002 |
| 辅助功能 | 后台日志写入 server.log / server.err，便于事后排查 |
| 提示信息 | 端口已占用提示"服务已在运行或端口被占，请先执行 stop.bat" |

**处理逻辑：**
1. 检测 9002 端口占用；
2. 占用 → 输出提示并退出；空闲 → 继续；
3. 以 pythonw 启动 render_server.py；
4. 获取 PID 写入 serve.pid；
5. 输出访问地址。

**状态流转：**

| 当前状态 | 事件 | 目标状态 | 前置校验 | 后置动作 |
|---------|------|---------|---------|---------|
| stopped | serve.bat 且 9002 空闲 | starting → running | 端口检测通过 | 写 serve.pid，打印访问地址 |
| stopped | serve.bat 且 9002 被占 | stopped（拒绝启动） | 端口检测不通过 | 提示已运行/冲突，指向 stop.bat |
| running | serve.bat 再次执行 | running（不变） | 端口检测不通过 | 拦截重复启动 |

**约束条件：**
1. 启停脚本仅适用于 Windows（bat 脚本 + pythonw）。
2. 不得出现双实例：任何启动路径都先过端口检测。
3. serve.pid 内容必须与实际服务进程一致，服务停止后应清理。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | serve.bat 命令、端口 9002 探测结果、Python 解释器路径 |
| 输出信息 | BO-07-002 服务进程（pid/port/state/launch_mode=pythonw） |

**业务表单：** 无（命令行脚本交互）

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 一键启动/停止/调试启动 | R04 系统运维/开发者、R01 视频创作者 |

##### 用例 U-07-002-02

| 项目 | 内容 |
|------|------|
| 用例编号 | U-07-002-02 |
| 用例名称 | 停止服务（PID 优先 + 端口反查兜底） |
| 业务说明 | 运维执行 stop.bat 停止服务；serve.pid 存在时按 PID 精确杀进程，PID 文件缺失或失效时按端口 9002 反查杀掉监听进程 |
| 规范引用 | 无 |
| 业务规则 | 1. 停止顺序必须先按 serve.pid 杀进程。2. serve.pid 缺失或指向进程已不存在时，必须按端口 9002 反查并杀掉监听进程（兜底，覆盖 PID 文件丢失场景）。3. 停止成功后清理 serve.pid，避免下次误判。4. 停止不得误杀其他端口进程——反查仅针对 9002 监听者。 |
| 使用范围 | R04 系统运维/开发者 |
| 先决条件 | 服务在运行（或疑似残留） |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 3 个基本功能点：|
| | 1. 读 serve.pid 按 PID 结束 pythonw 进程 |
| | 2. PID 不可用时按端口 9002 反查杀进程 |
| | 3. 停止后清理 serve.pid |
| 辅助功能 | 停止后可配合 Get-NetTCPConnection -LocalPort 9002 | Stop-Process 手工兜底（§19.1） |
| 提示信息 | 无 PID 且端口无监听时提示服务未在运行 |

**处理逻辑：**
1. 读 serve.pid；
2. 存在且进程存活 → 杀该进程；
3. 否则查 9002 端口监听进程 → 杀；
4. 清理 serve.pid，输出停止结果。

**约束条件：**
1. 仅结束 render_server 相关进程，不得波及剪映主程序（剪映由 S06 渲染驱动按 PID 自管）。
2. 服务停止时正在渲染的任务会中断，需提示用户知悉。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | serve.pid 内容、端口 9002 监听信息 |
| 输出信息 | BO-07-002 服务进程（state=stopped，serve.pid 清理） |

**业务表单：** 无

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 停止服务/残留进程清理 | R04 系统运维/开发者 |

##### 用例 U-07-002-03

| 项目 | 内容 |
|------|------|
| 用例编号 | U-07-002-03 |
| 用例名称 | 前台调试启动与故障排查 |
| 业务说明 | 运维以 `python render_server.py` 前台启动，实时查看 stdout 日志排查融合降级、桌面池初始化、LocalSend 待命等问题 |
| 规范引用 | _metadata.md 合规要求-密钥安全（调试时注意 API Key 硬编码现状） |
| 业务规则 | 1. 前台启动必须打印：融合状态（S07-001）、桌面池初始化、worker 数量、LocalSend 待命信息。2. 前台模式与后台模式功能完全一致，仅日志输出位置不同（stdout vs server.log/server.err）。3. 调试确认"日志显示 fusion 成功但端点 404"时，应判定为旧进程残留，按 U-07-002-02 处理后重启。4. 前台调试实例同样受端口 9002 约束，后台实例在运行时前台启动会端口冲突。 |
| 使用范围 | R04 系统运维/开发者 |
| 先决条件 | 9002 端口空闲（旧实例已停止） |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 2 个基本功能点：|
| | 1. 前台运行 render_server.py，stdout 实时输出日志 |
| | 2. 启动横幅打印融合状态/桌面池/worker 数/LocalSend 待命 |
| 辅助功能 | Ctrl+C 直接结束（调试场景）；异常栈直读 |
| 提示信息 | 端口冲突时输出占用错误 |

**处理逻辑：**
1. 运维执行 `python render_server.py`；
2. 观察启动横幅（融合状态等）；
3. 复现/定位问题（404 旧进程、融合降级、53317 冲突）；
4. 修复后切回 serve.bat 生产模式。

**约束条件：**
1. 调试模式不写 serve.pid（前台进程可 Ctrl+C 直接收敛），stop.bat 不承担其停止职责。
2. 前台调试属运维操作，不面向 R01 暴露。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | 命令行 `python render_server.py` |
| 输出信息 | BO-07-002 服务进程（launch_mode=foreground）+ stdout 启动日志 |

**业务表单：** 无

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 前台调试启动/日志排查 | R04 系统运维/开发者 |

##### 五、接口依赖

| 方向 | 对接系统 | 接口说明 | 数据格式 |
|------|---------|---------|---------|
| 输出 | 全部子系统 S01～S06、S08 | 启停的是承载全部端点的统一进程（9002） | 进程管理（bat/pid/端口） |
| 输出 | R04 运维 | server.log / server.err 运行日志 | 文本文件 |
| 输入 | S07-010 健康检查 | 启动后状态确认经 GET /health | HTTP JSON |

##### 六、需求追溯

| 来源 | 引用 |
|------|------|
| 技术响应表 | 数据源为系统功能手册，对应 _metadata.md S07 功能点索引 S07-002 |
| 技术方案 | SYSTEM_MANUAL.md §4 启动与停止（4.1 一键启动/4.2 停止/4.3 前台调试/4.4 前端开发模式）、§18.2 配置文件、§19.1 故障排查 |
| 优先级 | P1 |
| ▲标注 | 否 |

---

### 2.2 前端交互面板（Vue 3 + Element Plus + Pinia + Vite）

> 本功能域 5 个功能点（S07-003～007）共享同一前端技术底座：Vue 3.5 + Element Plus 2.8 + Pinia 2.2 状态管理 + axios HTTP 客户端 + marked（markdown 渲染）+ Vite 5.4 构建；组件位于 `frontend/src/`，构建产物输出到 `static/` 由 Flask 托管（见 S07-009）。全局状态由 4 个 Pinia stores 承担：`stores/asset.js`（素材）、`stores/chat.js`（对话）、`stores/project.js`（草稿/项目）、`stores/render.js`（渲染任务）；全部 HTTP 调用经 `api/index.js` axios 封装统一收敛到 http://localhost:9002 基址。

#### S07-003 素材面板（上传/LocalSend/分析/预览）

##### 一、功能综述

素材面板（`components/AssetPanel.vue`）是创作者进入生产线的第一入口，承担"素材从哪里来"的四种通道与"素材长什么样"的呈现：本地文件上传（经 /api/upload 自动分类 video/image/audio 到 render_uploads/）、LocalSend 无线接收入口（唤起 S07-004 接收对话框）、AI 分析触发（经 /api/perceive 调 S02 感知能力，结果入 S01 内存缓存）、视频在线预览（经 /api/video/serve 内存缓存字节流播放，命中时零磁盘 IO）。面板状态由 Pinia `stores/asset.js` 管理，列表经 GET /api/assets 扫描 render_uploads/ 全量素材获得。

该面板与 S01（素材接入与缓存）、S02（视频感知与质检）强关联：面板本身不实现分类、缓存与分析逻辑，仅作为前端交互层把它们串成"上传 → 出现在列表 → 点分析 → 看结果 → 选中进对话/渲染"的操作动线，为 S07-005 对话面板提供 asset_paths、为 S07-007 渲染面板提供素材来源。

##### 二、业务对象

| 编码 | 业务对象 | 数据项 | 数据类型 | 长度 | 必填 | 编码引用说明 | 备注 |
|------|---------|--------|---------|------|------|------------|------|
| BO-07-003 | 素材条目 | file_name | VARCHAR | 255 | Y | render_uploads/ 文件名 | 同名冲突自动加序号 (1)(2)（S01 规则） |
| BO-07-003 | 素材条目 | file_type | VARCHAR | 10 | Y | video / image / audio | /api/upload 自动分类 |
| BO-07-003 | 素材条目 | path | VARCHAR | 260 | Y | render_uploads/ 绝对路径 | Windows MAX_PATH 约定 |
| BO-07-003 | 素材条目 | size_mb | DECIMAL | (10,2) | Y | 文件大小 | 素材列表展示 |
| BO-07-003 | 素材条目 | duration | DECIMAL | (10,3) | N | 秒，ffprobe 提取 | video/audio 类适用 |
| BO-07-003 | 素材条目 | md5_key | VARCHAR | 32 | N | 路径 md5（S01 内存缓存键） | 已有分析则面板标记"已分析" |
| BO-07-003 | 素材条目 | analysis_cached | TINYINT | 1 | N | 0=否 1=是 | 依据 /api/perceive/cached 查询结果 |

##### 三、业务活动

1. **上传**：选择本地一个或多个文件 → POST /api/upload（multipart files/file）→ 落盘 render_uploads/ 并自动分类 → 刷新列表。
2. **刷新列表**：GET /api/assets 扫描 render_uploads/ → 写入 stores/asset.js → 表格/卡片渲染。
3. **发起分析**：选中素材 → POST /api/perceive {path, force?, do_asr?, frames?} → 展示 VLM 内容/情绪/质量/亮点、ASR 转录、场景列表；已有缓存时优先 /api/perceive/cached 直接读取。
4. **预览播放**：视频类素材经 GET /api/video/serve?path= 播放；响应头 X-From-RAM: true 表示命中内存缓存（≤50MB 视频，LRU 500MB，S01 规则）。
5. **接收入口**：点"接收"按钮打开 LocalSend 接收对话框（S07-004），接收完成回调刷新素材列表。
6. **选中传递**：将素材 path 集合作为 asset_paths 传给对话面板（S07-005）作为生产原料。

##### 四、用例描述

##### 用例 U-07-003-01

| 项目 | 内容 |
|------|------|
| 用例编号 | U-07-003-01 |
| 用例名称 | 上传素材并自动分类 |
| 业务说明 | 创作者在素材面板选择本地视频/图片/音频上传，服务端保存到 render_uploads/ 并自动分类，列表即时刷新 |
| 规范引用 | _metadata.md 数据类型约定（文件路径 260、文件大小阈值 MB） |
| 业务规则 | 1. 上传必须经 POST /api/upload（multipart files/file 字段），保存目录固定 render_uploads/。2. 服务端按内容自动分类为 video/image/audio 三类，面板按类型分组或标注展示。3. 同名文件冲突时服务端自动加序号 (1)、(2)（S01 规则），面板不得因重名覆盖而丢素材。4. 上传完成后必须刷新素材列表（GET /api/assets），保证列表与磁盘一致。 |
| 使用范围 | R01 视频创作者 |
| 先决条件 | 服务运行于 9002；render_uploads/ 目录可写 |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 4 个基本功能点：|
| | 1. 多文件选择与上传（multipart，经 api/index.js axios 封装） |
| | 2. 上传进度与结果反馈（成功/失败提示） |
| | 3. 上传后自动刷新素材列表并按类型呈现 |
| | 4. 素材元数据展示（文件名/类型/大小/时长） |
| 辅助功能 | 拖拽上传；失败文件单独提示不阻断批次 |
| 提示信息 | 上传成功/失败 toast；分类结果标注 |

**处理逻辑：**
1. 用户选择文件 → FormData 封装；
2. axios POST /api/upload；
3. 响应返回保存路径与分类；
4. 调用 GET /api/assets 刷新 stores/asset.js；
5. 列表重渲染，新素材置顶或按时间排序。

**约束条件：**
1. 上传走浏览器 → 9002 直连，文件大小受本地服务可接受范围约束（大文件建议走 LocalSend 通道）。
2. 面板不实现分类算法，仅消费服务端分类结果。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | 本地文件（multipart form-data） |
| 输出信息 | BO-07-003 素材条目（file_name/file_type/path/size_mb/duration） |

**业务表单：** 素材上传表单（文件选择器 + 提交）

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 上传/列表浏览 | R01 视频创作者（R03 脚本方可直接调 /api/upload 绕过面板） |

##### 用例 U-07-003-02

| 项目 | 内容 |
|------|------|
| 用例编号 | U-07-003-02 |
| 用例名称 | 发起 AI 分析并查看结果 |
| 业务说明 | 创作者选中素材点"分析"，系统调用 S02 感知（元数据/场景/VLM 画面分析/ASR 转录），面板展示结构化结果；重复分析时优先读缓存 |
| 规范引用 | _metadata.md 术语（VLM/ASR/场景检测/质检）；密钥安全（Qwen/ASR Key 服务端持有） |
| 业务规则 | 1. 分析必须先查缓存（GET /api/perceive/cached?path=），命中则直接展示不再计费调用 VLM/ASR。2. 未命中或用户勾选 force 时 POST /api/perceive {path, force, do_asr, frames}。3. 大视频可调小 frames 或 do_asr=false 以避免 VLM/ASR 超时（§19.4），面板须暴露 do_asr/frames 参数。4. 分析结果展示须覆盖 visual_analysis（内容/情绪/质量/亮点/用途/帧内文字）、audio（segments/full_text）、scenes（时间点）、meta（时长/分辨率/帧率）。 |
| 使用范围 | R01 视频创作者 |
| 先决条件 | 素材已在 render_uploads/；服务端可访问 EXT-02 通义千问与 EXT-03 自建 ASR（经 S02） |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 4 个基本功能点：|
| | 1. 缓存探测与结果直读 |
| | 2. 触发全量分析（可配 force/do_asr/frames） |
| | 3. 结构化结果展示（VLM JSON / ASR 词级时间戳 / 场景列表 / 元数据） |
| | 4. 分析状态标记（analysis_cached=true 的素材显示"已分析"） |
| 辅助功能 | 分析中 loading 与超时提示；VLM 质量分（1-10）可视化 |
| 提示信息 | "已分析（读缓存）"；VLM/ASR 超时提示降低 frames 或关 do_asr |

**处理逻辑：**
1. GET /api/perceive/cached?path=；
2. 命中 → 直接渲染结果；未命中 → POST /api/perceive；
3. 展示等待态（分析耗时与抽帧数相关）；
4. 结果写入面板视图；stores/asset.js 更新 analysis_cached。

**约束条件：**
1. 面板不解析 VLM/ASR 原始响应，格式归一化由 S02 负责。
2. 分析属计费 AI 调用，缓存优先是硬规则。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | 素材 path、force/do_asr/frames 选项 |
| 输出信息 | S02 分析结果（visual_analysis/audio/scenes/meta），素材条目 analysis_cached 置位 |

**业务表单：** 素材分析表单（素材选择 + 参数勾选 + 结果区）

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 发起分析/查看结果 | R01 视频创作者 |

##### 用例 U-07-003-03

| 项目 | 内容 |
|------|------|
| 用例编号 | U-07-003-03 |
| 用例名称 | 视频在线预览（内存缓存播放） |
| 业务说明 | 创作者点击视频素材在线播放预览，视频字节经 /api/video/serve 提供，≤50MB 视频命中 RAM 缓存时零磁盘 IO |
| 规范引用 | _metadata.md 术语（LRU 淘汰）；数据类型约定（文件大小阈值：≤50MB 入内存缓存，总上限 500MB） |
| 业务规则 | 1. 预览播放源必须统一为 GET /api/video/serve?path=，不得直读本地文件路径。2. 响应头 X-From-RAM: true 表示命中内存缓存，面板可标注"内存播放"。3. 超过 50MB 的视频走磁盘读取，播放可用但无 RAM 加速，面板不得报错。4. 播放地址仅本机/局域网 9002 服务有效，素材 path 须 URL 编码传递。 |
| 使用范围 | R01 视频创作者 |
| 先决条件 | 素材为 video 类型且存在于 render_uploads/ |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 2 个基本功能点：|
| | 1. 内嵌播放器加载 /api/video/serve 流 |
| | 2. 缓存命中标注（X-From-RAM） |
| 辅助功能 | 图片类素材缩略图直接展示；音频类播放 |
| 提示信息 | 大文件首帧加载慢提示 |

**处理逻辑：**
1. 构造 /api/video/serve?path=<urlencoded>；
2. 播放器加载；
3. 读取响应头 X-From-RAM 更新标注。

**约束条件：**
1. 内存缓存 LRU 淘汰由 S01 负责，面板对未命中静默降级为磁盘播放。
2. 预览属只读操作，不得修改素材。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | 素材 path（query 参数） |
| 输出信息 | 视频字节流（HTTP video/*） |

**业务表单：** 预览播放器弹层

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 预览播放 | R01 视频创作者 |

##### 五、接口依赖

| 方向 | 对接系统 | 接口说明 | 数据格式 |
|------|---------|---------|---------|
| 输出 | S01 素材接入与缓存管理 | POST /api/upload、GET /api/assets、GET /api/video/serve?path= | multipart / JSON / 视频流 |
| 输出 | S02 视频感知与质检 | POST /api/perceive、GET /api/perceive/cached | JSON |
| 输出 | S07-004 LocalSend 接收对话框 | "接收"按钮唤起对话框；接收完成回调刷新 | 组件事件 |
| 输出 | S07-005 对话面板 | 传递选中素材 asset_paths | 组件状态（stores/asset.js） |
| 输入 | EXT-02 通义千问 / EXT-03 自建 ASR / EXT-05 FFprobe | 经 S02 间接使用（分析能力来源） | REST（服务端侧） |

##### 六、需求追溯

| 来源 | 引用 |
|------|------|
| 技术响应表 | 数据源为系统功能手册，对应 _metadata.md S07 功能点索引 S07-003 |
| 技术方案 | SYSTEM_MANUAL.md §15 前端 Web GUI（15.2 组件结构 AssetPanel.vue/stores/asset.js）、§16.2 感知、§16.4 素材与上传、§10.2 视频字节缓存、§17 流程 A |
| 优先级 | P1 |
| ▲标注 | 否 |

---

#### S07-004 LocalSend 接收对话框（启停/状态/已收列表）

##### 一、功能综述

LocalSend 接收对话框（`components/ReceiveDialog.vue`）是素材无线通道的控制台：创作者点开对话框可按需启动/停止 LocalSend 接收端（协议实现属 S01），实时查看接收端状态（设备名/端口 53317/活跃会话/待收文件数/已收文件列表/本机出口 IP），接收完成后已收列表即时呈现并联动素材面板刷新。接收端不随服务常驻——这是明确设计：`start_server(save_dir)` 由前端"接收"按钮按需触发，`stop_server()` 停止并返回本次接收历史（`_received_log`，start 时清空），避免 53317 端口长期占用与官方 LocalSend 客户端冲突。

对话框是手机（EXT-04 LocalSend App，角色 R05 素材发送设备的操作侧）与系统之间的"可视开关+回执单"：手机搜索到设备名"AI 视频工作台"即可发送，对话框侧展示会话与落盘结果；端口被官方 LocalSend 占用时启动返回 409，对话框须将冲突原因呈现给用户。

##### 二、业务对象

| 编码 | 业务对象 | 数据项 | 数据类型 | 长度 | 必填 | 编码引用说明 | 备注 |
|------|---------|--------|---------|------|------|------------|------|
| BO-07-004 | 接收状态 | running | 布尔 | 1 | Y | S01 localsend_recv 运行标志 | 接收端是否在运行 |
| BO-07-004 | 接收状态 | device_name | VARCHAR | 50 | Y | "AI 视频工作台" | 手机 LocalSend App 搜索显示名 |
| BO-07-004 | 接收状态 | port | INT | 5 | Y | 53317（LocalSend v2.2） | 被官方客户端占用时启动 409 |
| BO-07-004 | 接收状态 | active_session | VARCHAR | 36 | N | S01 会话管理 | 单会话（协议要求，并发 409） |
| BO-07-004 | 接收状态 | pending_count | INT | 4 | Y | prepare-upload 待收数 | 会话进行中的待收文件数 |
| BO-07-004 | 接收状态 | received_files | TEXT | - | Y | _received_log JSON 数组 | 本次运行接收历史（start 时清空） |
| BO-07-004 | 接收状态 | local_ip | VARCHAR | 15 | Y | _detect_outbound_ip 出口 IP | 手机须与之同网段 |
| BO-07-004 | 接收状态 | error_code | VARCHAR | 10 | N | 409 等 | 启动失败错误码 |

##### 三、业务活动

1. **启动接收**：对话框点"启动"→ POST /api/localsend/start → S01 按需起 UDP 多播 announce（224.0.0.167:53317）+ HTTP 53317 → 状态轮询刷新。
2. **状态查询**：GET /api/localsend/status 周期轮询（设备名/端口/活跃会话/待收/已收/本机 IP）。
3. **接收呈现**：手机发送文件 → 会话/待收/已收实时变化 → 已收列表逐条展示（文件名/序号重命名结果）。
4. **联动刷新**：接收落盘 render_uploads/ 后通知素材面板（S07-003）刷新列表。
5. **停止接收**：POST /api/localsend/stop → 返回本次接收文件列表 → 对话框汇总展示。
6. **冲突处理**：53317 被占（官方 LocalSend 在跑）返回 409 → 对话框提示关闭占用程序。

##### 四、用例描述

##### 用例 U-07-004-01

| 项目 | 内容 |
|------|------|
| 用例编号 | U-07-004-01 |
| 用例名称 | 按需启动 LocalSend 接收端 |
| 业务说明 | 创作者点"接收"打开对话框并启动接收端，手机 LocalSend App 搜索到"AI 视频工作台"后发送素材 |
| 规范引用 | _metadata.md 术语（LocalSend/多播 announce）；外部系统 EXT-04 |
| 业务规则 | 1. 接收端必须按需启动（POST /api/localsend/start 触发），不得随主服务常驻。2. 端口 53317 被官方 LocalSend 或其他程序占用时启动失败返回 409，对话框须明确提示冲突原因与处理办法（关闭占用程序）。3. 启动成功后必须周期 GET /api/localsend/status 刷新状态（设备名/端口/活跃会话/待收/已收/本机 IP）。4. 手机发现依赖多播 announce 出口网卡正确（S01 IP_MULTICAST_IF 修复），对话框须展示本机 IP 供排障对照（手机连电脑热点时出口 IP=电脑在热点的 IP）。 |
| 使用范围 | R01 视频创作者（对面板）；R05 素材发送设备为对端设备角色 |
| 先决条件 | 服务运行；53317 端口空闲；手机与本机同网段（或连本机热点） |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 4 个基本功能点：|
| | 1. 启动接收端（POST /api/localsend/start，经 api/index.js） |
| | 2. 状态轮询与实时展示（running/设备名/端口/活跃会话/待收数/本机 IP） |
| | 3. 启动失败（409 冲突）错误呈现 |
| | 4. 对话框开关（素材面板"接收"按钮唤起） |
| 辅助功能 | 展示"手机搜索设备名：AI 视频工作台"引导文案 |
| 提示信息 | 409 端口占用提示；启动成功提示设备可被发现 |

**处理逻辑：**
1. 打开对话框 → GET /api/localsend/status 初始化显示；
2. 点启动 → POST /api/localsend/start；
3. 409 → 展示冲突提示；成功 → 进入轮询（间隔秒级）；
4. 状态变化驱动视图更新。

**状态流转：**

| 当前状态 | 事件 | 目标状态 | 前置校验 | 后置动作 |
|---------|------|---------|---------|---------|
| 未运行 | 点击启动且 53317 空闲 | 运行中 | 端口可用 | 开始 announce + 状态轮询 |
| 未运行 | 点击启动且 53317 被占 | 未运行（失败） | 端口检测不通过 | 展示 409 冲突提示 |
| 运行中 | 手机发起会话 | 接收中（active_session 非空） | 单会话约束（并发 409） | 待收/已收实时更新 |
| 接收中 | 会话完成 | 运行中（会话清空） | — | 已收列表追加，联动素材面板刷新 |

**约束条件：**
1. 对话框不实现协议（发现/传输/SHA256/重名序号属 S01），仅消费状态 API。
2. 同一时间仅一个活跃会话（协议要求），对话框须正确展示会话占用而非报错重试。
3. 关闭对话框不等于停止接收（停止须显式操作），避免误停正在进行的接收。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | 启停操作事件；GET /api/localsend/status 响应 |
| 输出信息 | BO-07-004 接收状态（running/device_name/port/active_session/pending_count/received_files/local_ip） |

**业务表单：** LocalSend 接收对话框（启动/停止按钮 + 状态区 + 已收列表）

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 启停接收端/查看状态 | R01 视频创作者（R04 运维可借其排障多播网卡问题） |

##### 用例 U-07-004-02

| 项目 | 内容 |
|------|------|
| 用例编号 | U-07-004-02 |
| 用例名称 | 查看已收列表并停止接收 |
| 业务说明 | 接收完成后创作者查看已收文件列表，点"停止"结束接收端，系统返回本次接收文件清单 |
| 规范引用 | 无 |
| 业务规则 | 1. 已收列表数据源为 _received_log（start_server 时清空），仅覆盖本次运行，不含历史批次。2. 停止必须调 POST /api/localsend/stop，返回值即本次接收文件列表，对话框须以其为最终回执展示。3. 停止后不再占用 53317 端口，官方 LocalSend 客户端可正常使用。4. 接收文件落盘 render_uploads/（同名冲突自动加序号），停止后素材面板须刷新可见。 |
| 使用范围 | R01 视频创作者 |
| 先决条件 | 接收端处于运行中 |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 3 个基本功能点：|
| | 1. 已收文件列表实时展示（文件名/接收状态） |
| | 2. 停止接收（POST /api/localsend/stop）并展示返回的接收汇总 |
| | 3. 停止后联动素材面板刷新 |
| 辅助功能 | 接收历史下载入口（转素材面板对应条目） |
| 提示信息 | 停止成功提示本次接收 N 个文件 |

**处理逻辑：**
1. 轮询 status 更新已收列表；
2. 用户点停止 → POST /api/localsend/stop；
3. 展示返回的接收文件列表汇总；
4. 触发素材面板刷新（GET /api/assets）。

**约束条件：**
1. 活跃会话进行中停止须提示可能中断待收文件。
2. 停止是幂等操作（未运行时停止不应报错）。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | 停止操作；/api/localsend/stop 响应（接收文件列表） |
| 输出信息 | BO-07-004 接收状态（running=false + received_files 最终清单） |

**业务表单：** LocalSend 接收对话框（同上）

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 查看/停止接收 | R01 视频创作者 |

##### 五、接口依赖

| 方向 | 对接系统 | 接口说明 | 数据格式 |
|------|---------|---------|---------|
| 输出 | S01 素材接入与缓存管理 | GET /api/localsend/status、POST /api/localsend/start、POST /api/localsend/stop | HTTP JSON |
| 输入 | EXT-04 LocalSend 客户端 App | 经 S01 间接对接（UDP 多播 + HTTP 53317），对话框为其人控开关 | LocalSend v2.2 协议 |
| 输出 | S07-003 素材面板 | 接收完成事件联动刷新 | 组件事件 / stores/asset.js |

##### 六、需求追溯

| 来源 | 引用 |
|------|------|
| 技术响应表 | 数据源为系统功能手册，对应 _metadata.md S07 功能点索引 S07-004 |
| 技术方案 | SYSTEM_MANUAL.md §15.2 组件结构 ReceiveDialog.vue、§11.4 生命周期、§16.8 LocalSend、§19.3 故障排查、§17 流程 A |
| 优先级 | P1 |
| ▲标注 | 否 |

---

#### S07-005 对话面板（SSE 流式渲染）

##### 一、功能综述

对话面板（`components/ChatPanel.vue` + `stores/chat.js`）把"用自然语言剪视频"的对话式生产能力（S04）呈现在浏览器：创作者输入指令（如"帮我把这个视频配个标题并渲染"），POST /api/chat {message, draft_id?, asset_paths?} 建立 SSE 连接，服务端 LLM（EXT-02 通义千问，OpenAI 兼容接口）经 function calling 依次调用 list_resources/get_resource_detail/get_transcript/create_draft/add_video/add_text/save_draft/render 等工具，过程以 SSE 事件流式推回：`data: {"text": "..."}` 增量文本、`data: {"tool": "...", "args": {...}, "result": {...}}` 工具调用透明化、`data: {"draft_id": "..."}` 草稿上下文锚定、`data: [DONE]` 结束。面板用 marked 渲染 markdown 增量气泡，让创作者实时看到"AI 在调什么工具、拿到了什么结果"。

draft_id 贯穿多轮对话（S04-005）：首条建草稿消息返回的 draft_id 存入 stores，后续消息自动携带，形成"同一草稿上持续编辑"的会话上下文；素材面板（S07-003）选中的 asset_paths 作为生产原料随消息传递。

##### 二、业务对象

| 编码 | 业务对象 | 数据项 | 数据类型 | 长度 | 必填 | 编码引用说明 | 备注 |
|------|---------|--------|---------|------|------|------------|------|
| BO-07-005 | 对话消息 | msg_id | VARCHAR | 8 | Y | 前端生成 | 气泡标识 |
| BO-07-005 | 对话消息 | role | VARCHAR | 10 | Y | user / assistant / tool_event | tool_event 为工具调用透明化气泡 |
| BO-07-005 | 对话消息 | content | TEXT | - | Y | markdown 文本 | marked 渲染，SSE text 增量拼接 |
| BO-07-005 | 对话消息 | draft_id | VARCHAR | 36 | N | S04/S05 草稿 UUID | 贯穿多轮的草稿上下文 |
| BO-07-005 | 对话消息 | tool_name | VARCHAR | 50 | N | S04 工具名 | create_draft/add_video/add_text/save_draft/render 等 |
| BO-07-005 | 对话消息 | tool_args | TEXT | - | N | 工具参数 JSON | SSE tool 事件 args |
| BO-07-005 | 对话消息 | tool_result | TEXT | - | N | 工具结果 JSON | SSE tool 事件 result |
| BO-07-005 | 对话消息 | timestamp | BIGINT | 毫秒 | Y | Unix 毫秒 | 气泡时序 |

##### 三、业务活动

1. **发送消息**：输入指令 → POST /api/chat {message, draft_id?, asset_paths?} → 建立 SSE 流。
2. **流式渲染**：逐 data 事件处理——text 增量追加当前 assistant 气泡；tool 事件生成工具调用气泡（名称/参数/结果折叠展示）；draft_id 事件更新 stores/chat.js 草稿上下文；[DONE] 结束本轮。
3. **草稿上下文维持**：draft_id 存 store，后续消息自动带上，实现同草稿多轮编辑。
4. **素材引用**：从素材面板带入 asset_paths，让 LLM 可 list_resources/get_resource_detail 获取分析详情。
5. **跨面板联动**：草稿创建后联动草稿面板（S07-006）刷新；render 工具触发后联动渲染面板（S07-007）出现新任务。

##### 四、用例描述

##### 用例 U-07-005-01

| 项目 | 内容 |
|------|------|
| 用例编号 | U-07-005-01 |
| 用例名称 | 发起流式对话并接收 SSE 事件 |
| 业务说明 | 创作者输入"帮我把这个视频配个标题并渲染"，面板流式展示 AI 回复与工具调用过程直至 [DONE] |
| 规范引用 | _metadata.md 术语（SSE/Function Calling/VLM） |
| 业务规则 | 1. 对话必须以 POST /api/chat {message, draft_id?, asset_paths?} 发起，响应为 SSE 流（text/event-stream）。2. SSE 事件须按四种形态处理：{"text"}、{"tool","args","result"}、{"draft_id"}、[DONE]，未识别事件不得中断渲染。3. assistant 文本为 markdown，须经 marked 渲染为富文本气泡。4. 会话结束以 [DONE] 为准，连接异常断开须提示且不丢已收内容。 |
| 使用范围 | R01 视频创作者 |
| 先决条件 | 服务运行；服务端可访问 EXT-02 通义千问；若需编辑能力则 S07-001 融合模式（FUSED_VC=True） |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 5 个基本功能点：|
| | 1. 消息输入与发送（携带 draft_id/asset_paths） |
| | 2. SSE 连接建立与事件分发（fetch/EventSource 流式读取） |
| | 3. 增量 markdown 渲染（marked） |
| | 4. 工具调用气泡（tool_name/args/result 折叠展示） |
| | 5. [DONE] 收尾与滚动跟随 |
| 辅助功能 | 发送中禁用输入防重入；历史气泡持久于 stores/chat.js 会话期 |
| 提示信息 | 连接断开/超时提示；融合降级下编辑类工具失败提示 |

**处理逻辑：**
1. 组装 {message, draft_id?, asset_paths?} POST /api/chat；
2. 读 SSE 流：按行解析 data: 前缀 JSON；
3. text → 追加气泡内容并增量渲染；tool → 追加工具气泡；draft_id → 更新 store；
4. [DONE] → 标记本轮完成，恢复输入。

**约束条件：**
1. 面板不实现 function calling 决策（S04 服务端 LLM 职责），仅忠实呈现事件。
2. 流式期间不得整页刷新（SPA 状态保持）。
3. dev 模式（5173）下 SSE 依赖 Vite 代理透传流式响应。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | 用户消息文本、draft_id（可空）、asset_paths（可空） |
| 输出信息 | BO-07-005 对话消息序列（user/assistant/tool_event 气泡） |

**业务表单：** 对话面板（消息输入框 + 流式气泡区）

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 发起对话/查看流式结果 | R01 视频创作者 |

##### 用例 U-07-005-02

| 项目 | 内容 |
|------|------|
| 用例编号 | U-07-005-02 |
| 用例名称 | 草稿上下文贯穿与跨面板联动 |
| 业务说明 | 首轮对话创建草稿获得 draft_id 后，后续多轮编辑自动作用于同一草稿；草稿/渲染面板同步刷新 |
| 规范引用 | 无 |
| 业务规则 | 1. draft_id 事件到达后必须写入 stores/chat.js，后续每条消息自动携带。2. draft_id 产生/变化须联动草稿管理面板（S07-006）刷新列表（新草稿经 save_draft 落盘后可见）。3. render 工具成功触发后须联动渲染任务面板（S07-007）刷新任务列表。4. 用户可显式清空 draft_id 开启新草稿会话（新一轮 create_draft）。 |
| 使用范围 | R01 视频创作者 |
| 先决条件 | 已完成至少一轮创建草稿对话（FUSED_VC=True） |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 3 个基本功能点：|
| | 1. draft_id 捕获与 store 持久（会话级） |
| | 2. 后续消息自动携带 draft_id |
| | 3. 草稿/渲染面板联动刷新 |
| 辅助功能 | 当前关联草稿标识展示（气泡头部） |
| 提示信息 | 切换新草稿提示 |

**处理逻辑：**
1. 监听 SSE draft_id 事件 → store 写入；
2. 下一条消息组装时从 store 读取携带；
3. save_draft/render 工具成功事件 → 触发对应面板刷新。

**约束条件：**
1. draft_id 为会话级状态，刷新页面后丢失（无持久化要求）。
2. 同一 draft_id 在对话中持续使用不得被前端擅自改写。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | SSE {"draft_id"} 事件 |
| 输出信息 | BO-07-005 对话消息（draft_id 字段）；stores/chat.js 草稿上下文 |

**业务表单：** 对话面板（同上）

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 多轮草稿编辑对话 | R01 视频创作者 |

##### 五、接口依赖

| 方向 | 对接系统 | 接口说明 | 数据格式 |
|------|---------|---------|---------|
| 输出 | S04 对话式生产 | POST /api/chat {message, draft_id?, asset_paths?} | SSE（text/event-stream，data 事件 JSON） |
| 输出 | S05 草稿编辑服务 / S06 无人值守渲染 | 经 S04 工具链间接调用（create_draft/add_video/add_text/save_draft/render） | HTTP JSON（服务端侧） |
| 输入 | EXT-02 通义千问 qwen3.7-plus | 经 S04 间接（LLM function calling 决策来源） | REST（OpenAI 兼容） |
| 输出 | S07-003 素材面板 / S07-006 草稿面板 / S07-007 渲染面板 | asset_paths 来源；draft/render 联动刷新 | 组件状态（Pinia stores） |

##### 六、需求追溯

| 来源 | 引用 |
|------|------|
| 技术响应表 | 数据源为系统功能手册，对应 _metadata.md S07 功能点索引 S07-005 |
| 技术方案 | SYSTEM_MANUAL.md §15.2 组件结构 ChatPanel.vue/stores/chat.js、§16.6 对话（SSE 流式）、§17 流程 A |
| 优先级 | P1 |
| ▲标注 | 否 |

---

#### S07-006 草稿管理面板（列表/封面/删除）

##### 一、功能综述

草稿管理面板（`components/DraftPanel.vue` + `stores/project.js`）提供仿 CapCut 剪映草稿管理的浏览界面：以 GET /api/drafts 列出剪映草稿目录（DRAFT_ROOT）下所有草稿（含封面、时长、创建/修改时间），封面经 GET /api/cover?folder= 按需加载，删除操作经 DELETE /api/drafts/<folder> 执行并连带 root_meta 同步（服务端逻辑属 S07-008）。面板让创作者在浏览器里即可掌握"剪映里有哪些草稿"——包括对话面板（S07-005）经编辑工具生成的草稿、模板引擎（S03）组装的草稿、以及 S06 渲染注入产生/清理的 rd* 草稿痕迹。

面板是 S07-008 API 的前端消费者：列表条目按修改时间排序（tm_draft_modified），封面缺失时有占位样式，删除须二次确认（删除不可恢复且同步改写 root_meta_info.json）。

##### 二、业务对象

| 编码 | 业务对象 | 数据项 | 数据类型 | 长度 | 必填 | 编码引用说明 | 备注 |
|------|---------|--------|---------|------|------|------------|------|
| BO-07-006 | 草稿条目 | folder | VARCHAR | 64 | Y | DRAFT_ROOT 下文件夹名 | 删除 API 的路径参数 |
| BO-07-006 | 草稿条目 | draft_id | VARCHAR | 36 | Y | draft_meta_info.json | UUID |
| BO-07-006 | 草稿条目 | draft_name | VARCHAR | 32 | Y | draft_meta_info.json | 展示名 |
| BO-07-006 | 草稿条目 | duration | DECIMAL | (10,3) | N | 秒 | 草稿时长 |
| BO-07-006 | 草稿条目 | cover_url | VARCHAR | 255 | N | /api/cover?folder= | 封面图 URL |
| BO-07-006 | 草稿条目 | tm_draft_create | BIGINT | 毫秒 | N | 剪映草稿元数据约定 | 创建时间 |
| BO-07-006 | 草稿条目 | tm_draft_modified | BIGINT | 毫秒 | Y | 剪映草稿元数据约定 | 修改时间，列表排序键 |
| BO-07-006 | 草稿条目 | draft_fold_path | VARCHAR | 260 | N | 草稿文件夹绝对路径 | 排障用 |

##### 三、业务活动

1. **列表加载**：进入面板/刷新 → GET /api/drafts → 写入 stores/project.js → 按修改时间倒序渲染卡片。
2. **封面加载**：每张卡片经 cover_url 懒加载封面（GET /api/cover?folder=）。
3. **删除草稿**：卡片删除按钮 → 二次确认 → DELETE /api/drafts/<folder> → 服务端删文件夹 + root_meta 同步 → 刷新列表。
4. **联动刷新**：S07-005 对话 save_draft 落盘、S03 模板执行后刷新可见；S06 渲染清理 rd* 草稿后列表同样反映。
5. **渲染入口**：对草稿面板中的草稿可发起按草稿 ID 渲染（POST /render/draft/<draft_id>，联动 S07-007）。

##### 四、用例描述

##### 用例 U-07-006-01

| 项目 | 内容 |
|------|------|
| 用例编号 | U-07-006-01 |
| 用例名称 | 浏览草稿列表与封面 |
| 业务说明 | 创作者打开草稿面板，查看剪映草稿目录下全部草稿的名称、封面、时长、创建/修改时间 |
| 规范引用 | _metadata.md 术语（草稿/root_meta_info.json）；数据类型约定（tm_draft_modified 毫秒） |
| 业务规则 | 1. 列表数据必须来自 GET /api/drafts（仿 CapCut 草稿管理，含封面/时长/创建/修改时间），不得前端直读磁盘。2. 默认按 tm_draft_modified 倒序排列（最近修改在前）。3. 封面必须按需经 /api/cover?folder= 加载，无封面草稿显示占位图，不得因单条封面 404 阻断整列表。4. 面板须能看到全部草稿来源（VectCutAPI 生成、模板引擎组装、剪映手工创建、S06 注入残留 rd*），与剪映首页草稿清单一致。 |
| 使用范围 | R01 视频创作者 |
| 先决条件 | 服务运行；剪映草稿根目录存在且可读 |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 4 个基本功能点：|
| | 1. 草稿列表加载与卡片渲染（名称/时长/时间） |
| | 2. 封面懒加载（cover_url） |
| | 3. 按修改时间排序 |
| | 4. 列表刷新（手动 + 对话/模板产草稿后联动） |
| 辅助功能 | 空状态提示（无草稿）；rd* 注入残留草稿可识别 |
| 提示信息 | 加载失败提示 |

**处理逻辑：**
1. GET /api/drafts；
2. 响应写入 stores/project.js；
3. 渲染卡片列表，逐卡片发起封面请求；
4. 排序键 tm_draft_modified 倒序。

**约束条件：**
1. 面板只读呈现，不解析 draft_content.json 内容。
2. 封面加载失败静默降级为占位图。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | GET /api/drafts 响应、GET /api/cover 响应（图片字节） |
| 输出信息 | BO-07-006 草稿条目列表视图 |

**业务表单：** 草稿管理面板（卡片列表）

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 浏览草稿列表/封面 | R01 视频创作者 |

##### 用例 U-07-006-02

| 项目 | 内容 |
|------|------|
| 用例编号 | U-07-006-02 |
| 用例名称 | 删除草稿（二次确认 + root_meta 同步提示） |
| 业务说明 | 创作者删除不需要的草稿，系统删除草稿文件夹并同步 root_meta_info.json，防止剪映下次启动弹"草稿丢失"对话框 |
| 规范引用 | _metadata.md 术语（root_meta_info.json：注入草稿清理时须同步移除条目）；SYSTEM_MANUAL §19.2 |
| 业务规则 | 1. 删除必须二次确认（对话框列出草稿名，明确"删除后不可恢复"）。2. 删除调用 DELETE /api/drafts/<folder>，服务端负责删除文件夹与 root_meta_info.json 的 all_draft_store 条目同步移除（S07-008 规则，面板不得只删文件夹）。3. 删除成功后必须刷新列表，删除失败（文件占用/权限）须展示服务端错误。4. 正在渲染中的草稿（对应 S06 任务 rendering 态）不得删除，须提示先等渲染完成。 |
| 使用范围 | R01 视频创作者 |
| 先决条件 | 草稿存在于 DRAFT_ROOT；无进行中渲染任务占用 |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 3 个基本功能点：|
| | 1. 删除按钮 + 二次确认对话框 |
| | 2. 调用 DELETE /api/drafts/<folder> |
| | 3. 成功/失败反馈并刷新列表 |
| 辅助功能 | 渲染中草稿禁删标识 |
| 提示信息 | "删除后不可恢复"；删除失败原因透传 |

**处理逻辑：**
1. 点击删除 → 确认对话框；
2. 确认 → DELETE /api/drafts/<folder>；
3. 2xx → 刷新列表；非 2xx → 展示错误信息。

**约束条件：**
1. 面板不直接操作文件系统，全部删除语义委托 S07-008。
2. 删除操作幂等性由服务端保证（文件夹不存在返回可重试语义）。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | folder（路径参数） |
| 输出信息 | BO-07-006 草稿条目（移除）；BO-07-010 root_meta 索引条目（服务端同步移除） |

**业务表单：** 草稿卡片删除确认框

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 删除草稿 | R01 视频创作者（R04 运维可借其清理异常残留 rd* 草稿） |

##### 五、接口依赖

| 方向 | 对接系统 | 接口说明 | 数据格式 |
|------|---------|---------|---------|
| 输出 | S07-008 草稿管理 API | GET /api/drafts、GET /api/cover?folder=、DELETE /api/drafts/<folder> | HTTP JSON / 图片 |
| 输出 | S06 无人值守渲染 | POST /render/draft/<draft_id>（面板发起按草稿渲染） | HTTP JSON |
| 输出 | S07-005 对话面板 / S03 模板引擎 | save_draft / 模板执行后联动刷新 | 组件状态（stores/project.js） |
| 输入 | EXT-01 剪映专业版 | 草稿目录与 root_meta_info.json 数据源（经 S07-008 间接读取） | 文件系统 JSON |

##### 六、需求追溯

| 来源 | 引用 |
|------|------|
| 技术响应表 | 数据源为系统功能手册，对应 _metadata.md S07 功能点索引 S07-006 |
| 技术方案 | SYSTEM_MANUAL.md §15.2 组件结构 DraftPanel.vue/stores/project.js、§16.3 草稿管理、§19.2"草稿丢失"排查 |
| 优先级 | P1 |
| ▲标注 | 否 |

---

#### S07-007 渲染任务面板（提交/状态/下载）

##### 一、功能综述

渲染任务面板（`components/RenderPanel.vue` + `stores/render.js`）是无人值守渲染（S06）的指挥台：创作者提交渲染任务（zip 草稿直传 POST /render，或按草稿 ID POST /render/draft/<draft_id>），系统返回 task_id 后面板进入轮询——周期 GET /render/status/<task_id> 跟踪 queued → rendering → done/error 状态机，GET /render/list 汇总全部任务；done 后经 GET /render/download/<task_id> 下载成片 mp4。任务由 S06 多桌面渲染池（JYRender_0/JYRender_1 两路并行 + 阻塞队列）实际执行，面板只消费任务 API。

面板覆盖生产线的最后一公里："提交 → 排队 → 真后台渲染（用户主桌面无感）→ done → 下载"，并承接 S07-005 对话面板 render 工具触发的任务（自动出现在列表）。error 态展示错误信息，供按 §19.2 排查（calib 缺坐标、modal 时序、dev 未获取等）。

##### 二、业务对象

| 编码 | 业务对象 | 数据项 | 数据类型 | 长度 | 必填 | 编码引用说明 | 备注 |
|------|---------|--------|---------|------|------|------------|------|
| BO-07-007 | 渲染任务视图 | task_id | VARCHAR(Hex) | 8 | Y | uuid hex 前 8 位（S06 约定） | 轮询/下载主键 |
| BO-07-007 | 渲染任务视图 | status | VARCHAR | 10 | Y | queued / rendering / done / error | S06 任务生命周期 |
| BO-07-007 | 渲染任务视图 | draft_name | VARCHAR | 32 | N | 草稿名（zip 内或按 ID 解析） | 列表展示 |
| BO-07-007 | 渲染任务视图 | mp4_name | VARCHAR | 255 | N | done 后产物名 | 查找：先 draft_name*.mp4 再 rd*.mp4 |
| BO-07-007 | 渲染任务视图 | duration | DECIMAL | (10,3) | N | 秒 | done 后回填 |
| BO-07-007 | 渲染任务视图 | download_url | VARCHAR | 255 | N | /render/download/<task_id> | done 后可用 |
| BO-07-007 | 渲染任务视图 | message | TEXT | - | N | error 时错误信息 | 排障依据 |

##### 三、业务活动

1. **提交 zip 直传渲染**：选择本地草稿 zip（内含 draft_content.json）→ POST /render（multipart draft + draft_name 可选）→ 获得 task_id。
2. **按草稿 ID 渲染**：对草稿面板/对话产生的 draft_id → POST /render/draft/<draft_id> → 获得 task_id。
3. **状态轮询**：周期 GET /render/status/<task_id> 刷新任务卡片（状态徽标/产物名/时长）。
4. **任务总览**：GET /render/list 列出全部任务（含历史）。
5. **下载成片**：done 后 GET /render/download/<task_id> 保存 mp4。
6. **异常呈现**：error 任务展示 message，引导重试或转 §19.2 排查。

##### 四、用例描述

##### 用例 U-07-007-01

| 项目 | 内容 |
|------|------|
| 用例编号 | U-07-007-01 |
| 用例名称 | 提交渲染任务（zip 直传 / 按草稿 ID） |
| 业务说明 | 创作者将草稿 zip 直传渲染（最快路径），或对已有 draft_id 发起按草稿渲染，任务进入 S06 队列 |
| 规范引用 | _metadata.md 术语（多桌面池/真后台/草稿注入）；数据类型约定（task_id 8 位 hex） |
| 业务规则 | 1. zip 直传必须校验 zip 内含 draft_content.json（草稿文件夹结构），不合格须前置提示而非入队后报错。2. 提交响应必须包含 task_id 与 poll 轮询地址（/render/status/<task_id>），面板即入轮询。3. 渲染是异步的：提交即返回，禁止面板阻塞等待完成。4. 按 draft_id 渲染（POST /render/draft/<id>）仅接受 VectCutAPI 生成的 draft_id（融合模式可用；降级模式下该通道不可用，仅 zip 直传可用）。 |
| 使用范围 | R01 视频创作者；R03 脚本/程序调用方（同一端点的 curl 形态，流程 D） |
| 先决条件 | 服务运行；zip 直传须有合规草稿 zip；按 ID 渲染须草稿已落盘且 FUSED_VC=True |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 4 个基本功能点：|
| | 1. zip 文件选择与直传提交（POST /render multipart） |
| | 2. 按草稿 ID 提交（POST /render/draft/<draft_id>） |
| | 3. 提交结果（task_id）写入 stores/render.js 并入轮询 |
| | 4. zip 结构不合规前置校验提示 |
| 辅助功能 | 对话面板 render 工具触发的任务自动并入列表 |
| 提示信息 | 提交成功（含 task_id）；降级模式下按 ID 渲染不可用提示 |

**处理逻辑：**
1. 用户选择 zip 或指定 draft_id；
2. 前端基本校验（zip 可选性/文件存在）；
3. POST /render 或 /render/draft/<id>；
4. 响应 {task_id, poll} → 加入任务列表 → 启动轮询。

**约束条件：**
1. 渲染并行度由 S06 桌面池决定（2 路），面板不做并发控制。
2. 大 zip 上传经浏览器直传 9002，须容忍上传耗时。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | 草稿 zip（multipart draft + draft_name）或 draft_id（路径参数） |
| 输出信息 | BO-07-007 渲染任务视图（task_id/status=queued） |

**业务表单：** 渲染提交表单（zip 选择器 / 草稿选择）

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 提交渲染任务 | R01 视频创作者、R03 脚本/程序调用方 |

##### 用例 U-07-007-02

| 项目 | 内容 |
|------|------|
| 用例编号 | U-07-007-02 |
| 用例名称 | 轮询任务状态并下载成片 |
| 业务说明 | 面板周期轮询任务状态，渲染完成后展示产物并提供 mp4 下载 |
| 规范引用 | _metadata.md 数据类型约定（状态枚举 queued/rendering/done/error） |
| 业务规则 | 1. 状态轮询必须以 GET /render/status/<task_id> 进行，间隔秒级，done/error 为终态即停止该任务轮询。2. done 后必须经 GET /render/download/<task_id> 下载（产物名与时长由 S06 回填：先找 draft_name*.mp4 再退而找 rd*.mp4）。3. error 任务必须展示服务端错误信息并保留在列表中供排障，不得自动清除。4. 任务总览经 GET /render/list 呈现全部任务（队列中/渲染中/已完成/失败分组或标注）。 |
| 使用范围 | R01 视频创作者；R03 脚本/程序调用方（curl 轮询下载） |
| 先决条件 | 已有 task_id（U-07-007-01） |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 4 个基本功能点：|
| | 1. 状态轮询与卡片实时刷新（状态徽标 queued/rendering/done/error） |
| | 2. 任务列表总览（GET /render/list） |
| | 3. done 后下载 mp4（download_url） |
| | 4. error 信息展示与留存 |
| 辅助功能 | 任务耗时展示；下载完成 toast |
| 提示信息 | 渲染中等待提示（真后台约 35-60s/条）；失败重试引导 |

**处理逻辑：**
1. 定时器轮询各未终态任务 /render/status/<id>；
2. 状态变化更新卡片；
3. done → 显示下载按钮（mp4_name/duration）；
4. error → 展示 message，停止轮询。

**状态流转：**

| 当前状态 | 事件 | 目标状态 | 前置校验 | 后置动作 |
|---------|------|---------|---------|---------|
| （无） | 提交任务 | queued | zip 合规 / draft_id 有效 | 获得 task_id，开始轮询 |
| queued | 桌面池 worker 取任务 | rendering | acquire_desktop 成功（S06） | 卡片转"渲染中" |
| rendering | 完成检测通过（S06） | done | mp4 >100KB 且大小稳定 | 回填 mp4_name/duration，出下载按钮 |
| rendering | 重试耗尽/异常 | error | S06 判定失败 | 展示 message，停止轮询 |

**约束条件：**
1. 轮询请求量与任务数线性相关，终态任务必须停轮询。
2. 下载依赖任务产物保留（S06 产物登记），面板不缓存文件。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | task_id（路径参数） |
| 输出信息 | BO-07-007 渲染任务视图（status/mp4_name/duration/download_url/message） |

**业务表单：** 渲染任务面板（任务卡片列表 + 下载操作）

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 轮询状态/下载成片 | R01 视频创作者、R03 脚本/程序调用方 |

##### 五、接口依赖

| 方向 | 对接系统 | 接口说明 | 数据格式 |
|------|---------|---------|---------|
| 输出 | S06 无人值守渲染 | POST /render、POST /render/draft/<draft_id>、GET /render/status/<task_id>、GET /render/download/<task_id>、GET /render/list | multipart / JSON / mp4 流 |
| 输出 | S07-005 对话面板 | render 工具触发的任务并入列表 | 组件状态（stores/render.js） |
| 输入 | EXT-01 剪映专业版 | 渲染执行后端（经 S06 间接） | Frida + Win32（服务端侧） |

##### 六、需求追溯

| 来源 | 引用 |
|------|------|
| 技术响应表 | 数据源为系统功能手册，对应 _metadata.md S07 功能点索引 S07-007 |
| 技术方案 | SYSTEM_MANUAL.md §15.2 组件结构 RenderPanel.vue/stores/render.js、§5.3 任务生命周期、§16.1 渲染、§17 流程 A/D、§19.2 |
| 优先级 | P1 |
| ▲标注 | 否 |

---

### 2.3 服务端支撑 API 与服务可用性

#### S07-008 草稿管理 API（/api/drafts 列表/封面/删除 + root_meta 同步）

##### 一、功能综述

草稿管理 API 是 S07 自研的三个服务端端点（GET /api/drafts、GET /api/cover、DELETE /api/drafts/<folder>），为 S07-006 草稿管理面板（及 R03 脚本调用方）提供仿 CapCut 草稿管理能力：枚举剪映草稿根目录（DRAFT_ROOT）下所有草稿文件夹，从 draft_meta_info.json 提取草稿名/时长/创建/修改时间，聚合为列表 JSON；按文件夹取草稿封面图字节；删除草稿时执行"删文件夹 + root_meta 同步"两步原子语义——从剪映草稿根的全局索引 root_meta_info.json 的 all_draft_store 中移除对应条目，避免剪映下次启动弹出"草稿丢失"对话框阻塞自动化流程。

root_meta 同步是该 API 的关键设计：剪映以 root_meta_info.json 维护草稿索引，仅删除文件夹而不清理索引会导致索引悬空，剪映启动时检测到"索引有而磁盘无"即弹丢失对话框——这对无人值守渲染（S06，§19.2 已知坑）是致命干扰。删除 API 在服务端一次性完成两步，前端与脚本调用方无需关心索引细节。

##### 二、业务对象

| 编码 | 业务对象 | 数据项 | 数据类型 | 长度 | 必填 | 编码引用说明 | 备注 |
|------|---------|--------|---------|------|------|------------|------|
| BO-07-006 | 草稿条目 | folder | VARCHAR | 64 | Y | DRAFT_ROOT 下文件夹名 | 删除路径参数 |
| BO-07-006 | 草稿条目 | draft_id | VARCHAR | 36 | Y | draft_meta_info.json 内 UUID | root_meta 匹配键 |
| BO-07-006 | 草稿条目 | draft_name | VARCHAR | 32 | Y | draft_meta_info.json | 列表展示 |
| BO-07-006 | 草稿条目 | duration | DECIMAL | (10,3) | N | 秒 | 时长 |
| BO-07-006 | 草稿条目 | cover_url | VARCHAR | 255 | N | /api/cover?folder= | 前端懒加载 |
| BO-07-006 | 草稿条目 | tm_draft_create | BIGINT | 毫秒 | N | 剪映元数据约定 | 创建时间 |
| BO-07-006 | 草稿条目 | tm_draft_modified | BIGINT | 毫秒 | Y | 剪映元数据约定 | 排序键 |
| BO-07-006 | 草稿条目 | draft_fold_path | VARCHAR | 260 | N | 绝对路径 | — |
| BO-07-010 | root_meta 索引条目 | draft_id | VARCHAR | 36 | Y | root_meta_info.json/all_draft_store | 与草稿条目匹配键 |
| BO-07-010 | root_meta 索引条目 | draft_name | VARCHAR | 32 | Y | 同上 | 索引内草稿名 |
| BO-07-010 | root_meta 索引条目 | draft_fold_path | VARCHAR | 260 | Y | 同上 | 索引内路径 |
| BO-07-010 | root_meta 索引条目 | tm_draft_modified | BIGINT | 毫秒 | Y | 同上 | 索引时间戳 |
| BO-07-010 | root_meta 索引条目 | sync_action | VARCHAR | 10 | Y | remove | 删除草稿时同步动作 |

##### 三、业务活动

1. **列表查询**：扫描 DRAFT_ROOT 子文件夹 → 逐个读 draft_meta_info.json → 聚合 {folder, draft_id, draft_name, duration, cover_url, tm_draft_create, tm_draft_modified} 列表。
2. **封面获取**：按 folder 定位草稿封面文件 → 以图片字节响应（无封面返回占位/404 语义）。
3. **删除草稿**：校验 folder 合法（防路径穿越）→ 删除草稿文件夹 → 读取 root_meta_info.json → 从 all_draft_store 移除该草稿条目 → 写回。
4. **索引一致性维护**：删除失败中途（如文件夹已删而索引未写）时，以索引为幂等基准重试。

##### 四、用例描述

##### 用例 U-07-008-01

| 项目 | 内容 |
|------|------|
| 用例编号 | U-07-008-01 |
| 用例名称 | 查询草稿列表与封面 |
| 业务说明 | 前端/脚本调用 GET /api/drafts 获得全部草稿元数据列表，再按需 GET /api/cover?folder= 取封面 |
| 规范引用 | _metadata.md 数据类型约定（tm_draft_modified 毫秒、文件路径 260） |
| 业务规则 | 1. 列表必须覆盖 DRAFT_ROOT 下全部草稿文件夹（含 VectCutAPI 生成、模板组装、剪映手工创建、S06 注入 rd* 残留），单个草稿 meta 解析失败不得阻断整体列表（跳过并容错）。2. 每条目须含封面 URL（前端懒加载）、时长、创建/修改时间，行为仿 CapCut 草稿管理。3. 封面端点按 folder 参数定位，无封面草稿须返回明确的空语义（占位或 404），不得 500。4. folder 参数须做路径合法性校验，禁止穿越 DRAFT_ROOT 之外的路径。 |
| 使用范围 | R01 视频创作者（经 S07-006 面板）；R03 脚本/程序调用方（直接 GET） |
| 先决条件 | DRAFT_ROOT 存在且可读 |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 3 个基本功能点：|
| | 1. GET /api/drafts 返回草稿元数据列表 |
| | 2. GET /api/cover?folder= 返回封面图字节 |
| | 3. 单草稿解析失败容错（跳过不阻断） |
| 辅助功能 | 列表天然按扫描聚合，排序由前端按 tm_draft_modified 处理 |
| 提示信息 | 无封面/无草稿的空语义 |

**处理逻辑：**
1. 枚举 DRAFT_ROOT 子目录；
2. 逐个读 draft_meta_info.json 提取字段（异常跳过）；
3. 组装列表 JSON 返回；
4. 封面请求按 folder 定位封面文件回传字节。

**约束条件：**
1. 只读操作，不修改任何草稿文件与索引。
2. 列表响应体积与草稿数线性，无分页硬需求（本地单机草稿量级有限）。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | DRAFT_ROOT 文件系统、draft_meta_info.json |
| 输出信息 | BO-07-006 草稿条目列表（JSON）/ 封面图片字节 |

**业务表单：** 无（REST API）

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 草稿列表/封面查询 | R01 视频创作者（经面板）、R03 脚本/程序调用方 |

##### 用例 U-07-008-02

| 项目 | 内容 |
|------|------|
| 用例编号 | U-07-008-02 |
| 用例名称 | 删除草稿与 root_meta 同步 |
| 业务说明 | 调用 DELETE /api/drafts/<folder>，服务端删除草稿文件夹并同步移除 root_meta_info.json 的 all_draft_store 条目，防止剪映弹"草稿丢失" |
| 规范引用 | _metadata.md 术语 root_meta_info.json（注入草稿清理时须同步移除条目）；SYSTEM_MANUAL §6.4/§19.2 |
| 业务规则 | 1. 删除必须执行两步语义：删除草稿文件夹 + 从 root_meta_info.json 的 all_draft_store 中移除对应条目，二者缺一不可。2. root_meta 同步以 draft_id/路径为匹配键，条目不存在时视为已同步（幂等），不得报错。3. folder 参数必须防路径穿越（仅允许 DRAFT_ROOT 下的合法草稿文件夹名）。4. 正在被渲染任务使用（S06 rendering 态对应的注入草稿）时删除须拒绝或提示风险。 |
| 使用范围 | R01 视频创作者（经 S07-006 面板二次确认）；R03 脚本/程序调用方；R04 运维（清理异常残留 rd*） |
| 先决条件 | folder 存在于 DRAFT_ROOT；文件未被剪映锁定 |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 3 个基本功能点：|
| | 1. DELETE /api/drafts/<folder> 删除草稿文件夹 |
| | 2. 同步移除 root_meta_info.json 的 all_draft_store 条目 |
| | 3. 幂等语义（重复删除/条目已不存在不报错） |
| 辅助功能 | 与 S06-010 渲染清理共用同一同步规则（rd* 注入草稿清理同样要求） |
| 提示信息 | 删除成功/失败（文件占用原因透传） |

**处理逻辑：**
1. 校验 folder 合法性；
2. 删除 DRAFT_ROOT/<folder> 文件夹；
3. 读 root_meta_info.json → all_draft_store 移除匹配条目 → 写回；
4. 返回删除结果。

**约束条件：**
1. 剪映正在运行且锁定草稿文件时删除可能失败，须返回可读错误。
2. 删除不可恢复，调用方（面板）负责二次确认。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | folder（路径参数） |
| 输出信息 | 删除结果（成功/失败+原因）；BO-07-010 root_meta 索引条目 sync_action=remove |

**业务表单：** 无（REST API；前端确认框见 S07-006）

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 删除草稿（API 层） | R01 视频创作者、R03 脚本/程序调用方、R04 系统运维/开发者 |

##### 五、接口依赖

| 方向 | 对接系统 | 接口说明 | 数据格式 |
|------|---------|---------|---------|
| 输出 | S07-006 草稿管理面板 | GET /api/drafts、GET /api/cover?folder=、DELETE /api/drafts/<folder> | HTTP JSON / 图片 |
| 输出 | R03 脚本/程序调用方 | 同上三个端点（自动化草稿清理） | HTTP JSON |
| 输入 | EXT-01 剪映专业版 | DRAFT_ROOT 草稿文件夹、draft_meta_info.json、root_meta_info.json（数据源与同步对象） | 文件系统 JSON |
| 输出 | S06 无人值守渲染 | 与 S06-010 草稿清理共享 root_meta 同步规则（一致性约束） | 文件系统 JSON |

##### 六、需求追溯

| 来源 | 引用 |
|------|------|
| 技术响应表 | 数据源为系统功能手册，对应 _metadata.md S07 功能点索引 S07-008 |
| 技术方案 | SYSTEM_MANUAL.md §16.3 草稿管理（/api/drafts、/api/cover、DELETE /api/drafts/<folder> + root_meta 同步）、§6.4 渲染流程清理步、§19.2"草稿丢失" |
| 优先级 | P1 |
| ▲标注 | 否 |

---

#### S07-009 静态托管与 SPA 路由（Vue catch-all + CORS）

##### 一、功能综述

render_server 以 Flask 直接托管前端构建产物（frontend 经 `npm run build` 输出到 `static/`），使用户访问 http://localhost:9002 即得完整 Web GUI，无需另起前端服务器。核心机制有三：其一，SPA catch-all 路由——Vue 是单页应用，任意非 API 路径的 GET 请求（前端路由路径）统一回落到 index.html，由 Vue Router 接管，刷新深链不 404；其二，CORS 全开（`Access-Control-Allow-Origin: *`）——开发模式下 Vite dev server 跑在 5173 端口、API 在 9002，跨源调用由该响应头放行；其三，未构建兜底——`static/` 无构建产物时 `/` 返回明确提示"Frontend not built"并给出构建命令（`cd frontend && npm install && npm run build`），而不是 404/500（§19.6）。

该功能决定了前端两种运行形态：生产形态（9002 直连 Flask 托管 SPA）与开发形态（5173 Vite dev server，代理 /api 等请求到 9002，热更新），二者共用同一后端。

##### 二、业务对象

| 编码 | 业务对象 | 数据项 | 数据类型 | 长度 | 必填 | 编码引用说明 | 备注 |
|------|---------|--------|---------|------|------|------------|------|
| BO-07-008 | SPA 资源请求 | request_path | VARCHAR | 255 | Y | URL 路径 | 非 API 的 GET 路径走 catch-all |
| BO-07-008 | SPA 资源请求 | method | VARCHAR | 10 | Y | GET 为主 | API 路径不受 catch-all 影响 |
| BO-07-008 | SPA 资源请求 | cors_header | VARCHAR | 255 | Y | Access-Control-Allow-Origin: * | dev 5173 跨源放行 |
| BO-07-008 | SPA 资源请求 | response_type | VARCHAR | 20 | Y | static_file / index.html / 提示文本 | 三类响应形态 |
| BO-07-008 | SPA 资源请求 | built | TINYINT | 1 | Y | static/ 是否有构建产物 | 0 时 / 返回 Frontend not built |

##### 三、业务活动

1. **静态托管**：命中 static/ 真实文件（js/css/图片）直接回传（Flask 静态文件语义）。
2. **SPA 回落**：非 API、非静态的 GET 路径统一返回 index.html（catch-all），前端路由接管。
3. **未构建兜底**：static/ 为空/缺失时返回"Frontend not built"提示文本与构建指引。
4. **CORS 注入**：响应统一带 Access-Control-Allow-Origin: *，支持 5173 dev 跨源。
5. **构建部署**：开发者执行 `cd frontend && npm install && npm run build` 产出 static/，服务无需重启即托管新版本（文件级替换）。
6. **开发模式**：`npm run dev` 起 Vite 5173，代理 API 请求至 9002，热更新开发。

##### 四、用例描述

##### 用例 U-07-009-01

| 项目 | 内容 |
|------|------|
| 用例编号 | U-07-009-01 |
| 用例名称 | SPA 托管与 catch-all 路由回落 |
| 业务说明 | 用户访问 http://localhost:9002（或任意前端深链路径），获得 Vue SPA；刷新非根路径不 404 |
| 规范引用 | _metadata.md 合规要求-运行环境（端口 9002/5173） |
| 业务规则 | 1. 静态资源请求命中 static/ 真实文件时必须直接返回文件，不得回落。2. 非 API 的 GET 路径未命中静态文件时必须统一回落 index.html（SPA catch-all），保证前端路由深链可刷新。3. catch-all 不得吞没 REST API 路径：/api/*、/render*、/perceive/*、/health 等端点优先匹配。4. static/ 无构建产物时 `/` 必须返回明确的"Frontend not built"提示（含构建命令指引），不得返回裸 404/500。 |
| 使用范围 | R01 视频创作者（生产访问）；R04 系统运维/开发者（构建部署） |
| 先决条件 | 生产形态：static/ 已构建；开发形态：5173 dev server 运行 |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 4 个基本功能点：|
| | 1. static/ 静态文件托管（Flask） |
| | 2. SPA catch-all：任意非 API GET 回落 index.html |
| | 3. API 路由优先匹配（catch-all 兜底不抢占） |
| | 4. 未构建提示（Frontend not built + 构建指引） |
| 辅助功能 | 构建产物文件级替换即可更新前端（无需重启服务） |
| 提示信息 | "Frontend not built" → cd frontend && npm install && npm run build |

**处理逻辑：**
1. 收到 GET 请求；
2. 命中 API 端点 → 走各业务端点；
3. 命中 static/ 文件 → 返回文件；
4. static/ 未构建 → 返回 Frontend not built 提示；
5. 其余 → 返回 index.html（SPA 回落）。

**约束条件：**
1. 托管仅限本机/局域网使用，无鉴权要求（本地单机工具定位）。
2. index.html 回落必须携带 CORS 头（与静态响应一致）。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | HTTP GET 请求路径 |
| 输出信息 | BO-07-008 SPA 资源请求（response_type=static_file/index.html/提示文本） |

**业务表单：** 无（服务端机制）

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 访问 SPA | R01 视频创作者 |
| 构建部署（npm run build → static/） | R04 系统运维/开发者 |

##### 用例 U-07-009-02

| 项目 | 内容 |
|------|------|
| 用例编号 | U-07-009-02 |
| 用例名称 | 开发模式 5173 代理与 CORS 放行 |
| 业务说明 | 开发者在 frontend/ 下 `npm run dev` 起 Vite（5173），API 请求代理到 9002；跨源由 Access-Control-Allow-Origin: * 放行 |
| 规范引用 | _metadata.md 运行环境约定（5173 前端 dev） |
| 业务规则 | 1. dev 模式下 Vite dev server（5173）须将 API 请求代理到 http://localhost:9002，前端代码使用相对路径即可同构生产。2. render_server 必须对所有响应附加 Access-Control-Allow-Origin: *，使 5173 → 9002 跨源调用免预检失败。3. dev 与生产共用同一后端与同一 api/index.js 封装，切换零代码改动。4. SSE 流式响应（/api/chat）在代理模式下须透传（不缓冲），保证对话面板流式可用。 |
| 使用范围 | R04 系统运维/开发者（前端开发） |
| 先决条件 | frontend/ 依赖已 npm install；9002 服务运行 |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 3 个基本功能点：|
| | 1. Vite 5173 dev server 与 9002 代理配置 |
| | 2. 后端 CORS 全开（Access-Control-Allow-Origin: *） |
| | 3. dev/生产同构（相对路径 API 封装） |
| 辅助功能 | 热更新开发；SSE 代理透传 |
| 提示信息 | 无 |

**处理逻辑：**
1. `cd frontend && npm run dev`；
2. 浏览器访问 http://localhost:5173；
3. API 请求经 Vite 代理转发 9002，响应带 CORS 头；
4. 改码热更新，联调完成后 `npm run build` 切生产。

**约束条件：**
1. CORS * 仅适配本地开发便利，系统为本地单机部署，不构成公网暴露面。
2. dev 模式不是生产形态，最终交付以 static/ 构建产物为准。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | 5173 代理转发的 API 请求 |
| 输出信息 | 带 CORS 头的 API 响应 / 静态资源 |

**业务表单：** 无

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 前端开发/代理联调 | R04 系统运维/开发者 |

##### 五、接口依赖

| 方向 | 对接系统 | 接口说明 | 数据格式 |
|------|---------|---------|---------|
| 输出 | S07-003～007 前端面板 | GET / 及 SPA catch-all 托管（static/ 产物） | HTML/JS/CSS 静态资源 |
| 输出 | R04 运维（Vite dev server） | CORS * 放行 5173 跨源调用 | HTTP 响应头 |
| 输入 | Node.js/npm 构建链 | frontend 源码 → static/ 构建产物 | 文件系统 |

##### 六、需求追溯

| 来源 | 引用 |
|------|------|
| 技术响应表 | 数据源为系统功能手册，对应 _metadata.md S07 功能点索引 S07-009 |
| 技术方案 | SYSTEM_MANUAL.md §15.3 构建与部署、§4.4 前端开发模式、§16.10 系统端点（/ Vue SPA catch-all）、§19.6 前端故障排查 |
| 优先级 | P2 |
| ▲标注 | 否 |

---

#### S07-010 健康检查（/health）

##### 一、功能综述

/health 是统一服务的最小可用性探针：GET /health 返回 `{ok, service, videos_dir}`——ok 标志服务存活，service 标识服务名（render_server），videos_dir 指示渲染输出目录（C:\Users\Administrator\Videos\，S06 完成检测的监视目录）。它是 S07-002 启停管理的配套确认手段（serve.bat 后验证就绪、stop.bat 后验证已停）、R03 脚本编排流程的前置探测（调用 /render 前先探活）、以及降级模式（S07-001）下确认"服务活着但编辑能力收缩"的依据。健康检查不依赖任何外部系统（VLM/ASR/剪映），仅表征本进程存活与关键路径配置。

##### 二、业务对象

| 编码 | 业务对象 | 数据项 | 数据类型 | 长度 | 必填 | 编码引用说明 | 备注 |
|------|---------|--------|---------|------|------|------------|------|
| BO-07-009 | 健康状态 | ok | 布尔 | 1 | Y | true = 服务存活 | 探活标志 |
| BO-07-009 | 健康状态 | service | VARCHAR | 50 | Y | render_server | 服务标识 |
| BO-07-009 | 健康状态 | videos_dir | VARCHAR | 260 | Y | C:\Users\Administrator\Videos\ | 渲染输出目录（§18.1） |

##### 三、业务活动

1. **探活**：GET /health → 200 + {ok:true,...} 判定存活；超时/拒绝判定未运行。
2. **启动确认**：serve.bat 启动后轮询 /health 确认就绪再访问 SPA。
3. **停止确认**：stop.bat 后 /health 不可达确认已停（无残留旧进程，§19.1 排查）。
4. **脚本前置检查**：R03 自动化流程调用业务端点前先探活，避免对已停服务排队请求。

##### 四、用例描述

##### 用例 U-07-010-01

| 项目 | 内容 |
|------|------|
| 用例编号 | U-07-010-01 |
| 用例名称 | 服务健康检查 |
| 业务说明 | 运维/脚本调用 GET /health，确认服务存活与渲染输出目录配置 |
| 规范引用 | 无 |
| 业务规则 | 1. /health 必须无认证、无参数、轻量响应，适合高频轮询。2. 响应固定为 {ok, service, videos_dir} 三字段结构。3. 健康检查不触发外部依赖调用（VLM/ASR/剪映/Frida），仅表征本进程与关键路径配置。4. 服务在运行（含 S07-001 降级模式）即返回 ok=true；融合状态不在本端点判定范围。 |
| 使用范围 | R04 系统运维/开发者（启停确认/排障）；R03 脚本/程序调用方（前置探活） |
| 先决条件 | 服务进程存活于 9002 |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 2 个基本功能点：|
| | 1. GET /health 返回 {ok, service, videos_dir} JSON |
| | 2. 进程存活即可响应（降级模式同样 ok） |
| 辅助功能 | 供 R03 脚本流程 D 前置探活 |
| 提示信息 | 无（探活语义由调用方判定） |

**处理逻辑：**
1. 收到 GET /health；
2. 组装 {ok:true, service:"render_server", videos_dir:<渲染输出目录>}；
3. 200 返回。

**约束条件：**
1. 响应必须为纯内存构造，不得有磁盘/网络副作用。
2. 不承载业务鉴权或详细诊断（详细状态看启动日志与 S07-002 调试模式）。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | GET /health 请求 |
| 输出信息 | BO-07-009 健康状态（ok/service/videos_dir） |

**业务表单：** 无

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 健康检查 | R04 系统运维/开发者、R03 脚本/程序调用方 |

##### 五、接口依赖

| 方向 | 对接系统 | 接口说明 | 数据格式 |
|------|---------|---------|---------|
| 输出 | S07-002 服务启停管理 | 启动后就绪确认、停止后不可达确认 | HTTP JSON |
| 输出 | R03 脚本/程序调用方 | 自动化流程前置探活 | HTTP JSON |
| 输出 | S08 MCP 工具服务 | （可选）MCP 客户端侧排障经 9002 直连 /health | HTTP JSON |

##### 六、需求追溯

| 来源 | 引用 |
|------|------|
| 技术响应表 | 数据源为系统功能手册，对应 _metadata.md S07 功能点索引 S07-010 |
| 技术方案 | SYSTEM_MANUAL.md §16.10 系统端点（/health → {ok, service, videos_dir}）、§5.4 端点分组 |
| 优先级 | P2 |
| ▲标注 | 否 |

---

## 三、业务对象汇总

| 编码 | 业务对象 | 字段数 | 所属功能点 | 说明 |
|------|---------|--------|-----------|------|
| BO-07-001 | 融合状态 | 6 | S07-001 | FUSED_VC 标志/挂载路径/缺失依赖/降级原因/启动日志 |
| BO-07-002 | 服务进程 | 7 | S07-002 | pid/serve.pid/端口 9002/占用检测/启动模式/状态/日志 |
| BO-07-003 | 素材条目 | 7 | S07-003 | 文件名/类型/路径/大小/时长/md5 键/分析缓存标志 |
| BO-07-004 | 接收状态 | 8 | S07-004 | running/设备名/53317 端口/活跃会话/待收/已收/本机 IP/错误码 |
| BO-07-005 | 对话消息 | 8 | S07-005 | 消息 ID/角色/markdown 内容/draft_id/工具名/参数/结果/时间戳 |
| BO-07-006 | 草稿条目 | 8 | S07-006、S07-008 | folder/draft_id/draft_name/duration/cover_url/创建/修改时间/路径 |
| BO-07-007 | 渲染任务视图 | 7 | S07-007 | task_id/状态/草稿名/产物名/时长/下载地址/错误信息 |
| BO-07-008 | SPA 资源请求 | 5 | S07-009 | 请求路径/方法/CORS 头/响应形态/构建标志 |
| BO-07-009 | 健康状态 | 3 | S07-010 | ok/service/videos_dir |
| BO-07-010 | root_meta 索引条目 | 5 | S07-008 | draft_id/draft_name/draft_fold_path/tm_draft_modified/sync_action |
| **合计** | **10 个业务对象** | **64 个数据字段** | | |

> 数据类型均遵循 `_metadata.md` 数据类型约定（无关系型数据库；状态为内存对象，配置/索引为 JSON 文件，路径受 MAX_PATH 260 约束，tm_* 为 Unix 毫秒时间戳）。

## 四、接口汇总

| 序号 | 接口 | 方法 | 提供方 | 消费方 | 数据格式 |
|------|------|------|--------|--------|---------|
| 1 | /health | GET | S07-010 | R03/R04、S07-002 | JSON {ok, service, videos_dir} |
| 2 | / （SPA catch-all） | GET | S07-009 | R01、R04 | HTML/静态资源（含 CORS *） |
| 3 | capcut_server.app 融合挂载（28 编辑路由） | 进程内导入 | S07-001 | S04/S08/S05 端点、前端 | Python 对象（sys.path 挂载） |
| 4 | /api/drafts | GET | S07-008 | S07-006、R03 | JSON 草稿列表 |
| 5 | /api/cover?folder= | GET | S07-008 | S07-006 | 图片字节 |
| 6 | /api/drafts/<folder> | DELETE | S07-008 | S07-006、R03/R04 | JSON（含 root_meta 同步语义） |
| 7 | /api/upload | POST | S01（S07-003 调用） | S07-003 素材面板、R03 | multipart |
| 8 | /api/assets | GET | S01 | S07-003、S07-004 联动 | JSON |
| 9 | /api/video/serve?path= | GET | S01 | S07-003 预览 | 视频流（X-From-RAM） |
| 10 | /api/perceive | POST | S02 | S07-003 | JSON {path, force?, do_asr?, frames?} |
| 11 | /api/perceive/cached | GET | S02 | S07-003 | JSON |
| 12 | /api/localsend/status | GET | S01 | S07-004 | JSON |
| 13 | /api/localsend/start | POST | S01 | S07-004 | JSON（409 端口冲突） |
| 14 | /api/localsend/stop | POST | S01 | S07-004 | JSON（接收文件列表） |
| 15 | /api/chat | POST | S04 | S07-005 对话面板 | JSON 请求 / SSE 流式响应 |
| 16 | /render | POST | S06 | S07-007、R03 | multipart zip（含 draft_content.json） |
| 17 | /render/draft/<draft_id> | POST | S06 | S07-007、S07-006 | JSON |
| 18 | /render/status/<task_id> | GET | S06 | S07-007、R03 | JSON 状态 |
| 19 | /render/download/<task_id> | GET | S06 | S07-007、R03 | mp4 流 |
| 20 | /render/list | GET | S06 | S07-007 | JSON 任务列表 |

> 接口 7-20 为 S07 面板层对 S01/S02/S04/S06 子系统端点的消费调用（对接方向为 S07 → 子系统），基址统一 http://localhost:9002（S07-001 融合 + S07-002 启停保障）。外部系统引用：EXT-01 剪映（草稿目录/root_meta，经 S07-008 文件系统交互）、EXT-02 通义千问（经 S04）、EXT-04 LocalSend 客户端（经 S01）、EXT-05 FFprobe（经 S01/S02）。

## 五、需求追溯矩阵

| 功能编号 | 功能名称 | 数据源（_metadata.md S07 索引） | 技术方案引用（SYSTEM_MANUAL.md） | 优先级 | ▲ | 用例 |
|---------|---------|-------------------------------|----------------------------------|-------|---|------|
| S07-001 | VectCutAPI 融合机制（sys.path 挂载 + 降级模式） | S07-001（原型 G/F，P1） | §5.1、§13、§13.3、§19.1 | P1 | 否 | U-07-001-01～02 |
| S07-002 | 服务启停管理（serve.bat/stop.bat/端口检测/PID） | S07-002（原型 F，P1） | §4.1-4.3、§18.2、§19.1 | P1 | 否 | U-07-002-01～03 |
| S07-003 | 素材面板（上传/LocalSend/分析/预览） | S07-003（原型 H，P1） | §15.2（AssetPanel.vue/stores/asset.js）、§16.2、§16.4、§10.2、§17 流程 A | P1 | 否 | U-07-003-01～03 |
| S07-004 | LocalSend 接收对话框（启停/状态/已收列表） | S07-004（原型 H，P1） | §15.2（ReceiveDialog.vue）、§11.4、§16.8、§19.3、§17 流程 A | P1 | 否 | U-07-004-01～02 |
| S07-005 | 对话面板（SSE 流式渲染） | S07-005（原型 H，P1） | §15.2（ChatPanel.vue/stores/chat.js）、§16.6、§17 流程 A | P1 | 否 | U-07-005-01～02 |
| S07-006 | 草稿管理面板（列表/封面/删除） | S07-006（原型 H，P1） | §15.2（DraftPanel.vue/stores/project.js）、§16.3、§19.2 | P1 | 否 | U-07-006-01～02 |
| S07-007 | 渲染任务面板（提交/状态/下载） | S07-007（原型 H，P1） | §15.2（RenderPanel.vue/stores/render.js）、§5.3、§16.1、§17 流程 A/D、§19.2 | P1 | 否 | U-07-007-01～02 |
| S07-008 | 草稿管理 API（/api/drafts 列表/封面/删除 + root_meta 同步） | S07-008（原型 A，P1） | §16.3、§6.4、§19.2 | P1 | 否 | U-07-008-01～02 |
| S07-009 | 静态托管与 SPA 路由（Vue catch-all + CORS） | S07-009（原型 F，P2） | §15.3、§4.4、§16.10、§19.6 | P2 | 否 | U-07-009-01～02 |
| S07-010 | 健康检查（/health） | S07-010（原型 F，P2） | §16.10、§5.4 | P2 | 否 | U-07-010-01 |

**合计**：功能点 10（S07-001～010 连续无缺失）｜▲标注 0｜业务对象 10（64 字段）｜用例 21｜接口 20。

---

*编制说明：本文档基于 `_metadata.md`（2026-08-14）与 SYSTEM_MANUAL.md 编写；数据源为系统功能手册（无标书响应表），故全部功能点无 ▲ 重点响应标注，优先级按 P1（核心）/P2（支撑）划分。*
