# 免安装渲染节点（Portable Render Node）

本文件夹是一个完整的、解压即用的 Win10 渲染节点。无需安装 Python、无需命令行，
双击 `start_here.html` 按引导操作即可。

> 本文件是 `start_here.html` 的纯文字镜像备份。**优先看 `start_here.html`**（带排版、按钮、折叠答疑）；
> 这里供无浏览器或想全文搜索的用户查阅。

---

## 这是什么

你的 web 后端（AI 视频工作台）提交的渲染任务，需要一个装有剪映的 Windows 机器来实际导出 mp4。
本包把这台机器需要的一切（Python、依赖库、ffmpeg、剪映官方安装器、全部脚本）打包在一起，
解压 + 简单配置后，你的这台 Win10 就成为一个**专属渲染节点** —— 渲染优先走你本机的剪映，
公共节点作为兜底。

包内已自带：

- **Python 3.12.8 embeddable**（`python\`，不写注册表、不动系统 PATH）
- **依赖库**（`python\Lib\site-packages\`）：frida、flask、flask-cors、waitress、requests、python-dotenv
- **ffmpeg / ffprobe**（`ffmpeg\`，静态构建）
- **剪映官方安装器**（`capcut-installer\JianyingPro_*.exe`）
- **6 个项目脚本**（`app\`）：render_service.py、render_driver.py、render_monitor.py、hook_focus.js、config.py、task_store.py
- **启动 / 校准 / 停止脚本**：start.bat、calibrate.bat、stop.bat
- **本引导页**：start_here.html、README.md

---

## 操作步骤（8 步，约 15 分钟）

### 01　解压安装包

把 `autocut-render-node-portable.zip` 解压到任意目录。右键 zip →「全部解压」即可。

解压后得到 `autocut-render-node` 文件夹，里面有 `start_here.html`、`config.env`、
`start.bat`、`calibrate.bat`、`fix_update.bat`、`python\`、`ffmpeg\`、`app\`、`capcut-installer\` 等。

> **建议**：解压路径尽量不含中文和空格，例如 `D:\autocut-render-node`。放桌面也行。

### 02　安装剪映

打开解压目录里的 `capcut-installer` 文件夹，双击里面的 `剪映专业版5.9.exe`（剪映官方安装器），
按默认选项一路「下一步」装完。

装完可在开始菜单搜到「剪映专业版」。建议**首次打开剪映并完成登录**，确保它能正常启动到主界面。

- 剪映默认装到 `%LocalAppData%\JianyingPro\Apps`，本包的配置已经对齐这个路径，无需手动指定。
- **找不到 `剪映专业版5.9.exe`？** 说明打包时漏放了安装器。请到剪映官网 `jianying.com` 下载 Windows 版安装包，
  放进该文件夹后再继续。本渲染节点没有剪映就无法工作。
- **已装过剪映？** 只要 `%LocalAppData%\JianyingPro\Apps\<版本号>\JianyingPro.exe` 存在即可，可直接跳到第 2½ 步。

### 2½　禁用剪映自动升级

**装完剪映后、校准前，务必做这一步。** 双击 `fix_update.bat`，它会找到剪映的更新器
（`Apps\<版本号>\update.exe`）并改名成 `update.exe.bak`，从而锁定版本、阻止自动升级。

- **预期**：命令行窗口显示 `[版本号] update.exe -> update.exe.bak (已禁用)`，最后提示
  「已禁用剪映自动升级. 当前版本将被锁定」。
- **为什么必须做**：本节点的**校准坐标和草稿格式都绑定剪映版本**。一旦剪映自动升级到新版本，
  按钮位置和草稿结构都可能变化，导致校准失效、渲染失败。把版本锁定在 5.9 才能长期稳定运行。
- **提示「没有找到 update.exe」？** 可能剪映还没装好（回第 2 步），或这一版没有独立更新器。
  若剪映已能正常使用，可直接继续下一步校准；但仍建议留意剪映弹出的升级提示，**不要点升级**。
- **以后剪映手动重装了？** 手动重装 / 覆盖安装会恢复 `update.exe`。那时只需**再双击一次
  `fix_update.bat`** 即可重新禁用（脚本会自动清理旧的 `.bak` 再改名）。

### 03　配置 config.env

用记事本打开解压目录里的 `config.env`。一般只需改一个地方：**设置调用令牌 `RENDER_SERVICE_TOKEN`**。

在等号后面填上一个随机串（建议 32 位以上）。生成方法二选一：

- `start_here.html` 第 3 步有「生成随机令牌」按钮，点一下即可生成并复制；
- 或在 PowerShell 里运行：

  ```powershell
  -join ((48..57)+(65..90)+(97..122) | Get-Random -Count 32 | % {[char]$_})
  ```

填成：

```env
RENDER_SERVICE_TOKEN=<生成的随机串>
```

其余项通常不用动：

- `RENDER_SERVICE_HOST=0.0.0.0` —— 允许局域网访问，保持不变。
- `RENDER_SERVICE_PORT=9020` —— 端口，没冲突就别改。
- 剪映路径三项保持注释（默认推导），除非把剪映装到了非默认位置。

> **记下这个令牌**：第 7 步填回 web 后端时要用到**同一个**令牌。令牌是这台节点和你账号之间的密码，
> 两边必须一致。留空虽然能跑，但任何能连到这台机器的人都能调用它，不建议。

### 04　首跑校准

双击 `calibrate.bat`。它会打开剪映，然后你在命令行窗口按提示**手动点击 5 个位置**：

1. 导出卡片（草稿列表里某个草稿的卡片）
2. 导出按钮
3. 确认导出
4. 完成后关闭弹窗
5. 关闭编辑器

- **预期**：校准成功后，`app\` 文件夹里会生成 `calib.json`。calibrate.bat 结束时会提示「已生成 calib.json」。
- **每台机器只需一次**：calib.json 记录的是这台机器、这个屏幕分辨率、这个剪映版本下的按钮坐标。
  以后只要不换屏幕分辨率、不换剪映大版本，就不用重跑。本包不会预置 calib.json，必须在自己机器上校准一次。
- **校准报错 / 没弹出剪映？** 先确认第 2 步剪映装好且能手动打开到主界面；校准前最好先手动登录剪映账号；
  剪映窗口位置怪异就拖到屏幕左上角再校准；屏幕缩放建议设为 100%。

### 05　启动服务

双击 `start.bat`。会弹出一个黑色命令行窗口，显示 `render_service 已启动`。

- **预期**：窗口保持打开，服务在后台运行。**不要关闭这个窗口**，关闭即停止服务。
- 停止也可以双击 `stop.bat`。
- **想后台无窗口运行？** 用记事本打开 `start.bat`，把里面的 `python.exe` 改成 `pythonw.exe` 保存，
  再双击就不会弹窗（但也看不到日志了，首跑不建议）。
- **提示端口 9020 被占用？** 说明已有别的程序（或上一个没关的 render_service）占着 9020。
  双击 `stop.bat` 清掉，再重新双击 `start.bat`。若仍占用，可在 config.env 里把 `RENDER_SERVICE_PORT`
  改成别的端口（如 9021），但第 7 步填回 web 后端的端口也要同步改。

### 06　验证连接

打开浏览器，访问（本机自测用 127.0.0.1）：

```
http://127.0.0.1:9020/health
```

- **预期**：返回类似
  `{"ok":true,"service":"render_service","videos_dir":"...","desktops":["JYRender_0"]}` 的 JSON。
  看到 `"ok":true` 就说明服务正常。

如果你要让**另一台电脑**（web 后端所在的服务器）连过来，要用这台机器的局域网 IP：

```powershell
ipconfig | findstr IPv4
```

找到形如 `192.168.x.x` 的那一行，那台电脑应访问 `http://本机IP:9020/health`。

