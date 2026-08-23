# S06-M03 Frida 注入层 — 需求规格

## 子模块信息

| 项目 | 内容 |
|------|------|
| 所属系统 | S06 无人值守渲染 |
| 子模块编号 | S06-M03 |
| 功能点范围 | S06-012 ~ S06-014 |
| 功能点数 | 3 |
| 功能原型 | G 集成/对接（012/013）、D 观测（014）、F 配置（按 _metadata 012~014 兼 F） |
| 上游 | S06-M02 渲染驱动闭环（IF-06-09 attach + RPC 调用） |
| 下游 | EXT-01 剪映（Qt6 内部函数注入）、R04 运维（监视器日志） |

## 2.3 Frida 注入层
### S06-012 Frida 合成事件层（click/clickmodal/typewm/状态 RPC）
#### 一、功能综述
hook_focus.js 是注入剪映主进程的 Frida 脚本，把"程序化点击/输入"能力以 rpc.exports
形式暴露给 Python 驱动层。全部 Qt 函数地址按 mangled 导出名子串动态查找，PID/基址
变化均可复用。核心入口为 QWindowSystemInterface::handleMouseEvent/handleKeyEvent：从
agent 线程调用不触发线程断言，Qt 事件循环接受合成事件直达真实按钮（手册 §20.2 关键
突破 1）。共 8 个 RPC：click/clickmodal/clicklocal/clickglobal/typewm/status/lastshown/
windiff，覆盖点击、modal 点击、桌面模式键盘输入与运行状态查询。
#### 二、业务对象

| 编码 | 业务对象 | 数据项 | 数据类型 | 长度 | 必填 | 编码引用说明 | 备注 |
|------|---------|--------|---------|------|------|------------|------|
| BO-06-012 | Frida RPC 调用 | rpc_name | VARCHAR | 16 | Y | click/clickmodal/clicklocal/clickglobal/typewm/status/lastshown/windiff | 8 个枚举 |
| | | args | TEXT(JSON) | - | Y | lx/ly 或 text / (bc,bs) | 按 rpc 定义 |
| | | result | TEXT(JSON) | - | N | 返回值（状态/窗口信息） | status/lastshown 有值 |
| | | ok | 布尔 | 1 | Y | 调用是否成功 | 失败上抛驱动层 |

> 8 个 RPC 语义：click(lx,ly)=focusWindow+setGeometry+handleMouseEvent 按下与释放；
> clickmodal(lx,ly)=用 lastShownWin（最近显示 modal）点击；clicklocal(lx,ly)=local 坐标点击；
> clickglobal(gx,gy)=global 坐标点击（旧法，窗口移位会偏）；typewm(text)=桌面模式经
> PostMessageW(WM_CHAR) 跨桌面输入；status()=返回 curWin/curDev/showCount；
> lastshown()=返回最近显示窗口+showCount；windiff(bc,bs)=相对基线的窗口创建/显示增量。
#### 三、业务活动
Python 驱动层 attach 剪映进程（跨桌面 by PID）→ 加载 hook_focus.js 动态解析 Qt6 导出
（focusWindow/x/y/primaryPointingDevice/winId/setGeometry/handleMouseEvent/handleKeyEvent/
setVisible）→ 驱动按闭环阶段调用相应 RPC → 接收结果/异常并回写闭环上下文。
#### 四、用例描述
##### 用例 U-06-012-01 合成事件 RPC 调用

