# mac-screen-record

一个供 AI Agent 调用的 macOS 录屏技能，也可以直接从命令行使用。支持屏幕录制、截图、视频切片、GIF、压缩和操作字幕。

## 安装

克隆本仓库，将目录放入支持 Agent Skills 的客户端技能目录，例如：

```bash
git clone https://github.com/mysoloai/mac-screen-record.git
mkdir -p ~/.agents/skills
ln -s "$(pwd)/mac-screen-record" ~/.agents/skills/mac-screen-record
```

重新加载客户端技能后，可以用自然语言提出录屏或加字幕需求。也可以在仓库目录直接运行：

```bash
python3 scripts/screen_rec.py ensure --check
python3 scripts/screen_rec.py permissions
python3 scripts/screen_rec.py record -d 30
python3 scripts/screen_rec.py gif demo.mp4 --width 800 --fps 12
python3 scripts/screen_rec.py subtitle auto demo.mp4 --burn
```

首次使用可运行 `python3 scripts/screen_rec.py ensure`，它可能安装 ffmpeg、Pillow 或触发 Xcode 命令行工具安装；`ensure --check` 只检查。系统会按功能要求屏幕录制、输入监控或麦克风权限。具体参数、自动字幕和 Agent 辅助字幕流程参见 [SKILL.md](SKILL.md)。

## 数据与限制

录制默认保存在本机桌面 Recordings 目录。默认采集点击坐标、组合键键码与前台应用名，写入视频旁的事件文件；可使用 `--no-events` 关闭。屏幕视频和 OCR 文字可能包含敏感信息，分享前请自行检查。麦克风默认关闭。此工具仅适用于 macOS；事件字幕依赖系统权限，OCR 可能识别错误。

## License

[MIT](LICENSE)
