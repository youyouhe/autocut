# S08 MCP 工具服务 — 需求规格说明书

## 文档信息

| 项目 | 内容 |
|------|------|
| 项目名称 | AI 视频工作台 |
| 所属分册 | 第四册 服务集成与交互 |
| 系统编号 | S08 |
| 系统名称 | MCP 工具服务 |
| 系统分级 | 小型（功能点 ≤ 6，单文件一次性完成） |
| 功能点总数 | 4 |
| ▲标注数 | 0 |
| 编制日期 | 2026-08-14 |
| 文档版本 | V1.0 |

## 一、系统概述

### 1.1 系统定位

MCP 工具服务（`mcp_video_server.py`）是 AI 视频工作台面向外部 AI Agent 的统一工具网关。它将核心服务 render_server（`http://localhost:9002`）的视频感知、草稿编辑、真后台渲染、资源字典等能力封装为 MCP（Model Context Protocol）工具，供 Claude Code、Cursor 等 MCP 客户端（外部系统 EXT-06）通过 **stdio 传输**直接调用，使外部智能体在对话中即可完成"看懂视频 → 建草稿 → 编辑 → 渲染 → 查状态 → 质检"的全流程自动化生产（手册 §17 流程 C）。

服务采用"stdio 进程 + REST 透传"双层架构：MCP Server 进程由客户端按 `.mcp.json` 配置以子进程方式拉起；进程内部通过 `requests` 库调用 `http://localhost:9002` 的 REST API，自身不直接操作剪映、Frida、多桌面或草稿文件系统，全部业务语义与状态由 S01~S07 各系统承担。S08 仅做协议转换（MCP 工具调用 ↔ REST HTTP）与参数/结果的序列化封装。

### 1.2 业务范围

| 功能编号 | 功能名称 | 功能原型 | 优先级 |
|---------|---------|---------|--------|
| S08-001 | MCP Server 配置与 stdio 传输（.mcp.json） | G 集成/同步/对接 | P1 |
| S08-002 | 感知工具（perceive_video / perceive_result） | G 集成/同步/对接 | P1 |
| S08-003 | 编辑与渲染工具（create_draft / add_video / add_text / add_audio / save_draft / render / render_status） | G 集成/同步/对接 | P1 |
| S08-004 | 资源列表工具（get_animations：intro/outro/combo/transition/mask/font/effect） | G 集成/同步/对接 | P2 |

**数据源勘误说明**：手册 §14.1 标题计"暴露的 MCP 工具 11 个"，但其工具表仅具名列出 10 个（perceive_video、create_draft、add_video、add_text、add_audio、save_draft、render、render_status、perceive_result、get_animations）。本规格以具名的 10 个工具为准逐项展开数据字典；若后续版本确认第 11 个工具，按本模板增补其 BO 与调用链映射即可（编号顺延，不影响既有条目）。

### 1.3 用户角色（引用 _metadata.md 角色定义）

| 角色编码 | 角色名称 | 在本系统中的职责 |
|---------|---------|----------------|
| R02 | 外部 AI Agent | 主要使用者。经 MCP stdio 接入的智能体客户端（Claude Code、Cursor 等），通过工具发现获得工具清单，由模型决策调用感知/编辑/渲染/资源工具完成自动化视频生产 |
| R04 | 系统运维/开发者 | 辅助使用者。维护 `.mcp.json` 配置（command/args/cwd）、确认 render_server（9002）已启动、排查工具调用链路故障 |

### 1.4 系统边界

**系统内（S08 职责）：**
- `.mcp.json` 配置的约定结构与加载；
- MCP stdio 进程生命周期（拉起、握手、工具注册、调用分发、连接退出）；
- MCP 工具 ↔ localhost:9002 REST 端点的调用链映射、参数透传与结果回传。

**系统外（不在 S08 职责内）：**
- 感知计算本体（VLM/ASR/场景检测，S02）；
- 草稿文件生成与编辑语义（VectCutAPI/pyJianYingDraft，S05）；
- 渲染调度、多桌面、Frida 注入（S06）；
- REST 服务本体与端口 9002 的启停（S07）；
- MCP 客户端侧的模型决策与提示词（EXT-06 自身）。

**边界依赖**：S08 的所有工具调用以 render_server 已在 9002 端口正常服务为先决条件；render_server 未启动或 VectCutAPI 融合降级（缺 oss2/json5，编辑端点不可用）时，相应工具调用返回错误结果。

## 二、功能需求

### 2.1 接入与传输配置

#### S08-001 MCP Server 配置与 stdio 传输（.mcp.json）

##### 一、功能综述

外部 AI Agent（R02）接入本系统的第一步是服务发现与连接建立。Claude Code 等 MCP 客户端启动时读取项目根目录的 `.mcp.json`，按其中的 `mcpServers."video-tools"` 配置以子进程方式拉起 MCP Server：`command` 为 `python`，`args` 为 `["mcp_video_server.py"]`，`cwd` 指定项目根（`.../ym`）目录。`cwd` 约定保证脚本以项目根为工作目录启动，相对路径（脚本自身、VectCutAPI 挂载路径等）解析一致。

进程拉起后，客户端经 stdin/stdout 双向管道与 Server 进行 JSON-RPC 2.0 消息交互（stdio 传输）：先完成协议握手（initialize），随后客户端通过工具发现（tools/list）取得工具注册表——每个工具含名称、说明与入参 Schema；Agent 的 LLM 在对话中决策调用某工具时，客户端发送工具调用请求（tools/call），Server 将入参转换为 HTTP 请求转发至 `http://localhost:9002` 对应 REST 端点，并将 REST 响应封装为 MCP 工具结果经 stdout 回传。本功能是 S08-002/003/004 三个工具功能点的公共传输底座。

##### 二、业务对象