| 项目 | 内容 |
|------|------|
| 用例编号 | U-06-012-01 |
| 用例名称 | 经 rpc.exports 注入合成鼠标/键盘事件 |
| 业务说明 | 驱动层在导出闭环各阶段调用 8 个 RPC 完成点击、modal 点击、输入与状态探测 |
| 规范引用 | 版本锁定：剪映 JianyingPro 5.9.0.11632（mangled 导出名按版本回归） |
| 业务规则 | 1. 全部 Qt 函数地址按 mangled 导出名子串动态查找，不写死地址，PID/模块基址变化仍可用；2. 点击类 RPC（click/clickmodal/clicklocal/clickglobal）坐标一律用 local 坐标（BO-06-011），global 仅保留兼容旧用法且窗口移位会偏；3. clickmodal 必须用于 modal 窗口（confirm/close_done），其目标窗口为 lastShownWin 而非 focusWindow；4. typewm 桌面模式经 user32 PostMessageW(WM_CHAR) 实现跨桌面输入，窗口句柄经 QWindow::winId 获取；5. 导出循环每轮 base_show 与 modal 判定经 status()/lastshown()/windiff(bc,bs) 组合完成；6. 任一 RPC 失败（ok=false）须上抛驱动层进入相应重试分支，不得静默吞错。 |
| 使用范围 | R04 系统运维/开发者（注入链路维护）；渲染闭环（S06-008）为自动调用方 |
| 先决条件 | 剪映进程已启动（BO-06-007.jy_pid）；Frida 可 attach（跨桌面 by PID）；Qt6 导出解析成功 |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 8 个基本功能点（与 8 个 RPC 一一对应）：|
| | 1. click(lx,ly)：focusWindow+setGeometry+handleMouseEvent（按下+释放） |
| | 2. clickmodal(lx,ly)：以 lastShownWin 为目标点击 modal 坐标 |
| | 3. clicklocal(lx,ly)：local 坐标点击；4. clickglobal(gx,gy)：global 坐标点击（兼容保留） |
| | 5. typewm(text)：PostMessageW(WM_CHAR) 跨桌面键盘输入 |
| | 6. status()：返回 curWin/curDev/showCount；7. lastshown()：返回最近显示窗口+showCount |
| | 8. windiff(bc,bs)：相对基线的窗口创建/显示增量（modal 判定依据） |
| 辅助功能 | dev 兜底：primaryPointingDevice 失败时借 handleMouseEvent hook 捕获真实点击的 dev |
| 提示信息 | 导出解析失败/RPC 异常写入 focus_&lt;pid&gt;.txt 调试日志 |

**处理逻辑：**
1. attach 后按 mangled 名子串解析 Qt6 导出并构造 NativeFunction；
2. 驱动按阶段选择 RPC：首页/编辑器点击用 click，modal 用 clickmodal，检索输入用 typewm；
3. 每轮导出取 status/lastshown/windiff 判定窗口变化，结果回写 BO-06-008。

**约束条件：**
1. NativeFunction 类型名只允许 uint/pointer，不可用 uint32/uintptr（抛 invalid type）；
2. PostMessage 参数声明为 pointer 时必须传 ptr(value)，不能传裸数字；winId 返回 pointer
   非 uint64，禁止转 uint64；
3. Module.getExportByName 不适用，user32 导出须经 Process.getModuleByName('user32.dll')
   .getExportByName('PostMessageW') 获取；
4. handleMouseEvent send() 携带复杂 payload 会崩溃，跨进程数据用 File API 写文件替代；
5. hook 脚本内变量禁止命名为 ptr（遮蔽全局 ptr() 函数）。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | BO-06-011 坐标 / 文本输入 / (bc,bs) 基线（IF-06-09） |
| 输出信息 | BO-06-012 RPC 结果；BO-06-013 窗口跟踪状态更新 |

**业务表单：** 无。**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 合成事件 RPC（系统自动） | R04 系统运维/开发者（注入排障） |

#### 五、接口依赖

| 方向 | 对接系统 | 接口说明 | 数据格式 |
|------|---------|---------|---------|
| 输出 | EXT-01 剪映 | IF-06-09 Frida attach + rpc.exports 8 个 RPC | Frida RPC（JS） |
| 输入 | S06-M02 | 驱动层调用（坐标/文本/基线） | 函数参数 |

#### 六、需求追溯