- **别的电脑访问不通 / 超时？** 多半是 Windows 防火墙拦了 9020 端口。到
  「Windows 安全中心 → 防火墙 → 高级设置 → 入站规则」新建一条允许 TCP 9020 的规则，
  或临时关闭防火墙测试。另确认两台机器在同一局域网、能互相 ping 通。
- **本机 127.0.0.1 也访问不了？** 回到第 5 步，确认 start.bat 窗口还开着、显示「已启动」。
  窗口里若有红色报错（如找不到剪映、frida 错误），按提示处理。

### 07　填回 web 后端

打开你的 web 后端（AI 视频工作台），登录后点左侧 **Render Node** 标签页，填入：

- **Render Service URL**：`http://本机IP:9020`
  （本机自测可填 `http://127.0.0.1:9020`；若 web 后端在另一台机器，用第 6 步查到的本机局域网 IP）
- **X-Render-Token**：第 3 步在 config.env 里设的 `RENDER_SERVICE_TOKEN` 值（两边必须一致）

点 **测试连接**，应返回「连接成功 · render_service · desktops: JYRender_0」。然后点 **Save** 保存。

- **完成**：之后你提交的渲染任务会优先走这台节点（用你本机的剪映导出）。
  如果这台节点连不上或提交失败，会自动回退到公共节点，并在任务上标注回退原因。