| 编码 | 业务对象 | 数据项 | 数据类型 | 长度 | 必填 | 编码引用说明 | 备注 |
|------|---------|--------|---------|------|------|------------|------|
| BO-08-001 | MCP Server 配置（.mcp.json） | mcpServers | JSON Object | - | Y | 本系统配置文件（项目根 `.mcp.json`） | 顶层键，值为服务器映射表 |
| | | mcpServers."video-tools" | VARCHAR | 32 | Y | 服务器注册名，固定 `video-tools` | 客户端内工具集命名空间 |
| | | command | VARCHAR | 50 | Y | 解释器命令 | 固定 `python` |
| | | args | JSON Array | - | Y | 命令行参数 | `["mcp_video_server.py"]` |
| | | cwd | VARCHAR | 260 | Y | 项目根（`.../ym`）绝对路径 | MAX_PATH 约定；保证相对路径解析 |
| BO-08-002 | stdio 会话消息（JSON-RPC 2.0） | jsonrpc | VARCHAR | 3 | Y | MCP/JSON-RPC 协议版本 | 固定 `2.0` |
| | | method | VARCHAR | 32 | Y | `initialize` / `tools/list` / `tools/call` | 请求方法名 |
| | | params | TEXT(JSON) | - | Y | 工具名 `name`、入参对象 `arguments` | tools/call 时携带 |
| | | id | VARCHAR | 32 | N | 请求标识 | 响应按 id 关联 |
| | | result | TEXT(JSON) | - | N | 工具结果 content 数组 | 成功响应 |
| | | isError | JSON bool | 1 | N | false/true | true 时 content 携带错误描述 |
| | | content | TEXT(JSON) | - | Y | MCP 内容块（text 等） | 回传给 Agent 模型 |
| BO-08-003 | 工具描述符（tools/list 条目） | name | VARCHAR | 50 | Y | 10 个工具名之一 | 见 S08-002~004 数据字典 |
| | | description | VARCHAR | 200 | Y | 工具用途中文说明 | 供 LLM 理解选择 |
| | | inputSchema | TEXT(JSON) | - | Y | JSON Schema（类型/必填/枚举） | 如 category 的 7 值枚举 |

> 数据类型遵循 _metadata.md 数据类型约定（无关系型数据库；配置为 JSON 文件、路径 VARCHAR(260)、布尔 JSON bool、长文本 TEXT）。

##### 三、业务活动

1. **配置加载**：MCP 客户端启动时读取项目根 `.mcp.json`，解析 `video-tools` 服务器条目（R04 负责维护该文件）。
2. **进程拉起**：客户端以 `command + args` 在 `cwd` 目录下启动 `mcp_video_server.py` 子进程，建立 stdin/stdout 管道。
3. **协议握手**：完成 initialize 握手，交换协议版本与能力。
4. **工具注册与发现**：Server 返回 10 个工具的名称/说明/入参 Schema（tools/list）。
5. **调用分发**：客户端发起 tools/call，Server 校验工具名、组装 HTTP 请求转发 9002。
6. **结果回传**：REST 响应序列化为 MCP content 块回传；异常封装为 isError 结果。
7. **连接生命周期管理**：客户端会话结束时终止子进程；进程崩溃由客户端按其重连策略处理。

##### 四、用例描述

##### 用例 U-08-001-01 MCP Server 加载与工具发现

| 项目 | 内容 |
|------|------|
| 用例编号 | U-08-001-01 |
| 用例名称 | MCP Server 加载与工具发现 |
| 业务说明 | 外部 Agent 客户端启动时自动加载 `.mcp.json`，拉起 video-tools Server 并取得全部工具清单，使模型具备调用系统能力的前提（手册 §4.5、§17 流程 C 步骤 1） |
| 规范引用 | 无（本项目无行业强制合规；密钥安全要求见 _metadata.md，不影响本功能） |
| 业务规则 | 1. `.mcp.json` 须位于项目根目录，且含 `mcpServers."video-tools"` 条目，`command`/`args`/`cwd` 三项齐全方可拉起进程。2. 子进程必须以 `cwd`（项目根 `.../ym`）为工作目录启动，保证 `mcp_video_server.py` 相对路径解析正确。3. 工具发现返回的注册表须覆盖本系统全部 10 个工具，每个工具含 name/description/inputSchema 三要素。4. 工具 Schema 中的枚举取值（如 get_animations 的 category）须与 REST 端点接受的取值一致。 |
| 使用范围 | R02 外部 AI Agent（发起）；R04 系统运维/开发者（维护 .mcp.json） |
| 先决条件 | 项目根存在合法 `.mcp.json`；本机 python 可用；（仅工具调用时）9002 服务已启动 |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 5 个基本功能点：|
| | 1. 解析 `.mcp.json` 的 `video-tools` 配置（command/args/cwd） |
| | 2. 以 stdio 子进程方式启动 MCP Server |
| | 3. 完成 initialize 协议握手 |
| | 4. 响应 tools/list 返回 10 个工具描述符（含入参 JSON Schema） |
| | 5. 工具调用请求按注册表校验工具名后分发至对应 REST 透传处理 |
| 辅助功能 | Server 启动时对 `http://localhost:9002/health` 自检，服务不可达时在工具结果中给出可读错误提示 |
| 提示信息 | 配置缺失/进程拉起失败/9002 不可达时，向客户端返回明确错误描述 |

**处理逻辑：**
1. 客户端读取 `.mcp.json` → 校验 `video-tools` 条目完整性；
2. 在 `cwd` 目录执行 `python mcp_video_server.py`，建立 stdio 管道；
3. 完成 initialize 握手，客户端调用 tools/list；
4. Server 返回工具注册表（10 个工具描述符）；
5. Agent 对话中模型按 Schema 决策调用 → 进入 U-08-001-02。

**约束条件：**
1. 传输方式限定 stdio（stdin/stdout），不提供 SSE/WebSocket 等 MCP 传输；
2. `.mcp.json` 的 `cwd` 须为项目根绝对路径，禁止指向其他目录；
3. stdio 通道上除协议消息外不得混入日志等非协议输出（避免破坏 JSON-RPC 流解析）；
4. 本地单机部署，9002 为唯一后端服务地址（手册 §3.5 运行环境端口约定）。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | `.mcp.json`（JSON 文件）；客户端 initialize/tools/list 请求（JSON-RPC） |
| 输出信息 | BO-08-002 stdio 会话消息；BO-08-003 工具描述符列表 |

**业务表单：** 无（配置文件 `.mcp.json`，非界面表单）

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 工具发现、工具调用（U-08-001-01 全部基本功能 1~4） | R02 外部 AI Agent |
| .mcp.json 配置维护（基本功能 1 的配置侧） | R04 系统运维/开发者 |

##### 用例 U-08-001-02 工具调用 stdio→REST 透传