| 来源 | 引用 |
|------|------|
| 技术响应表 | 无标书响应表；数据源 SYSTEM_MANUAL §7.2 核心 RPC、§7.1 Qt6 导出表 |
| 技术方案 | SYSTEM_MANUAL §19.5 Frida 已知坑、§20.2 突破 1/2/3 |
| 优先级 | P1 |
| ▲标注 | 否 |
### S06-013 modal 检测与窗口跟踪（setVisible hook + focusWindow）
#### 一、功能综述
剪映的导出弹窗与完成弹窗是独立 QWindow（不属于编辑器窗口），且窗口会随 UI 导航重建，
因此不能缓存窗口指针。方案有三：其一，QGuiApplication::focusWindow() 静态调用返回
当前聚焦 QWindow，自动跟踪 home→editor→dialog 切换，agent 线程可用（关键突破 2）；
其二，hook QWindow::setVisible 记录 lastShownWin 与 showCount（累计显示计数），modal
出现即被捕获，clickmodal 据此点击；其三，每次点击前经 QWindow::setGeometry(403,108,
1280,720) 固定窗口位置，消除 Windows 随机放置导致的坐标偏移（三大根因修复之一）。
#### 二、业务对象

| 编码 | 业务对象 | 数据项 | 数据类型 | 长度 | 必填 | 编码引用说明 | 备注 |
|------|---------|--------|---------|------|------|------------|------|
| BO-06-013 | 窗口跟踪状态 | curWin | POINTER | - | Y | focusWindow() 当前值 | 随 UI 导航自动切换 |
| | | curDev | POINTER | - | Y | primaryPointingDevice() | 合成事件必要参数 |
| | | lastShownWin | POINTER | - | Y | setVisible(true) 最近窗口 | modal 检测依据 |
| | | showCount | INT | 4 | Y | 累计窗口显示计数 | editor_ready/modal 判定 |

#### 三、业务活动
hook 安装（setVisible 拦截 + 导出解析）→ 持续更新 curWin/lastShownWin/showCount →
向 RPC 层提供窗口目标 → 点击前 setGeometry 固定窗口 → UI 导航后指针自动刷新。
#### 四、用例描述
##### 用例 U-06-013-01 modal 检测与窗口自动跟踪

| 项目 | 内容 |
|------|------|
| 用例编号 | U-06-013-01 |
| 用例名称 | focusWindow 跟踪 + setVisible hook 捕获 modal |
| 业务说明 | 闭环全程自动锁定当前窗口，捕获导出/完成 modal 供 clickmodal 点击 |
| 规范引用 | 版本锁定：剪映 5.9.0（setVisible/focusWindow mangled 名按版本回归） |
| 业务规则 | 1. 禁止缓存窗口指针跨 UI 导航使用：QWindow 在页面切换时会重建，click/clickmodal 每次调用现取 focusWindow()/lastShownWin；2. modal 检测以 hook QWindow::setVisible 记录的 lastShownWin 为准——导出弹窗是独立 QWindow，经 focusWindow 点击会落空；3. showCount 为累计显示计数，editor_ready 判据 = showCount 相对打开编辑器前增加且窗口稳定 1.5s（S06-008 五次重试的依据）；4. 每次点击前 setGeometry(403,108,1280,720) 固定窗口位置与尺寸，保证 local 坐标换算稳定；5. dev 经 primaryPointingDevice 程序化获取，失败时以真实点击种子兜底（handleMouseEvent hook 捕获 dev）；6. status()/lastshown() 如实暴露 curWin/curDev/lastShownWin/showCount 供驱动层判定。 |
| 使用范围 | R04 系统运维/开发者；渲染闭环（S06-008）为自动调用方 |
| 先决条件 | hook_focus.js 已注入；Qt6 相关导出（focusWindow/setVisible/setGeometry/winId/x/y）解析成功 |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 4 个基本功能点：|
| | 1. focusWindow 自动跟踪（home→editor→dialog 切换） |
| | 2. setVisible hook 维护 lastShownWin + showCount（modal 捕获） |
| | 3. 点击前 setGeometry 固定窗口（403,108,1280,720） |
| | 4. status/lastshown 暴露窗口跟踪状态（BO-06-013） |
| 辅助功能 | QWindow::x/y 取窗口原点，供 local→global 换算（global = origin + local） |
| 提示信息 | 窗口指针失效/解析失败写入 focus_&lt;pid&gt;.txt |

