---
name: mac-screen-record
description: macOS 命令行录屏与视频处理一条龙：录屏转 mp4（可指定时长/区域/显示器/麦克风）、截图、视频切片、转 GIF、压缩，以及智能加字幕（点击/快捷键事件采集 + Vision OCR 自动生成，或抽关键帧由 Agent 读图写字幕后烧录）。触发词：录屏、录制屏幕、屏幕录制、录个视频、录教程、加字幕、视频转gif、视频切片、剪视频、截屏、screenshot、screen record。不用于：非 macOS 平台、直播推流。
version: 1.4.0
---

# mac-screen-record

macOS 命令行录屏 / 截图 / 切片 / GIF / 压缩 / 智能字幕。全流程脚本化，适合 Agent 调用。

**以下命令均在技能目录下执行**（`python3 scripts/screen_rec.py …`）。

## 环境准备（首次使用 / 换机先跑）

```bash
python3 scripts/screen_rec.py ensure
```

`ensure` 会依次检查并尽量自动补齐：

| 依赖 | 用途 | 缺失时 ensure 的动作 |
|---|---|---|
| `screencapture` | 录屏、截图 | 系统自带，只检查 |
| `ffmpeg` / `ffprobe` | 转码、切片、GIF、字幕 | 有 Homebrew 则 `brew install ffmpeg`；没有则提示先装 Homebrew |
| `Pillow` | 字幕文字渲染 | `pip install --target ~/.cache/mac-screen-record/pydeps-pyXY`（不污染系统 Python） |
| `swiftc`（Xcode CLT） | 事件采集 / OCR / 录制悬浮窗 | 触发 `xcode-select --install` 弹窗，需人工完成 |
| 中文字体 | 字幕渲染 | 检查系统字体并报告 |
| 辅助程序 | screen_info / ocr / event_tap / rec_overlay | 按需编译到缓存目录 |

- 只检查不安装：`ensure --check`
- 结构化输出：`ensure --json`
- **每个命令开跑前都会做轻量预检**：缺依赖时直接报「缺少依赖：… / 先运行 ensure」，不会先录了再在转码时失败

## 系统权限（运行时）

```bash
python3 scripts/screen_rec.py permissions           # 检查四类权限
python3 scripts/screen_rec.py permissions --open    # 打开缺失权限的系统设置面板
python3 scripts/screen_rec.py permissions --request # 触发系统授权弹窗（屏幕录制/输入监控）
```

| 权限 | 用途 | 缺失时的行为 |
|---|---|---|
| 屏幕录制 | 录屏、截图、采集电脑声音 | `record` / `shot` 直接报错并给出设置路径，不会产出无效文件 |
| 输入监控 | 点击/快捷键事件采集（智能字幕自动模式） | 录屏照常；事件采集降级，字幕自动转「抽帧 + AI 读图」 |
| 辅助功能 | 部分系统版本的事件采集兜底 | 一般不需要；事件采集失败时再试 |
| 麦克风 | `--audio mic` 录人声 | 开启时先检查；未询问则首次录制弹系统授权 |

授权后**必须完全退出并重开终端/宿主 App**。`ensure`、`doctor`、`permissions` 都会显示当前状态；`record`/`shot` 缺屏幕录制权限会直接拒绝执行（`--force` 可跳过预检）。

只读自检（不安装）：`python3 scripts/screen_rec.py doctor`。

## 命令地图

| 想做 | 命令 |
|---|---|
| 录屏 30 秒 | `record -d 30` |
| 录屏并出 GIF | `record -d 20 --gif` |
| 录完转 mp4（已有视频） | `compress input.mov` |
| 截图 | `shot`、`shot --region 0,0,1200,800` |
| 视频切片 | `clip input.mp4 --start 00:00:10 --end 00:00:25` |
| 转 GIF | `gif input.mp4 --width 960 --fps 10` |
| 加字幕 | `subtitle auto`（离线）/ `subtitle draft` + `subtitle burn`（AI） |
| 环境准备（首次先跑） | `ensure`（`--check` 只检查） |
| 系统权限 | `permissions`（`--open` 打开设置面板） |
| 环境自检 | `doctor` |

所有子命令都支持 `--json`（放子命令前后均可），便于脚本/Agent 解析返回里的路径。

## 录屏

**开始录制前先确认声音模式**（用户已明确时跳过）。用 AskUserQuestion 问：

1. 纯录屏，无声音 → `--audio none`
2. 录屏 + 电脑播放的声音 → `--audio system`
3. 录屏 + 讲解旁白 → `--audio mic`
4. 录屏 + 电脑声音 + 讲解旁白（混音）→ `--audio mix`
5. 指定音频设备（高级）→ `--audio <设备ID>`