| 项目 | 内容 |
|------|------|
| 用例编号 | U-08-001-02 |
| 用例名称 | 工具调用 stdio→REST 透传 |
| 业务说明 | Agent 模型决策调用某工具后，客户端经 tools/call 发起调用，Server 将入参转为 HTTP 请求调用 localhost:9002 对应端点，并将结果回传模型，形成"模型 ↔ 工具 ↔ REST"闭环（手册 §14.2） |
| 规范引用 | 无 |
| 业务规则 | 1. 每个工具名必须映射且仅映射到一个确定的 REST 端点与方法（映射表见 S08-002/003/004）。2. 透传须按目标端点的内容类型组装请求：JSON 端点（如 /api/perceive、/create_draft）用 application/json，multipart 端点（/perceive/result）用文件表单。3. REST 返回非 2xx 或连接失败时，必须封装为 isError=true 的工具结果并附 HTTP 状态码/错误摘要，不得静默吞错。4. 工具结果统一以 MCP content 文本块（JSON 序列化）回传，供模型直接阅读。 |
| 使用范围 | R02 外部 AI Agent |
| 先决条件 | U-08-001-01 已完成（连接建立且工具已注册）；render_server 已在 9002 服务 |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 4 个基本功能点：|
| | 1. 校验工具名与入参（按 inputSchema） |
| | 2. 组装并发出 HTTP 请求（requests → localhost:9002） |
| | 3. REST 响应序列化为 MCP 工具结果回传 |
| | 4. 异常（超时/非 2xx/连接拒绝）封装为 isError 结果 |
| 辅助功能 | 无 |
| 提示信息 | 错误结果包含端点路径与失败原因，提示用户检查 9002 服务状态 |

**处理逻辑：**
1. 收到 tools/call（name + arguments）；
2. 查调用链映射表得 REST 端点与方法；
3. 按 BO 数据字典组装请求体/查询参数；
4. `requests` 发起 HTTP 调用 localhost:9002；
5. 成功：响应 JSON → content 回传；失败：isError=true + 错误摘要回传。

**约束条件：**
1. HTTP 调用须设置超时，防止 Agent 会话因后端长任务（渲染）无限阻塞；渲染类长任务采用"提交返回 task_id + 轮询"模式（见 S08-003）而非同步等待；
2. Server 不做业务规则改写，仅透传（校验语义由 S01~S07 端点承担）；
3. 单 Agent 会话内工具调用串行处理，结果按请求 id 关联返回。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | tools/call 请求（工具名 + 入参 JSON） |
| 输出信息 | MCP 工具结果（REST 响应 JSON 序列化 / 错误描述） |

**业务表单：** 无

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 工具调用透传（U-08-001-02 全部基本功能 1~4） | R02 外部 AI Agent |

##### 五、接口依赖

| 方向 | 对接系统 | 接口说明 | 数据格式 |
|------|---------|---------|---------|
| 输入 | EXT-06 MCP 客户端（Claude Code 等） | stdio 传输：initialize 握手、tools/list 工具发现、tools/call 工具调用 | JSON-RPC 2.0 over stdio |
| 输出 | localhost:9002 render_server（S07） | GET /health 启动自检；各业务端点透传见 S08-002/003/004 | HTTP + JSON |

##### 六、需求追溯

| 来源 | 引用 |
|------|------|
| 技术响应表 | 无标书响应表；数据源为系统功能手册 |
| 技术方案 | SYSTEM_MANUAL.md §14（MCP 工具服务）、§14.2（.mcp.json 配置与 stdio→requests→localhost:9002）、§4.5（MCP 接入）、§17 流程 C 步骤 1、§18.2（配置文件清单） |
| 优先级 | P1 |
| ▲标注 | 否 |

### 2.2 感知与质检工具

#### S08-002 感知工具（perceive_video / perceive_result）

##### 一、功能综述

感知工具让外部 Agent 具备"看懂视频"与"验收成片"两个能力入口。`perceive_video` 接收本地视频路径，透传至 S02 感知服务完成元数据提取、场景检测、抽帧 VLM 画面分析（内容/情绪/质量/亮点/帧内文字）与 ASR 词级转录，返回结构化分析结果，作为 Agent 后续编辑决策（选段、配标题、配字幕）的依据。`perceive_result` 接收渲染产出的 mp4 路径与可选期望描述，透传至质检端点由 VLM 抽帧打分，返回 quality_score/issues/suggestions，构成"渲染 → 质检"的闭环验收（手册 §17 流程 C 步骤 2、4）。

两个工具分别映射 S02 的 `/api/perceive`（路径分析，内存缓存优先）与 `/perceive/result`（渲染质检）。Agent 典型时序：先 perceive_video 理解素材 → 编辑渲染（S08-003）→ render_status 确认 done → perceive_result 质检出片。

##### 二、业务对象

| 编码 | 业务对象 | 数据项 | 数据类型 | 长度 | 必填 | 编码引用说明 | 备注 |
|------|---------|--------|---------|------|------|------------|------|
| BO-08-004 | perceive_video 工具调用 | video_path | VARCHAR | 260 | Y | 本地文件路径（render_uploads/ 等） | 透传为 REST `path` 字段 |
| | | do_asr | JSON bool | 1 | N | 默认 true；false 跳过转录 | 大视频降 token 手段 |
| | | frames | INT | 2 | N | 抽帧数，默认 5 | VLM 分析帧数 |
| | | force | JSON bool | 1 | N | 默认 false；true 强制重析 | 透传 /api/perceive `force` |
| | | analysis（返回） | TEXT(JSON) | - | Y | S02 分析结果聚合：元数据（duration DECIMAL(10,3)/width/height INT/fps DECIMAL(5,2)）、场景区间列表、VLM 画面分析、ASR full_text | 内存缓存命中时秒回（md5 键 O(1)） |
| BO-08-005 | perceive_result 工具调用 | video_path | VARCHAR | 260 | Y | 渲染产出 mp4 路径 | 透传为 multipart `video` |
| | | expectations | TEXT(JSON) | - | N | 期望描述（JSON），可选 | 透传 multipart `expectations` |
| | | quality_score（返回） | DECIMAL | (3,1) | Y | 0-10 VLM 质量分 | 引用 _metadata 数据约定 |
| | | issues（返回） | TEXT(JSON) | - | Y | 问题清单 | 供 Agent 决定是否重渲 |
| | | suggestions（返回） | TEXT(JSON) | - | Y | 改进建议 | 供 Agent 修订草稿 |

##### 三、业务活动

1. **视频感知查询**：Agent 以路径发起 perceive_video → 透传 POST /api/perceive → 缓存优先（md5 键）或执行分析 → 返回聚合结果。
2. **强制重析**：force=true 时绕过缓存重新分析。
3. **渲染质检**：渲染 done 后以 mp4 路径发起 perceive_result → 透传 POST /perceive/result → 返回打分/问题/建议。
4. **结果消费**：Agent 依据 analysis 做编辑决策、依据质检结果决定重渲或交付。