**处理逻辑：**
1. 注入后解析 focusWindow/x/y/setGeometry/setVisible 导出并安装 hook；
2. setVisible(true) 触发时更新 lastShownWin 并 showCount+1；
3. 每次 click/clickmodal 现取目标窗口，点击前 setGeometry 固定，再合成事件。

**约束条件：**
1. winId 返回 pointer 而非 uint64，禁止数值化转换（跨桌面 PostMessage 需 ptr(value)）；
2. 不缓存跨导航窗口指针（QWindow 重建导致野指针）；
3. setGeometry 参数须与校准基准 1280×720 一致（BO-06-011）。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | EXT-01 Qt6 内部函数（focusWindow/setVisible/setGeometry/winId/x/y） |
| 输出信息 | BO-06-013 窗口跟踪状态 → S06-008 闭环判定 |

**业务表单：** 无。**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 窗口跟踪与 modal 检测（系统自动） | R04 系统运维/开发者（排障） |

#### 五、接口依赖

| 方向 | 对接系统 | 接口说明 | 数据格式 |
|------|---------|---------|---------|
| 输出 | EXT-01 剪映 | IF-06-09 Qt6 导出 hook（setVisible/focusWindow/setGeometry） | Frida hook |
| 输出 | S06-012 | 为 click/clickmodal/status/lastshown 提供窗口目标 | BO-06-013 |

#### 六、需求追溯

| 来源 | 引用 |
|------|------|
| 技术响应表 | 无标书响应表；数据源 SYSTEM_MANUAL §7.3 关键技术点、§7.1 导出表 |
| 技术方案 | SYSTEM_MANUAL §19.5（不缓存指针/winId 陷阱）、§20.2 突破 2/5 |
| 优先级 | P1 |
| ▲标注 | 否 |
### S06-014 渲染监视器（render_monitor.py 文件轮询状态机）
#### 一、功能综述
render_monitor.py 是独立的文件轮询后台进程，作为 wait_render_done（S06-009）的补充
观测手段：同时监视最终输出目录 C:\Users\Administrator\Videos\（mp4）与临时目录
.__jianying_export_temp_folder__（temp 文件），以 idle→rendering→done 三态状态机
记录渲染过程，日志写入 render_monitor.log。定位为调试观察：快速渲染时 temp 闪现
太快，主流程完成判定以 S06-009 按输出名轮询为准（手册 §8）。
#### 二、业务对象

| 编码 | 业务对象 | 数据项 | 数据类型 | 长度 | 必填 | 编码引用说明 | 备注 |
|------|---------|--------|---------|------|------|------------|------|
| BO-06-014 | 监视器状态 | state | VARCHAR | 10 | Y | 枚举 idle/rendering/done | 状态机字段 |
| | | watch_dirs | VARCHAR | 260 | Y | Videos\ 与 temp 目录 | 目录轮询见 IF-06-11 |
| | | latest_mp4 | VARCHAR | 260 | N | 最新成片路径 | 大小稳定后记录 |
| | | log_path | VARCHAR | 260 | Y | render_monitor.log | 每次迁移追加 |

#### 三、业务活动
后台启动轮询 → 监视 temp 与 mp4 目录 → 状态机迁移（idle→rendering→done）→
状态与事件写入 render_monitor.log → 供 R04 观察渲染过程。
#### 四、用例描述
##### 用例 U-06-014-01 渲染过程观察与状态记录