```bash
# 录 30 秒，默认 1080p / 30fps / mp4
python3 scripts/screen_rec.py record -d 30

# 指定输出 + 同时生成 GIF
python3 scripts/screen_rec.py record -d 20 -o ~/Desktop/demo.mp4 --gif

# 只录一块区域（全局坐标 x,y,w,h，单位 point）
python3 scripts/screen_rec.py record --region 0,0,1200,800 -d 10

# 录第二块屏 / 录麦克风 / 显示鼠标与点击效果
python3 scripts/screen_rec.py record --display 2 -d 10
python3 scripts/screen_rec.py record --audio mic -d 30 --cursor --clicks

# 录电脑里播放的声音 / 电脑声音 + 讲解旁白
python3 scripts/screen_rec.py record -d 30 --audio system
python3 scripts/screen_rec.py record -d 30 --audio mix

# 不定时长：Ctrl+C 停止
python3 scripts/screen_rec.py record
```

默认行为：

- 输出 `~/Desktop/Recordings/rec_YYYYMMDD_HHMMSS.mp4`（`--out` 可指定文件或目录）
- 1080p / 30fps / H.264 CRF 23；`--crf`、`--max-height`、`--fps` 可调，`--no-compress` 只 remux
- 同时写 `<视频>.meta.json`（录制起点、屏幕布局、区域/显示器）和 `<视频>.events.jsonl`（点击/快捷键/前台应用）
- `--keep-mov` 保留 screencapture 原始 .mov；`--no-events` 关闭事件采集
- `screencapture` 启动到首帧约需 1 秒，脚本提示「录制中」后稍等再操作

**录制计时悬浮窗**：录屏时默认在右上角显示「● REC 00:07 / 00:30」，让你随时看到录制状态和时长。它只在你的屏幕上显示，**默认不会进入录制的视频**（窗口 `sharingType = .none`，screencapture 捕获不到）。

- 需要让它入镜：`--overlay-visible`
- 不想要悬浮窗：`--no-overlay`
- 位置：`--overlay-position top-left|top-right|bottom-left|bottom-right`
- 总时长：默认取 `-d` 的值，可 `--overlay-total 90` 覆盖
- 悬浮窗显示在录制的那块屏上（跟随 `--display`）

### 声音模式

| 模式 | 参数 | 说明 |
|---|---|---|
| 纯录屏 | `--audio none` | 默认；不采集任何声音 |
| 电脑声音 | `--audio system` | 用 ScreenCaptureKit 采集系统音频，**不需要 BlackHole**；macOS 13+ |
| 讲解旁白 | `--audio mic` | 默认输入设备（麦克风） |
| 两者混音 | `--audio mix` | 电脑声音 + 麦克风混成一条；两边的权限都要有 |
| 指定设备 | `--audio <数字ID>` | 高级；外接声卡 / 虚拟声卡的 CoreAudio ID |

- `--audio` 不填且运行在交互终端时，会弹出 1–5 的选择菜单；**Agent 场景请在调用前用 AskUserQuestion 询问**，再把结果转成 `--audio` 参数。
- `system` / `mix` 需要屏幕录制权限（系统音频由 ScreenCaptureKit 采集）和 macOS 13+；`mic` / `mix` 需要麦克风权限。
- 系统声音采集与输出设备无关：AirPods、扬声器、甚至系统静音时都能录到系统音频。
- 用扬声器外放 + 麦克风同时录时，麦克风可能把外放声音再收一遍造成回声，建议戴耳机。
- `--keep-audio` 保留系统音频中间文件 `<视频>.system.m4a`，便于排查。
- 录 iPhone 真机屏幕不在本技能范围（用 QuickTime）。

## 截图

```bash
python3 scripts/screen_rec.py shot                          # 全屏
python3 scripts/screen_rec.py shot --region 0,0,800,600     # 区域
python3 scripts/screen_rec.py shot --delay 3                # 3 秒后
python3 scripts/screen_rec.py shot --window                 # 交互式选窗口（要人工点）
python3 scripts/screen_rec.py shot --clipboard              # 直接进剪贴板
python3 scripts/screen_rec.py shot --format jpg -o /tmp/a.jpg
```

## 切片 / GIF / 压缩

```bash
# 精确切片（重编码，起点准确）
python3 scripts/screen_rec.py clip demo.mp4 --start 00:00:12 --end 00:00:18

# 快速切片（流拷贝，起点对齐关键帧）
python3 scripts/screen_rec.py clip demo.mp4 --start 12 --duration 6 --fast

# GIF（调色板优化；可只截一段）
python3 scripts/screen_rec.py gif demo.mp4 --start 3 --end 8 --width 800 --fps 12

# 压缩（默认 1080p / CRF 24）
python3 scripts/screen_rec.py compress input.mov --crf 26 --max-height 720
```

时间格式：`12` / `12.5` / `00:12` / `01:02:03.5`。

## 智能字幕

### 模式 A：自动（离线，事件 + OCR）