##### 四、用例描述

##### 用例 U-08-002-01 视频感知（perceive_video）

| 项目 | 内容 |
|------|------|
| 用例编号 | U-08-002-01 |
| 用例名称 | 视频感知（perceive_video） |
| 业务说明 | Agent 对话"看懂这个视频 C:/x.mp4"时调用本工具，获得视频的画面理解、场景结构与语音转录，作为后续编辑的事实依据（手册 §17 流程 C 步骤 2） |
| 规范引用 | _metadata.md 密钥安全要求（Qwen/ASR Key 环境变量化，影响 S02 后端，本工具透传层不涉密钥） |
| 业务规则 | 1. video_path 必填且为本机可访问路径（≤260 字符，正/反斜杠均接受），缺失或不可读时返回 isError。2. 同一素材重复分析命中内存缓存（md5 键）直接返回，不重复消耗 VLM/ASR 配额；force=true 时强制重析并更新缓存。3. do_asr 默认 true；大视频可传 do_asr=false 或调小 frames 控制分析时长与 token 消耗（手册 §19.4）。4. 返回的 analysis 须完整包含元数据、场景列表、VLM 分析与转录四类信息，不得截断丢弃（TEXT 长文本整体透传）。 |
| 使用范围 | R02 外部 AI Agent |
| 先决条件 | U-08-001-01 连接已建立；9002 服务已启动；视频文件存在于本机 |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 3 个基本功能点：|
| | 1. 接收 video_path/do_asr/frames/force 入参并组装 JSON 请求 |
| | 2. 透传 POST /api/perceive 并等待分析结果 |
| | 3. 将聚合分析结果序列化回传 Agent |
| 辅助功能 | 缓存命中提示（结果中标注来自缓存/新析，由 S02 端点语义决定） |
| 提示信息 | 路径不存在、VLM/ASR 超时错误透传为可读错误信息 |

**处理逻辑：**
1. 校验 video_path 非空；
2. 组装 `{path, do_asr, frames, force}` JSON 体；
3. requests POST http://localhost:9002/api/perceive；
4. 响应 analysis JSON 原样序列化为 content 回传。

**约束条件：**
1. 感知为同步调用，大视频分析耗时随帧数/时长增长，Agent 侧须容忍分钟级等待（或调小 frames/do_asr=false）；
2. 路径须为本机路径（本地单机部署约定），不支持 URL 输入。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | BO-08-004 入参（路径 + 分析开关） |
| 输出信息 | BO-08-004 analysis（S02 感知结果聚合，引用 S02 业务对象） |

**业务表单：** 无

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| perceive_video 调用（基本功能 1~3） | R02 外部 AI Agent |

##### 用例 U-08-002-02 渲染质检（perceive_result）

| 项目 | 内容 |
|------|------|
| 用例编号 | U-08-002-02 |
| 用例名称 | 渲染质检（perceive_result） |
| 业务说明 | 渲染任务 done 后，Agent 对产出 mp4 发起质检，获得质量分、问题与建议，决定交付或回改草稿重渲（手册 §17 流程 C 步骤 4） |
| 规范引用 | _metadata.md 密钥安全要求（同上，透传层不涉密钥） |
| 业务规则 | 1. 仅当渲染任务状态为 done 且 mp4_path 已产出（可由 render_status 确认）后调用方有意义；对非 done 产物路径调用由 S02 端点返回相应错误。2. expectations 为可选 JSON 期望描述，填写时质检按期望对照打分。3. 质检采用 multipart 文件上传方式（video 字段）透传 POST /perceive/result，与 perceive_video 的 JSON 方式区分。4. quality_score 为 0-10 一位小数；低于阈值（Agent 自定）时建议 Agent 修订草稿后重新走 S08-003 渲染链。 |
| 使用范围 | R02 外部 AI Agent |
| 先决条件 | 存在渲染产出 mp4 文件；9002 服务已启动 |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 3 个基本功能点：|
| | 1. 接收 video_path/expectations 并组装 multipart 请求 |
| | 2. 透传 POST /perceive/result 触发 VLM 抽帧打分 |
| | 3. 返回 quality_score/issues/suggestions 结构化结果 |
| 辅助功能 | 无 |
| 提示信息 | mp4 读取失败/VLM 超时错误透传为可读错误信息 |

**处理逻辑：**
1. 校验 video_path 非空且文件存在；
2. 组装 multipart（video=文件，expectations=可选 JSON 串）；
3. requests POST http://localhost:9002/perceive/result；
4. 打分结果序列化回传。

**约束条件：**
1. 质检为同步调用，依赖 VLM 云服务（EXT-02），网络异常时返回错误而非挂起；
2. 质检帧数由 S02 端点约定（渲染质检抽 8 帧），本工具不暴露帧数参数。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | BO-08-005 入参（mp4 路径 + 可选期望） |
| 输出信息 | BO-08-005 质检结果（quality_score/issues/suggestions） |

**业务表单：** 无

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| perceive_result 调用（基本功能 1~3） | R02 外部 AI Agent |

##### 五、接口依赖

| 方向 | 对接系统 | 接口说明 | 数据格式 |
|------|---------|---------|---------|
| 输入 | EXT-06 MCP 客户端 | tools/call：perceive_video、perceive_result 两个工具入口 | JSON-RPC over stdio |
| 输出 | localhost:9002 render_server（S02 感知端点） | POST /api/perceive（JSON：path/force/do_asr/frames）；POST /perceive/result（multipart：video/expectations） | HTTP JSON / multipart |

##### 六、需求追溯

| 来源 | 引用 |
|------|------|
| 技术响应表 | 无标书响应表；数据源为系统功能手册 |
| 技术方案 | SYSTEM_MANUAL.md §14.1（perceive_video/perceive_result 工具定义）、§9（感知能力）、§16.2（感知端点）、§17 流程 C 步骤 2/4 |
| 优先级 | P1 |
| ▲标注 | 否 |

### 2.3 编辑与渲染工具

#### S08-003 编辑与渲染工具（create_draft / add_video / add_text / add_audio / save_draft / render / render_status）

##### 一、功能综述