| 项目 | 内容 |
|------|------|
| 用例编号 | U-06-014-01 |
| 用例名称 | 监视器跟踪一次渲染并记录状态机迁移 |
| 业务说明 | 运维观察后台渲染进行情况；temp 出现即视为渲染开始，temp 清空且 mp4 大小稳定视为完成 |
| 规范引用 | 无 |
| 业务规则 | 1. 状态枚举仅 idle/rendering/done 三值（_metadata 监视器状态约定）；2. temp 文件出现即迁移至 rendering（RENDER STARTED）；3. temp 清空且最新 mp4 大小稳定即迁移至 done（RENDER DONE），记录 latest_mp4；4. 新一轮渲染开始时从 done 回到 rendering（重置观测基线）；5. 本监视器仅作调试观察，不参与主流程判定，结论与 S06-009 冲突时以 S06-009 为准；6. 每次状态迁移必须追加写 render_monitor.log（含时间戳）。 |
| 使用范围 | R04 系统运维/开发者 |
| 先决条件 | render_monitor.py 后台进程运行；输出/临时目录可访问 |

**功能要求：**

| 类型 | 描述 |
|------|------|
| 基本功能 | 共计 4 个基本功能点：|
| | 1. 轮询监视 Videos（mp4）与 temp 目录 |
| | 2. 状态机迁移判定（idle→rendering→done） |
| | 3. 状态迁移日志追加（render_monitor.log） |
| | 4. 最新成片 latest_mp4 记录 |
| 辅助功能 | 后台进程独立于 render_server 运行，可单独启停 |
| 提示信息 | 目录不可访问时日志告警 |

**处理逻辑：**
1. 周期扫描 temp 与 mp4 目录；
2. temp 出现 → rendering；temp 清空且 mp4 大小稳定 → done 并记录 latest_mp4；
3. 全部迁移写日志，等待下一轮渲染回到 rendering。

**状态流转：**

| 当前状态 | 事件 | 目标状态 | 前置校验 | 后置动作 |
|---------|------|---------|---------|---------|
| idle | temp 目录出现渲染临时文件 | rendering | 当前无进行中观测 | 日志记 RENDER STARTED |
| rendering | temp 清空且最新 mp4 大小稳定 | done | mp4 存在且大小不再增长 | 日志记 RENDER DONE，记 latest_mp4 |
| done | 新一轮 temp 文件出现 | rendering | 新渲染已开始 | 重置观测基线，日志记 RENDER STARTED |

**约束条件：**
1. 监视器只读文件系统，不得干预渲染流程；
2. 快速渲染时 temp 可能闪现过快，rendering 态可能被跳过（允许 idle→done 观测降级）；
3. 日志追加写，不覆盖历史。

**信息处理要求：**

| 方向 | 内容 |
|------|------|
| 输入信息 | IF-06-11 输出目录 + temp 目录文件状态 |
| 输出信息 | BO-06-014 监视器状态 + render_monitor.log |

**业务表单：** 无。**角色权限：**

| 功能点 | 使用人员 |
|--------|---------|
| 渲染监视器启停与日志观察 | R04 系统运维/开发者 |

#### 五、接口依赖

| 方向 | 对接系统 | 接口说明 | 数据格式 |
|------|---------|---------|---------|
| 输入 | EXT-01 剪映 | IF-06-11 Videos 与 temp 目录轮询 | 文件系统 |
| 输出 | R04 运维 | IF-06-12 render_monitor.log 状态日志 | 日志文件 |

#### 六、需求追溯

| 来源 | 引用 |
|------|------|
| 技术响应表 | 无标书响应表；数据源 SYSTEM_MANUAL §8 完成检测：render_monitor.py |
| 技术方案 | SYSTEM_MANUAL §6.5（与 wait_render_done 的分工） |
| 优先级 | P2 |
| ▲标注 | 否 |

## 本子模块 BO 数据字典汇总

| BO 编码 | 名称 | 字段数 | 关键字段 |
|---------|------|--------|---------|
| BO-06-012 | Frida RPC 调用 | 4 | rpc_name(8 个 RPC)/args/result/ok |
| BO-06-013 | 窗口跟踪状态 | 4 | curWin/curDev/lastShownWin/showCount |
| BO-06-014 | 监视器状态 | 4 | state(idle/rendering/done)/latest_mp4/log_path |