录制时默认采集事件，直接生成模板化字幕：

```bash
python3 scripts/screen_rec.py subtitle auto demo.mp4            # 输出 demo.srt
python3 scripts/screen_rec.py subtitle auto demo.mp4 --burn     # 生成并烧录成 demo-sub.mp4
```

生成内容示例：`点击「保存」` / `按下 ⌘S` / `切换到 Chrome`。
事件缺失（未授权输入监控）时会明确报错并提示改用模式 B。

### 模式 B：AI 读图写字幕（质量优先，Agent 在环）

```bash
# 1) 抽关键帧 + 事件清单 + OCR 提示
python3 scripts/screen_rec.py subtitle draft demo.mp4 --ocr
#    → demo-subtitle/keyframes/*.png、manifest.json、subtitles.template.json

# 2) Agent 用 Read 工具逐张查看关键帧（manifest.points[i].frame 对应），
#    结合 points[i].kind / event / ocr 理解这一步在做什么

# 3) 写 demo-subtitle/subtitles.json：
#    [{"start": 1.2, "end": 3.4, "text": "打开设置，把超时改成 30 秒"}]

# 4) 烧录
python3 scripts/screen_rec.py subtitle burn demo.mp4 --sub demo-subtitle/subtitles.json
```

没有事件时 `draft` 自动改用画面变化检测抽帧（`--scene` 强制、`--interval` 调采样间隔、`--max` 限制帧数）。

**写字幕的约定**：写「用户视角的操作解释」，不要复述屏幕文字；每条 ≤ 20 字；单条时间窗 0.8~3 秒；一个关键动作一条，别把整段流程塞进一条。

### 烧录（两种模式共用）

```bash
python3 scripts/screen_rec.py subtitle burn demo.mp4 --sub demo.srt \
  --font-size 48 --position bottom --bg-opacity 0.65 --color '#FFFFFF'
```

参数：`--max-height`（默认 1080）、`--fps`（30）、`--crf`（20）、`--font`、`--font-size`、`--position bottom|top`、`--color`、`--bg-opacity`、`--no-bg`。
实现是「PIL 渲染透明字幕 PNG + ffmpeg overlay」，**不依赖 ffmpeg 的 libass/subtitles 滤镜**（Homebrew 精简版没有）。

## 输出约定

| 产物 | 路径 |
|---|---|
| 录屏 | `~/Desktop/Recordings/rec_<时间>.mp4`（+ `.meta.json`、`.events.jsonl`） |
| 截图 | `~/Desktop/Recordings/shot_<时间>.png` |
| 切片 | `<源文件名>-clip.mp4` |
| GIF | `<源文件名>.gif` |
| 压缩 | `<源文件名>-min.mp4` |
| 带字幕 | `<源文件名>-sub.mp4` |

## Agent 使用约定

1. 首次使用或换机先 `ensure`（可先 `ensure --check`，缺依赖再 `ensure`）；任何命令报「缺少依赖」都先跑 `ensure` 再重试。权限缺失时直接报告设置路径，**不要硬跑**。
2. 所有命令加 `--json`，从返回的 `video` / `subtitles` / `dir` 字段取下一步输入。
3. 需要「智能字幕」且要质量时，走模式 B：`draft --ocr` → Read 看帧 → 写 `subtitles.json` → `burn`。不要凭空编造没看到的内容。
4. 录屏建议始终给 `--duration`（Agent 无法可靠地按 Ctrl+C）；需要定长演示就 `-d`。
5. 完成后把产物绝对路径、时长、分辨率回报给用户。

## 已知限制与坑

- **首帧延迟**：screencapture 启动约 1 秒后才开始录，演示前先等提示再操作。
- **VFR**：系统录制是可变帧率，脚本统一转 30fps CFR，避免播放器/GIF 时间轴漂移。
- **事件对齐**：用「文件 mtime − 视频时长」反推录制起点，误差约 ±0.3 秒，字幕时间窗因此留了余量。
- **隐私**：事件采集只记录点击坐标、组合键键码、前台应用名，**不记录输入的字符内容**。
- **OCR 精度**：自动模式取点击位置最近的文本，复杂界面可能取到相邻文字；质量优先用模式 B。
- **性能**：烧字幕是每条字幕一个 overlay 输入，几十条很快；超过 ~100 条会明显变慢，可先 `clip` 分段或减少字幕条数。
- **`--clicks`** 只是让视频里显示点击涟漪，和事件采集是两回事；事件采集默认开启。
- **悬浮窗不入镜是系统级排除**：`sharingType = .none` 会让它同时不被其他截屏/录屏/共享工具捕获，这是预期行为；只有 `--overlay-visible` 会把它写进视频。
- **悬浮窗位置**跟随录制屏幕；录第二块屏时如果看不到，确认 `--display` 是否指对。