本功能点把草稿构建与出片两条链路完整暴露给外部 Agent。**草稿构建链**：create_draft 建草稿取得 draft_id → add_video/add_text/add_audio 向时间线追加视频轨道（含转场/蒙版/变速/音量）、文字（字体/颜色/阴影/背景/动画）与音频 → save_draft 落盘生成剪映草稿文件夹。**渲染出片链**：render 按 draft_id 提交真后台渲染取得 task_id → render_status 轮询任务状态直至 done（携带 mp4 产物路径）。七个工具依次映射 VectCutAPI 融合编辑端点（S05）与渲染端点（S06），Agent 据此可在无人工介入下完成"建片→剪辑→出片"（手册 §17 流程 C 步骤 3）。

渲染采用异步模式：render 立即返回 task_id 不阻塞会话，Agent 以 render_status 轮询（任务状态机 queued → rendering → done/error 由 S06 定义），避免 stdio 工具调用长时间挂起。

##### 二、业务对象

> 通用约定：draft_id 为 VARCHAR(UUID) 36；所有文件路径 VARCHAR 260；编辑工具返回统一为 `result`（TEXT(JSON)，含状态与消息）。

| 编码 | 业务对象 | 数据项 | 数据类型 | 长度 | 必填 | 编码引用说明 | 备注 |
|------|---------|--------|---------|------|------|------------|------|
| BO-08-006 | create_draft 工具调用 | width | INT | 4 | N | 画布宽，默认 1080 | S05 create_draft 入参 |
| | | height | INT | 4 | N | 画布高，默认 1920 | 竖版默认 1080×1920 |
| | | draft_name | VARCHAR | 32 | N | 草稿名 | 可选 |
| | | draft_id（返回） | VARCHAR(UUID) | 36 | Y | 草稿唯一标识 | 贯穿后续全部编辑/渲染工具 |
| BO-08-007 | add_video 工具调用 | draft_id | VARCHAR(UUID) | 36 | Y | 引用 BO-08-006 返回值 | 草稿上下文键 |
| | | video_path | VARCHAR | 260 | Y | 视频素材路径 | render_uploads/ 等 |
| | | transition | VARCHAR | 50 | N | 转场类型名 | 取值见 get_animations(transition) |
| | | mask | VARCHAR | 50 | N | 蒙版类型名 | 取值见 get_animations(mask) |
| | | speed | DECIMAL | (4,2) | N | 变速倍率 | S05 add_video 语义 |
| | | volume | INT | 3 | N | 音量 | 百分比语义由 S05 定义 |
| | | result（返回） | TEXT(JSON) | - | Y | 添加结果状态 | |
| BO-08-008 | add_text 工具调用 | draft_id | VARCHAR(UUID) | 36 | Y | 引用 BO-08-006 | |
| | | text | TEXT | - | Y | 文字内容 | |
| | | font | VARCHAR | 50 | N | 字体 | 取值见 get_animations(font) |
| | | color | VARCHAR | 16 | N | 颜色 | |
| | | shadow | JSON bool | 1 | N | 阴影开关 | |
| | | background | VARCHAR | 32 | N | 文字背景 | |
| | | animation | VARCHAR | 50 | N | 入/出场动画 | 取值见 get_animations(intro/outro) |
| | | result（返回） | TEXT(JSON) | - | Y | 添加结果状态 | |
| BO-08-009 | add_audio 工具调用 | draft_id | VARCHAR(UUID) | 36 | Y | 引用 BO-08-006 | |
| | | audio_path | VARCHAR | 260 | Y | 音频素材路径 | |
| | | result（返回） | TEXT(JSON) | - | Y | 添加结果状态 | |
| BO-08-010 | save_draft 工具调用 | draft_id | VARCHAR(UUID) | 36 | Y | 引用 BO-08-006 | |
| | | draft_folder（返回） | VARCHAR | 260 | Y | 落盘草稿文件夹路径 | 含 draft_content.json 等 |
| BO-08-011 | render 工具调用 | draft_id | VARCHAR(UUID) | 36 | Y | 已 save_draft 的草稿 | 透传路径参数 |
| | | task_id（返回） | VARCHAR(Hex) | 8 | Y | uuid hex 前 8 位 | 轮询键（引用 S06 任务对象） |
| BO-08-012 | render_status 工具调用 | task_id | VARCHAR(Hex) | 8 | Y | 引用 BO-08-011 返回值 | |
| | | status（返回） | VARCHAR | 10 | Y | queued/rendering/done/error | 引用 _metadata 任务状态枚举 |
| | | mp4_path（返回） | VARCHAR | 260 | N | done 后产物路径 | |
| | | mp4_name（返回） | VARCHAR | 100 | N | 产物文件名 | |
| | | duration（返回） | DECIMAL | (10,3) | N | 产物时长（秒） | |

**逐工具调用链映射（MCP 工具 → REST 端点）：**

| MCP 工具 | 透传 REST 端点（localhost:9002） | 方法 | 入参载体 | 主要返回 |
|---------|-------------------------------|------|---------|---------|
| create_draft | /create_draft | POST | JSON（width/height/draft_name） | draft_id |
| add_video | /add_video | POST | JSON（draft_id/video_path/transition/mask/speed/volume） | result |
| add_text | /add_text | POST | JSON（draft_id/text/font/color/shadow/background/animation） | result |
| add_audio | /add_audio | POST | JSON（draft_id/audio_path） | result |
| save_draft | /save_draft | POST | JSON（draft_id） | 草稿文件夹路径 |
| render | /render/draft/&lt;draft_id&gt; | POST | 路径参数（无请求体） | task_id |
| render_status | /render/status/&lt;task_id&gt; | GET | 路径参数 | status/mp4_path/mp4_name/duration |

##### 三、业务活动

1. **草稿创建**：create_draft 建立空白时间线，取得 draft_id 作为会话上下文。
2. **轨道追加**：add_video/add_text/add_audio 按序向草稿追加素材（参数可选增强：转场/蒙版/变速/字体/动画）。
3. **草稿落盘**：save_draft 生成剪映草稿文件夹（S05 语义）。
4. **渲染提交**：render 按 draft_id 经 /render/draft/&lt;id&gt; 入 S06 渲染队列（多桌面池）。
5. **状态轮询**：render_status 反复查询直至 done/error。
6. **产物衔接**：done 后以 mp4_path 衔接 S08-002 perceive_result 质检，或交 R01 下载。

##### 四、用例描述

##### 用例 U-08-003-01 草稿构建链（建草稿→加轨道→保存）