> **日常使用**：以后每次要用，只要双击 `start.bat` 启动服务即可（校准过的机器不用再校准）。
> 不用时双击 `stop.bat` 或直接关掉 start.bat 的窗口。config.env 改过后需要重启 start.bat 生效。

---

## 目录结构

```
autocut-render-node/
├─ start_here.html          ← 双击入口（引导页）
├─ README.md                ← 本文件
├─ config.env               ← 用户改这个（令牌等）
├─ start.bat                ← 启动 render_service
├─ calibrate.bat            ← 首跑校准
├─ stop.bat                 ← 停止服务
├─ fix_update.bat           ← 禁用剪映自动升级（装完剪映后跑一次）
├─ python\                  ← 自带 Python 3.12.8（不动系统）
│  └─ Lib\site-packages\    ← frida / flask / waitress / requests / dotenv
├─ ffmpeg\                  ← ffmpeg.exe + ffprobe.exe
├─ capcut-installer\        ← 剪映官方安装器
│  └─ JianyingPro_*.exe
└─ app\                     ← 项目脚本（运行时目录）
   ├─ render_service.py
   ├─ render_driver.py
   ├─ render_monitor.py
   ├─ hook_focus.js
   ├─ config.py
   ├─ task_store.py
   ├─ .env                  ← start.bat 启动时从根 config.env 拷来
   ├─ calib.json            ← 第 4 步校准生成（每机不同，不预置）
   └─ render_uploads\       ← 渲染临时文件（运行时生成）
```

---

## 常见问题

**Q：必须装剪映吗？能不能不装？**
必须。本节点通过 frida 驱动剪映的 GUI 来导出视频，剪映本身做全部 H.264/字幕编码。
没有剪映就没有渲染。这也是为什么包里要带剪映官方安装器。

**Q：config.env 里的 QWEN_API_KEY / ASR_API_KEY 在哪？**
没有，也不需要。那两个是大模型 / 语音识别密钥，是 web 后端用的；渲染节点只驱动剪映导出，
不调大模型、不做语音识别，所以配置里不含这些 key。

**Q：换台电脑能用同一个包吗？**
能。把整个 `autocut-render-node` 文件夹拷过去即可（Python/ffmpeg/脚本都是可重定位的）。
但新机器需要：① 装剪映（第 2 步）；② 重新校准（第 4 步，calib.json 是每机不同的）；
③ 视情况改 config.env 里的令牌。`app\calib.json` 不要跨机器复制。

**Q：服务开机自启怎么做？**
把 `start.bat` 的快捷方式放进「开始菜单 → 启动」文件夹即可。注意：本节点**不能**作为
Windows 服务运行 —— 它需要交互式桌面会话（frida 要连剪映的 GUI 进程）。所以必须在你登录后启动。

**Q：想多路并行渲染怎么办？**
在 config.env 里把 `DESKTOP_NAMES` 改成多个（逗号分隔，如 `JYRender_0,JYRender_1`），
每个桌面独立跑一路剪映。但每个桌面要分别校准，且对机器性能要求更高。一般 1 路够用。