| 项目 | 内容 |
|------|------|
| 用例编号 | U-08-003-01 |
| 用例名称 | 草稿构建链（建草稿→加轨道→保存） |
| 业务说明 | Agent 对话"建草稿加上它配标题"时，依次调用 create_draft → add_video → add_text（→ add_audio）→ save_draft，产出可渲染的剪映草稿（手册 §17 流程 C 步骤 3） |
| 规范引用 | _metadata.md 版本锁定（VectCutAPI jianying_pro_10 格式与剪映 5.9.0 兼容性未充分验证，见手册 §13.2 注意事项） |
| 业务规则 | 1. 编辑链工具必须按序调用：add_video/add_text/add_audio/save_draft 的 draft_id 必须来自先前 create_draft 的返回值，非法/过期 draft_id 由 S05 端点报错并以 isError 回传。2. save_draft 之前必须完成全部轨道追加；save 之后再追加素材须遵循 S05 端点的状态语义（草稿已存在时的处理由 S05 定义）。3. 转场/蒙版/字体/动画等类型名参数的合法取值须来自 get_animations（S08-004）返回的列表，Agent 应先查列表再填值。4. 文件路径类参数（video_path/audio_path）须为本机存在的素材路径（≤260 字符）。 |
| 使用范围 | R02 外部 AI Agent |
| 先决条件 | 连接已建立；9002 服务已启动且 VectCutAPI 融合成功（未降级）；素材文件存在 |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 4 个基本功能点：|
| | 1. create_draft 透传 POST /create_draft，返回 draft_id |
| | 2. add_video/add_text/add_audio 分别透传对应 POST 端点（含可选增强参数） |
| | 3. save_draft 透传 POST /save_draft，返回草稿文件夹路径 |
| | 4. 各步结果（含 S05 校验错误）结构化回传 Agent |
| 辅助功能 | 工具说明（description）中提示推荐调用顺序，引导模型按链路调用 |
| 提示信息 | VectCutAPI 融合降级（编辑端点 404）时提示检查 oss2/json5 依赖与 9002 服务状态 |

**处理逻辑：**
1. Agent 调 create_draft → 得 draft_id；
2. 依次调 add_video（含 transition/mask/speed/volume 可选）、add_text（字体/样式/动画可选）、add_audio；
3. 调 save_draft 落盘；
4. 每步结果回传，失败即中断链路并向模型报错。

**约束条件：**
1. 编辑端点依赖 VectCutAPI 融合成功（sys.path 挂载 capcut_server），融合降级时编辑工具全部不可用（手册 §19.1）；
2. draft 格式受 VectCutAPI/config.json 的 draft_profile 约束（capcut_legacy/jianying_legacy/jianying_pro_10），与剪映 5.9.0 兼容性风险见规范引用；
3. 编辑工具为同步调用，须在 HTTP 超时内完成。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | BO-08-006~010 入参（草稿参数 + 素材路径 + 增强参数） |
| 输出信息 | BO-08-006~010 返回（draft_id/result/draft_folder），引用 S05 草稿对象 |

**业务表单：** 无

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| create_draft/add_video/add_text/add_audio/save_draft 调用（基本功能 1~4） | R02 外部 AI Agent |

##### 用例 U-08-003-02 渲染提交与状态轮询（render / render_status）

| 项目 | 内容 |
|------|------|
| 用例编号 | U-08-003-02 |
| 用例名称 | 渲染提交与状态轮询 |
| 业务说明 | 草稿保存后，Agent 调 render 提交真后台渲染取得 task_id，随后以 render_status 轮询直至 done，取得 mp4 产物路径（手册 §17 流程 C 步骤 3~4） |
| 规范引用 | _metadata.md 版本锁定（剪映 5.9.0.11632）与校准基准（1280×720，S06 侧约束） |
| 业务规则 | 1. render 的 draft_id 必须已经 save_draft 落盘；未保存草稿直接渲染由 S06 端点报错。2. render 为异步提交，立即返回 task_id（uuid hex 前 8 位），不等待渲染完成，工具调用不得阻塞至出片。3. render_status 返回状态机四态 queued/rendering/done/error；error 时结果须包含失败原因供 Agent 决策（改草稿重提或上报用户）。4. done 携带 mp4_path/mp4_name/duration 产物三元组；Agent 后续质检（U-08-002-02）应使用该 mp4_path。 |
| 使用范围 | R02 外部 AI Agent |
| 先决条件 | 草稿已保存；9002 服务已启动；S06 渲染环境（剪映 + Frida + 校准）就绪 |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 3 个基本功能点：|
| | 1. render 透传 POST /render/draft/&lt;draft_id&gt;，返回 task_id |
| | 2. render_status 透传 GET /render/status/&lt;task_id&gt;，返回四态与产物信息 |
| | 3. 未知 task_id / 服务错误以 isError 回传 |
| 辅助功能 | 工具说明中提示轮询节奏（建议间隔数秒，渲染受多桌面池 2 并行限制可能排队） |
| 提示信息 | queued 提示任务排队中；error 透传 S06 失败原因 |

**处理逻辑：**
1. Agent 调 render(draft_id) → POST /render/draft/&lt;draft_id&gt; → 得 task_id；
2. 周期调 render_status(task_id) → GET /render/status/&lt;task_id&gt;；
3. queued/rendering → 继续轮询；done → 取 mp4_path 交付；error → 报错并结束。

**状态流转**（任务状态由 S06 定义，本工具只读透传）：

| 当前状态 | 事件 | 目标状态 | 前置校验 | 后置动作 |
|---------|------|---------|---------|---------|
| —（提交） | render 调用 | queued | draft_id 已落盘 | 入 RENDER_QUEUE，返回 task_id |
| queued | 池 worker 取任务 | rendering | acquire_desktop 成功 | 驱动剪映真后台渲染 |
| rendering | 完成检测通过 | done | Videos/*.mp4 大小稳定 | 登记 mp4_path/mp4_name/duration |
| rendering | 驱动失败/超时 | error | 重试次数耗尽 | 记录失败原因 |

**约束条件：**
1. 渲染容量受 S06 双桌面池限制（2 并行），高并发提交时任务排队，轮询间隔不宜过密；
2. 渲染时长随草稿复杂度增长，Agent 会话须容忍分钟级轮询周期；
3. render_status 为只读查询，不产生副作用。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | BO-08-011/012 入参（draft_id / task_id） |
| 输出信息 | BO-08-011/012 返回（task_id / 四态状态 + 产物三元组），引用 S06 任务对象 |

**业务表单：** 无

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| render/render_status 调用（基本功能 1~3） | R02 外部 AI Agent |

##### 五、接口依赖

| 方向 | 对接系统 | 接口说明 | 数据格式 |
|------|---------|---------|---------|
| 输入 | EXT-06 MCP 客户端 | tools/call：create_draft/add_video/add_text/add_audio/save_draft/render/render_status 七个工具入口 | JSON-RPC over stdio |
| 输出 | localhost:9002 render_server（S05 VectCutAPI 融合编辑端点） | POST /create_draft、/add_video、/add_text、/add_audio、/save_draft | HTTP JSON |
| 输出 | localhost:9002 render_server（S06 渲染端点） | POST /render/draft/&lt;draft_id&gt;；GET /render/status/&lt;task_id&gt; | HTTP JSON |

##### 六、需求追溯

| 来源 | 引用 |
|------|------|
| 技术响应表 | 无标书响应表；数据源为系统功能手册 |
| 技术方案 | SYSTEM_MANUAL.md §14.1（7 个编辑/渲染工具定义）、§13.1（编辑端点 28 个之 5）、§16.1（渲染端点）、§5.3（任务生命周期）、§17 流程 C 步骤 3~4 |
| 优先级 | P1 |
| ▲标注 | 否 |

### 2.4 资源列表工具

#### S08-004 资源列表工具（get_animations）

##### 一、功能综述

`get_animations` 向外部 Agent 暴露系统的资源类型字典：Agent 传入类别参数 category，即可取得该类别下全部合法资源名列表，用于在调用编辑工具（S08-003）前获取转场、蒙版、字体、动画、特效等类型名的合法取值，避免模型凭空编造资源名导致 S05 端点校验失败。category 共 7 个取值——intro（入场动画）、outro（出场动画）、combo（组合动画）、transition（转场）、mask（蒙版）、font（字体）、effect（音效/特效）——分别映射 7 个 VectCutAPI 字典端点，一次调用返回一类完整清单。

本工具是编辑链路的"查字典"配套：典型用法为 Agent 先 get_animations(transition) 拿到转场列表，再把选定名称填入 add_video 的 transition 参数（手册 §14.1）。

##### 二、业务对象

| 编码 | 业务对象 | 数据项 | 数据类型 | 长度 | 必填 | 编码引用说明 | 备注 |
|------|---------|--------|---------|------|------|------------|------|
| BO-08-013 | get_animations 工具调用 | category | VARCHAR | 20 | Y | 枚举 7 值：intro/outro/combo/transition/mask/font/effect | inputSchema 中以 enum 声明 |
| | | items（返回） | TEXT(JSON) | - | Y | 该类别资源名列表（数组） | 供编辑工具参数取值 |
| | | error（返回） | TEXT | - | N | 非法类别错误描述 | isError=true 时返回 |

**category 取值与端点映射：**

| category 取值 | 含义 | 透传 REST 端点（localhost:9002） | 方法 |
|--------------|------|-------------------------------|------|
| intro | 入场动画列表 | /get_intro_animation_types | GET |
| outro | 出场动画列表 | /get_outro_animation_types | GET |
| combo | 组合动画列表 | /get_combo_animation_types | GET |
| transition | 转场列表 | /get_transition_types | GET |
| mask | 蒙版列表 | /get_mask_types | GET |
| font | 字体列表 | /get_font_types | GET |
| effect | 音效/特效列表 | /get_audio_effect_types | GET |

##### 三、业务活动

1. **类别查询**：Agent 传入 category → 映射对应字典端点 → GET 请求 → 返回资源名数组。
2. **取值回填**：Agent 从 items 中选定资源名，填入 add_video/add_text 等编辑工具参数。
3. **字典维护**：列表内容由 pyJianYingDraft 字典决定（S05-M04），本工具只读透传，不提供增删改。

##### 四、用例描述

##### 用例 U-08-004-01 按类别获取资源列表

| 项目 | 内容 |
|------|------|
| 用例编号 | U-08-004-01 |
| 用例名称 | 按类别获取资源列表（get_animations） |
| 业务说明 | Agent 在设置转场/字体/动画等参数前，调用本工具取得该类别全部合法资源名，保证编辑参数取值有效（手册 §14.1 get_animations 定义） |
| 规范引用 | 无 |
| 业务规则 | 1. category 必填且必须为 7 个枚举值之一（intro/outro/combo/transition/mask/font/effect），非法值在 Schema 校验阶段即拒绝并返回 isError。2. 每个枚举值映射唯一确定的 GET 字典端点（见映射表），映射关系静态固化于 Server，不可配置漂移。3. 返回 items 为该类别完整列表（数组），不得分页截断；列表内容以 S05 字典为准，本工具不得增删改。4. 字典端点属 VectCutAPI 融合路由，融合降级时本工具不可用并返回可读错误。 |
| 使用范围 | R02 外部 AI Agent |
| 先决条件 | 连接已建立；9002 服务已启动且 VectCutAPI 融合成功 |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 3 个基本功能点：|
| | 1. 校验 category 枚举合法（inputSchema enum） |
| | 2. 按映射表透传对应 GET 字典端点（7 选 1） |
| | 3. 返回该类别资源名列表 items |
| 辅助功能 | 工具 description 中列出 7 个类别取值，供模型直接选择 |
| 提示信息 | 非法 category、字典端点不可达时返回明确错误描述 |

**处理逻辑：**
1. 收到 get_animations(category)；
2. Schema 校验 category ∈ 7 枚举；
3. 查映射表得端点（如 transition → /get_transition_types）；
4. requests GET http://localhost:9002/&lt;端点&gt;；
5. 资源名数组序列化回传。

**约束条件：**
1. 本工具为只读查询，无副作用，可高频调用；
2. 枚举集合与端点映射须与 S05-M04 的 11 个字典端点保持一致（本工具覆盖其中 7 类；text_intro/text_outro/text_loop/video_scene_effect/video_character_effect 等其余字典端点暂未暴露为 category 取值，如需扩展在映射表追加枚举即可）；
3. 字典内容随 pyJianYingDraft 版本演进，工具层不做取值硬编码。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | BO-08-013 入参（category 枚举） |
| 输出信息 | BO-08-013 items（引用 S05 资源类型字典业务对象） |

**业务表单：** 无

**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| get_animations 调用（基本功能 1~3） | R02 外部 AI Agent |

##### 五、接口依赖

| 方向 | 对接系统 | 接口说明 | 数据格式 |
|------|---------|---------|---------|
| 输入 | EXT-06 MCP 客户端 | tools/call：get_animations 工具入口 | JSON-RPC over stdio |
| 输出 | localhost:9002 render_server（S05 VectCutAPI 字典端点） | GET /get_intro_animation_types、/get_outro_animation_types、/get_combo_animation_types、/get_transition_types、/get_mask_types、/get_font_types、/get_audio_effect_types（7 端点按 category 七选一） | HTTP JSON |

##### 六、需求追溯

| 来源 | 引用 |
|------|------|
| 技术响应表 | 无标书响应表；数据源为系统功能手册 |
| 技术方案 | SYSTEM_MANUAL.md §14.1（get_animations 定义与 category 7 取值）、§13.1（字典 GET 端点清单） |
| 优先级 | P2 |
| ▲标注 | 否 |

## 三、业务对象汇总

| 编码 | 业务对象 | 数据项数 | 所属功能点 | 说明 |
|------|---------|---------|-----------|------|
| BO-08-001 | MCP Server 配置（.mcp.json） | 5 | S08-001 | 配置文件结构（mcpServers/video-tools/command/args/cwd） |
| BO-08-002 | stdio 会话消息（JSON-RPC 2.0） | 7 | S08-001 | 协议消息（jsonrpc/method/params/id/result/isError/content） |
| BO-08-003 | 工具描述符（tools/list 条目） | 3 | S08-001 | name/description/inputSchema |
| BO-08-004 | perceive_video 工具调用 | 5 | S08-002 | 入参 4 + 返回 analysis |
| BO-08-005 | perceive_result 工具调用 | 5 | S08-002 | 入参 2 + 质检返回 3 |
| BO-08-006 | create_draft 工具调用 | 4 | S08-003 | 入参 3 + draft_id |
| BO-08-007 | add_video 工具调用 | 7 | S08-003 | 入参 6 + result |
| BO-08-008 | add_text 工具调用 | 8 | S08-003 | 入参 7 + result |
| BO-08-009 | add_audio 工具调用 | 3 | S08-003 | 入参 2 + result |
| BO-08-010 | save_draft 工具调用 | 2 | S08-003 | 入参 1 + draft_folder |
| BO-08-011 | render 工具调用 | 2 | S08-003 | 入参 1 + task_id |
| BO-08-012 | render_status 工具调用 | 5 | S08-003 | 入参 1 + 状态/产物 4 |
| BO-08-013 | get_animations 工具调用 | 3 | S08-004 | category + items + error |
| **合计** | **13 个业务对象** | **59** | | 覆盖 10 个具名 MCP 工具的入参/出参 |

## 四、接口汇总

| 序号 | 方向 | 对接系统/服务 | 接口 | 协议/格式 | 所属功能点 |
|------|------|-------------|------|----------|-----------|
| 1 | 输入 | EXT-06 MCP 客户端 | initialize 握手 + tools/list 工具发现 | MCP stdio（JSON-RPC 2.0） | S08-001 |
| 2 | 输入 | EXT-06 MCP 客户端 | tools/call 工具调用（全部 10 工具统一入口） | MCP stdio（JSON-RPC 2.0） | S08-001~004 |
| 3 | 输出 | localhost:9002（S07） | GET /health（启动自检） | HTTP JSON | S08-001 |
| 4 | 输出 | localhost:9002（S02） | POST /api/perceive（perceive_video 透传） | HTTP JSON | S08-002 |
| 5 | 输出 | localhost:9002（S02） | POST /perceive/result（perceive_result 透传） | HTTP multipart | S08-002 |
| 6 | 输出 | localhost:9002（S05） | POST /create_draft | HTTP JSON | S08-003 |
| 7 | 输出 | localhost:9002（S05） | POST /add_video | HTTP JSON | S08-003 |
| 8 | 输出 | localhost:9002（S05） | POST /add_text | HTTP JSON | S08-003 |
| 9 | 输出 | localhost:9002（S05） | POST /add_audio | HTTP JSON | S08-003 |
| 10 | 输出 | localhost:9002（S05） | POST /save_draft | HTTP JSON | S08-003 |
| 11 | 输出 | localhost:9002（S06） | POST /render/draft/&lt;draft_id&gt; | HTTP JSON | S08-003 |
| 12 | 输出 | localhost:9002（S06） | GET /render/status/&lt;task_id&gt; | HTTP JSON | S08-003 |
| 13 | 输出 | localhost:9002（S05） | GET /get_intro_animation_types | HTTP JSON | S08-004 |
| 14 | 输出 | localhost:9002（S05） | GET /get_outro_animation_types | HTTP JSON | S08-004 |
| 15 | 输出 | localhost:9002（S05） | GET /get_combo_animation_types | HTTP JSON | S08-004 |
| 16 | 输出 | localhost:9002（S05） | GET /get_transition_types | HTTP JSON | S08-004 |
| 17 | 输出 | localhost:9002（S05） | GET /get_mask_types | HTTP JSON | S08-004 |
| 18 | 输出 | localhost:9002（S05） | GET /get_font_types | HTTP JSON | S08-004 |
| 19 | 输出 | localhost:9002（S05） | GET /get_audio_effect_types | HTTP JSON | S08-004 |

## 五、需求追溯矩阵

| 功能编号 | 功能名称 | 数据源（SYSTEM_MANUAL.md） | 引用角色 | 引用外部系统 | 原型 | 优先级 | ▲ |
|---------|---------|--------------------------|---------|-------------|------|-------|---|
| S08-001 | MCP Server 配置与 stdio 传输（.mcp.json） | §14、§14.2、§4.5、§17 流程 C 步骤 1、§18.2 | R02、R04 | EXT-06 | G | P1 | 否 |
| S08-002 | 感知工具（perceive_video/perceive_result） | §14.1、§9、§16.2、§17 流程 C 步骤 2/4 | R02 | EXT-06 | G | P1 | 否 |
| S08-003 | 编辑与渲染工具（7 工具） | §14.1、§13.1、§16.1、§5.3、§17 流程 C 步骤 3~4 | R02 | EXT-06 | G | P1 | 否 |
| S08-004 | 资源列表工具（get_animations） | §14.1（category 7 取值）、§13.1 | R02 | EXT-06 | G | P2 | 否 |

> 追溯说明：本项目无标书响应表，全部需求追溯至唯一数据源 SYSTEM_MANUAL.md（2026-08-14 版）；工具数量以 §14.1 具名 10 个为准（标题"11 个"系数据源笔误，见 §1.2 勘误说明）。

---
*编制日期：2026-08-14　版本：V1.0　数据源：SYSTEM_MANUAL.md + _metadata.md*
