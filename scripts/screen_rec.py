#!/usr/bin/env python3
"""mac-screen-record: macOS 命令行录屏、截图、切片、GIF、压缩与智能字幕。

设计要点：
- 录制用系统自带 screencapture（无需安装，权限体系一致）
- 转码/切片/GIF 用 ffmpeg
- 字幕烧录不依赖 libass：PIL 渲染 PNG + ffmpeg overlay（Homebrew 的精简版 ffmpeg 没有 subtitles 滤镜）
- 「关键操作点」由 Swift 辅助程序采集（点击坐标 / 快捷键 / 前台应用），OCR 用 Apple Vision
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

__version__ = "1.3.1"

SKILL_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = Path.home() / ".cache" / "mac-screen-record"
BIN_DIR = CACHE_DIR / "bin"
# Pillow 允许装到独立目录（不污染系统 Python；PEP 668 环境也能用）
PYDEPS_DIR = CACHE_DIR / f"pydeps-py{sys.version_info.major}{sys.version_info.minor}"
if PYDEPS_DIR.exists() and str(PYDEPS_DIR) not in sys.path:
    sys.path.insert(0, str(PYDEPS_DIR))
DEFAULT_OUT_DIR = Path.home() / "Desktop" / "Recordings"

FONT_CANDIDATES = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
]

KEY_NAMES = {
    36: "Return", 48: "Tab", 49: "Space", 51: "Delete", 53: "Escape", 76: "Enter",
    115: "Home", 119: "End", 116: "PageUp", 121: "PageDown",
    123: "Left", 124: "Right", 125: "Down", 126: "Up",
}
KEY_LETTERS = {
    0: "A", 1: "S", 2: "D", 3: "F", 4: "H", 5: "G", 6: "Z", 7: "X", 8: "C", 9: "V",
    11: "B", 12: "Q", 13: "W", 14: "E", 15: "R", 16: "Y", 17: "T",
    31: "O", 32: "U", 34: "I", 35: "P", 37: "L", 38: "J", 40: "K",
    41: "N", 42: "M", 18: "1", 19: "2", 20: "3", 21: "4", 23: "5", 22: "6",
    26: "7", 28: "8", 25: "9", 29: "0",
}
MOD_SYMBOLS = {"cmd": "⌘", "shift": "⇧", "opt": "⌥", "ctrl": "⌃"}

VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".webm"}


class RecError(Exception):
    pass


# ---------------------------------------------------------------- 基础工具


def which(name: str) -> str | None:
    p = shutil.which(name)
    if p:
        return p
    for d in ("/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/usr/sbin", "/bin", "/sbin"):
        c = Path(d) / name
        if c.exists():
            return str(c)
    return None


def ffmpeg() -> str:
    p = os.environ.get("FFMPEG") or which("ffmpeg")
    if not p or not Path(p).exists():
        raise RecError("找不到 ffmpeg，请先运行 ensure 或 brew install ffmpeg")
    return p


def ffprobe() -> str:
    p = os.environ.get("FFPROBE") or which("ffprobe")
    if not p or not Path(p).exists():
        raise RecError("找不到 ffprobe（ffmpeg 自带），请先运行 ensure 或 brew install ffmpeg")
    return p


def screencapture() -> str:
    p = os.environ.get("SCREENCAPTURE") or which("screencapture") or "/usr/sbin/screencapture"
    if not Path(p).exists():
        raise RecError("找不到 screencapture（macOS 系统命令）")
    return p


def swiftc() -> str | None:
    p = which("swiftc")
    if p:
        return p
    x = which("xcrun")
    if x:
        try:
            r = subprocess.run([x, "--find", "swiftc"], capture_output=True, text=True, timeout=20)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            pass
    return None


def run(cmd: list, check: bool = True, timeout: int | None = None) -> subprocess.CompletedProcess:
    cmd = [str(c) for c in cmd]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise RecError(f"命令不存在: {cmd[0]}") from e
    except subprocess.TimeoutExpired as e:
        raise RecError(f"命令超时: {' '.join(cmd[:4])} …") from e
    if check and res.returncode != 0:
        tail = (res.stderr or res.stdout or "").strip()[-2000:]
        raise RecError(f"命令失败（exit {res.returncode}）: {' '.join(cmd)}\n{tail}")
    return res


def run_stream(cmd, timeout: int | None = None) -> None:
    """执行安装类命令：输出实时透传（走 stderr，避免污染 --json）。"""
    cmd = [str(c) for c in cmd]
    print(f"$ {' '.join(cmd)}", file=sys.stderr)
    try:
        p = subprocess.Popen(cmd)
    except FileNotFoundError as e:
        raise RecError(f"命令不存在: {cmd[0]}") from e
    try:
        rc = p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        raise RecError(f"命令超时: {cmd[0]}")
    if rc != 0:
        raise RecError(f"命令失败（exit {rc}）: {' '.join(cmd)}")


def preflight(*need) -> None:
    """命令开跑前的轻量依赖检查；缺失时给出统一的 ensure 提示。"""
    missing = []
    if "ffmpeg" in need:
        try:
            ffmpeg()
            ffprobe()
        except RecError:
            missing.append("ffmpeg/ffprobe")
    if "pillow" in need:
        try:
            from PIL import Image  # noqa: F401
        except Exception:
            missing.append("Pillow")
    if "swift" in need and not swiftc():
        missing.append("Xcode Command Line Tools（swiftc）")
    if missing:
        raise RecError(
            "缺少依赖：" + "、".join(missing) + "\n"
            "先运行环境准备：python3 scripts/screen_rec.py ensure"
        )


def ensure_helper(name: str) -> Path:
    """按需编译 scripts/native/<name>.swift 到缓存目录。"""
    src = SKILL_ROOT / "scripts" / "native" / f"{name}.swift"
    if not src.exists():
        raise RecError(f"缺少辅助程序源码: {src}")
    exe = BIN_DIR / name
    try:
        if exe.exists() and exe.stat().st_mtime >= src.stat().st_mtime:
            return exe
    except OSError:
        pass
    compiler = swiftc()
    if not compiler:
        raise RecError("缺少 Swift 编译器（swiftc），需要安装 Xcode Command Line Tools：xcode-select --install")
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    print(f"⏳ 首次编译辅助工具 {name} …", file=sys.stderr)
    res = subprocess.run(
        [compiler, "-O", "-swift-version", "5", str(src), "-o", str(exe)],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise RecError(f"编译 {name} 失败:\n{(res.stderr or res.stdout)[-2000:]}")
    return exe


def screen_info(request: bool = False) -> dict:
    exe = ensure_helper("screen_info")
    res = run([exe] + (["--request"] if request else []), timeout=30)
    return json.loads(res.stdout)


PERMISSION_PANES = {
    "screen_capture": (
        "屏幕录制", "录屏、截图",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    ),
    "listen_event": (
        "输入监控", "事件采集：智能字幕的关键操作点、快捷键",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
    ),
    "accessibility": (
        "辅助功能", "部分系统版本的事件采集兜底（一般不需要）",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    ),
    "microphone": (
        "麦克风", "仅 --audio mic 录人声时需要",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
    ),
}


def permission_status(layout: dict, key: str) -> tuple:
    """返回 (True/False/None, 文案)。None 表示未询问/未知。"""
    value = layout.get(key)
    if key == "microphone":
        return {
            "authorized": (True, "已授权"),
            "not_determined": (None, "未询问（首次使用会弹窗）"),
            "denied": (False, "已拒绝"),
            "restricted": (False, "受限（无法修改）"),
        }.get(value, (None, "未知"))
    if value is True:
        return True, "已授权"
    if value is False:
        return False, "未授权"
    return None, "未知"


def ocr_image(image: Path, rect: tuple | None = None) -> list:
    exe = ensure_helper("ocr")
    cmd = [exe, image]
    if rect:
        cmd += [int(round(v)) for v in rect]
    res = run(cmd, timeout=60)
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return []


def probe(path: Path) -> dict:
    res = run([
        ffprobe(), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ], timeout=60)
    data = json.loads(res.stdout or "{}")
    fmt = data.get("format") or {}
    duration = float(fmt.get("duration") or 0)
    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    width = int(v["width"]) if v and v.get("width") else 0
    height = int(v["height"]) if v and v.get("height") else 0
    fps = 0.0
    if v:
        rate = v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1"
        try:
            n, d = rate.split("/")
            fps = float(n) / float(d) if float(d) else 0.0
        except Exception:
            pass
    return {
        "duration": round(duration, 3),
        "width": width,
        "height": height,
        "fps": round(fps, 2),
        "has_audio": has_audio,
        "size": int(fmt.get("size") or Path(path).stat().st_size),
    }


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    for i in range(2, 1000):
        cand = p.with_name(f"{p.stem}-{i}{p.suffix}")
        if not cand.exists():
            return cand
    raise RecError(f"无法生成不冲突的文件名: {p}")


def parse_time(value: str | float) -> float:
    """支持 12 / 12.5 / 00:12 / 01:02:03.5"""
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return 0.0
    parts = s.split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        pass
    raise RecError(f"无法解析时间: {value}（示例：3 / 3.5 / 00:03 / 01:02:03.5）")


def srt_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms >= 1000:
        s += 1
        ms -= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_srt(text: str) -> list:
    cues = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip())
    for block in blocks:
        lines = [x for x in block.split("\n") if x.strip() != ""]
        if not lines:
            continue
        idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if idx is None:
            continue
        m = re.match(
            r"\s*(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)",
            lines[idx],
        )
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        cue_text = "\n".join(lines[idx + 1:]).strip()
        if cue_text:
            cues.append({"start": start, "end": end, "text": cue_text})
    return cues


def load_cues(path: Path) -> list:
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(raw)
        if isinstance(data, dict):
            data = data.get("cues") or data.get("subtitles") or []
        cues = []
        for c in data:
            cues.append({
                "start": float(c.get("start", 0)),
                "end": float(c.get("end", 0)),
                "text": str(c.get("text", "")).strip(),
            })
        return [c for c in cues if c["text"] and c["end"] > c["start"]]
    return parse_srt(raw)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n/1:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def resolve_out_dir(out: str | None) -> tuple[Path, str]:
    """返回 (输出目录, 文件名 stem)。--out 可以是文件路径或目录。"""
    if not out:
        return DEFAULT_OUT_DIR, f"rec_{now_stamp()}"
    p = Path(out).expanduser().resolve()
    if p.is_dir() or str(out).endswith("/"):
        return p, f"rec_{now_stamp()}"
    return p.parent, p.stem


def print_result(data: dict, as_json: bool, lines: list):
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print("\n".join(str(x) for x in lines))


# ---------------------------------------------------------------- 事件采集


def start_event_tap(events_path: Path):
    """启动事件采集，返回 (Popen, 状态说明)。失败返回 (None, 原因)，不阻塞录制。"""
    try:
        exe = ensure_helper("event_tap")
    except RecError as e:
        return None, str(e)
    try:
        events_path.touch(exist_ok=True)
        p = subprocess.Popen(
            [str(exe), str(events_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError as e:
        return None, f"事件采集启动失败: {e}"

    line = ""
    deadline = time.time() + 4
    while time.time() < deadline:
        try:
            r, _, _ = select.select([p.stderr], [], [], 0.2)
        except (OSError, ValueError):
            break
        if r:
            line = (p.stderr.readline() or "").strip()
            break
        if p.poll() is not None:
            break
    if line == "EVENTTAP_READY":
        return p, "ok"
    if p.poll() is not None:
        return None, ("事件采集无权限（系统设置 → 隐私与安全性 → 输入监控/辅助功能，勾选终端或宿主 App，勾完需重启该 App；"
                      "或运行 python3 scripts/screen_rec.py permissions --open）")
    return p, "ok"  # 没读到就绪行但进程还活着，按可用处理


def stop_process(p) -> None:
    if not p:
        return
    try:
        p.terminate()
        p.wait(timeout=3)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def stop_event_tap(p) -> None:
    stop_process(p)


def read_events(path: Path, video_start: float, duration: float) -> list:
    events = []
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = e.get("t")
        if not isinstance(t, (int, float)):
            continue
        rel = float(t) - video_start
        if rel < -0.5 or rel > duration + 1.5:
            continue
        e["rel"] = round(max(0.0, rel), 3)
        events.append(e)
    events.sort(key=lambda x: x["rel"])
    return events


def load_meta(video: Path) -> dict:
    meta_path = Path(str(video) + ".meta.json")
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    info = probe(video)
    st = video.stat()
    return {
        "video": str(video),
        "duration": info["duration"],
        "width": info["width"],
        "height": info["height"],
        "video_start_epoch": st.st_mtime - info["duration"],
        "recording": {"mode": "full", "display": 1},
        "screen_layout": None,
        "events_file": None,
    }


def mapping_for(meta: dict) -> dict | None:
    """把全局点位坐标（points）映射到视频像素坐标。"""
    rec = meta.get("recording") or {}
    layout = meta.get("screen_layout")
    if not layout:
        try:
            layout = screen_info()
        except RecError:
            return None
    displays = layout.get("displays") or []
    if not displays:
        return None
    vw, vh = int(meta.get("width") or 0), int(meta.get("height") or 0)
    if not vw or not vh:
        return None

    if rec.get("mode") == "region":
        region = rec.get("region") or []
        if len(region) != 4:
            return None
        rx, ry, rw, rh = [float(x) for x in region]
        if rw <= 0 or rh <= 0:
            return None
        return {"origin": (rx, ry), "scale": (vw / rw, vh / rh), "bounds": (rx, ry, rw, rh)}

    target = int(rec.get("display") or 1)
    d = next((x for x in displays if int(x.get("index", 0)) == target), displays[0])
    w, h = float(d.get("w") or 0), float(d.get("h") or 0)
    if w <= 0 or h <= 0:
        return None
    return {
        "origin": (float(d["x"]), float(d["y"])),
        "scale": (vw / w, vh / h),
        "bounds": (float(d["x"]), float(d["y"]), w, h),
    }


def point_to_pixel(x: float, y: float, meta: dict):
    m = mapping_for(meta)
    if not m:
        return None
    ox, oy, bw, bh = m["bounds"]
    if not (ox <= x < ox + bw and oy <= y < oy + bh):
        return None
    px = (x - m["origin"][0]) * m["scale"][0]
    py = (y - m["origin"][1]) * m["scale"][1]
    return px, py


# ---------------------------------------------------------------- 录制


def cmd_record(args) -> int:
    preflight("ffmpeg")
    sc = screencapture()
    duration = float(args.duration) if args.duration else 0.0
    outdir, stem = resolve_out_dir(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    layout = None
    try:
        layout = screen_info()
    except RecError as e:
        print(f"⚠️  无法检查屏幕录制权限（{e}），继续录制", file=sys.stderr)
    if layout and not layout.get("screen_capture", True) and not args.force:
        raise RecError(
            "没有屏幕录制权限，录出来只会是桌面壁纸。\n"
            "请到：系统设置 → 隐私与安全性 → 屏幕录制（macOS 15+ 叫「屏幕与系统音频录制」）\n"
            "勾选运行本命令的终端/宿主 App，然后完全退出并重开它。\n"
            "打开面板：python3 scripts/screen_rec.py permissions --open\n"
            "（确认已授权但仍误判时加 --force）"
        )
    if args.audio == "mic" and layout:
        mic = layout.get("microphone")
        if mic in ("denied", "restricted"):
            raise RecError(
                "没有麦克风权限，无法录制人声。\n"
                "系统设置 → 隐私与安全性 → 麦克风，勾选终端/宿主 App 后完全重启它。\n"
                "打开面板：python3 scripts/screen_rec.py permissions --open"
            )
        if mic == "not_determined":
            print("🎤 首次使用麦克风，系统会弹出授权提示（请点允许）", file=sys.stderr)

    if args.region:
        parts = [float(x) for x in args.region.split(",")]
        if len(parts) != 4:
            raise RecError("--region 格式应为 x,y,w,h，例如 0,0,1200,800")
        region = parts
        mode = "region"
    else:
        region = None
        mode = "display" if args.display else "full"

    raw = outdir / f"{stem}.raw.mov"
    events_path = outdir / f"{stem}.events.jsonl"
    meta_events = None
    tap = None
    overlay = None
    overlay_bin = None

    if not args.no_overlay:
        try:
            overlay_bin = ensure_helper("rec_overlay")
        except RecError as e:
            print(f"⚠️  录制悬浮窗不可用：{e}", file=sys.stderr)

    if not args.no_events:
        tap, note = start_event_tap(events_path)
        if tap is None:
            print(f"⚠️  {note}（继续录制，字幕将退化为关键帧/AI 模式）", file=sys.stderr)
            events_path = None
        else:
            meta_events = events_path

    if args.countdown and args.countdown > 0:
        print(f"⏳ {args.countdown} 秒后开始录制…（可切到目标窗口）")
        time.sleep(args.countdown)

    cmd = [sc, "-x", "-v"]
    if duration > 0:
        cmd += ["-V", str(int(math.ceil(duration)))]
    if args.display:
        cmd += ["-D", str(args.display)]
    if region:
        cmd += ["-R", ",".join(str(x) for x in region)]
    if args.cursor:
        cmd.append("-C")
    if args.clicks:
        cmd.append("-k")
    if args.audio == "mic":
        cmd.append("-g")
    elif args.audio and args.audio != "none":
        if not str(args.audio).isdigit():
            raise RecError("--audio 只接受 none / mic / 数字设备 ID")
        cmd += ["-G", str(args.audio)]
    cmd.append(str(raw))

    if duration > 0:
        print(f"⏺  录制中…（{int(math.ceil(duration))} 秒后自动停止）")
    else:
        print("⏺  录制中… 按 Ctrl+C 结束（screencapture 启动约需 1 秒，请稍等再操作）")
    print(f"   输出：{outdir / (stem + '.mp4')}")

    if overlay_bin:
        try:
            ov_cmd = [
                str(overlay_bin), "--position", args.overlay_position,
                "--delay", str(args.overlay_delay), "--screen", str(args.display or 1),
            ]
            total_ov = args.overlay_total if args.overlay_total else (
                int(math.ceil(duration)) if duration > 0 else None
            )
            if total_ov:
                ov_cmd += ["--total", str(total_ov)]
            if args.overlay_visible:
                ov_cmd.append("--visible")
            if duration > 0:
                ov_cmd += ["--duration", str(int(math.ceil(duration)) + 3)]
            overlay = subprocess.Popen(ov_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            print(f"⚠️  录制悬浮窗启动失败：{e}", file=sys.stderr)

    try:
        proc = subprocess.Popen(cmd)
        try:
            rc = proc.wait()
        except KeyboardInterrupt:
            try:
                proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
            rc = proc.wait()
    finally:
        stop_process(overlay)
        stop_event_tap(tap)

    if rc != 0 and rc != 1:
        raise RecError(f"screencapture 退出码 {rc}（录制可能被中断）")
    if not raw.exists() or raw.stat().st_size == 0:
        raise RecError("录制文件为空：检查屏幕录制权限，或目标区域是否有效")

    info = probe(raw)
    duration_actual = info["duration"]
    if duration_actual <= 0:
        raise RecError("录制文件没有有效时长")
    # screencapture 结束时才落盘，用 mtime - 时长 反推首帧墙钟时间
    video_start = raw.stat().st_mtime - duration_actual

    events = []
    if meta_events:
        events = read_events(meta_events, video_start, duration_actual)

    # 转 mp4（默认压缩）
    out = unique_path(outdir / f"{stem}.mp4")
    if args.no_compress:
        cmd2 = [ffmpeg(), "-y", "-i", str(raw), "-c:v", "copy"]
        if info["has_audio"]:
            cmd2 += ["-c:a", "aac", "-b:a", "192k"]
        cmd2 += ["-movflags", "+faststart", str(out)]
    else:
        vf = []
        if args.fps and args.fps > 0:
            vf.append(f"fps={args.fps}")
        if args.max_height and args.max_height > 0 and info["height"] > args.max_height:
            vf.append(f"scale=-2:{args.max_height}")
        cmd2 = [ffmpeg(), "-y", "-i", str(raw)]
        if vf:
            cmd2 += ["-vf", ",".join(vf)]
        cmd2 += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(args.crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        ]
        if info["has_audio"]:
            cmd2 += ["-c:a", "aac", "-b:a", "160k"]
        cmd2.append(str(out))
    print("🎞  正在转码 mp4 …")
    run(cmd2, timeout=3600)

    final = probe(out)

    gif_path = None
    if args.gif:
        gif_path = make_gif(out, args.gif_width, args.gif_fps, args.gif_colors, None)

    if not args.keep_mov:
        try:
            raw.unlink()
        except OSError:
            pass

    meta = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "video": str(out),
        "duration": final["duration"],
        "width": final["width"],
        "height": final["height"],
        "fps": final["fps"],
        "has_audio": final["has_audio"],
        "video_start_epoch": round(video_start, 3),
        "recording": {"mode": mode, "display": args.display or 1, "region": region},
        "screen_layout": layout,
        "events_file": str(meta_events) if meta_events else None,
        "events_count": len(events),
        "overlay": None if args.no_overlay else {
            "visible_in_video": bool(args.overlay_visible),
            "position": args.overlay_position,
        },
    }
    meta_path = Path(str(out) + ".meta.json")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    data = {
        "ok": True,
        "video": str(out),
        "meta": str(meta_path),
        "duration": final["duration"],
        "size": final["size"],
        "size_human": human_size(final["size"]),
        "gif": str(gif_path) if gif_path else None,
        "events": str(meta_events) if meta_events else None,
        "events_count": len(events),
        "resolution": f'{final["width"]}x{final["height"]}',
    }
    lines = [
        f"✅ 录制完成：{out}",
        f"   时长 {final['duration']:.2f}s · {final['width']}x{final['height']} · {human_size(final['size'])}",
    ]
    if gif_path:
        lines.append(f"   GIF：{gif_path}")
    if meta_events:
        lines.append(f"   事件：{meta_events}（{len(events)} 条）")
    lines.append(f"   元数据：{meta_path}")
    print_result(data, args.json, lines)
    return 0


# ---------------------------------------------------------------- 截图


def cmd_shot(args) -> int:
    sc = screencapture()
    try:
        layout = screen_info()
    except RecError:
        layout = None
    if layout and not layout.get("screen_capture", True) and not args.force:
        raise RecError(
            "没有屏幕录制权限，截图只会得到桌面壁纸。\n"
            "系统设置 → 隐私与安全性 → 屏幕录制，勾选终端/宿主 App 后完全重启它。\n"
            "打开面板：python3 scripts/screen_rec.py permissions --open\n"
            "（确认已授权但仍误判时加 --force）"
        )
    if args.out:
        p = Path(args.out).expanduser().resolve()
        out = p if not (p.is_dir() or str(args.out).endswith("/")) else unique_path(p / f"shot_{now_stamp()}.png")
    else:
        DEFAULT_OUT_DIR.mkdir(parents=True, exist_ok=True)
        out = unique_path(DEFAULT_OUT_DIR / f"shot_{now_stamp()}.png")

    cmd = [sc, "-x"]
    if args.region:
        cmd += ["-R", args.region]
    if args.display:
        cmd += ["-D", str(args.display)]
    if args.delay:
        cmd += ["-T", str(args.delay)]
    if args.cursor:
        cmd.append("-C")
    if args.window:
        cmd.append("-W")
    if args.clipboard:
        cmd.append("-c")
        run(cmd)
        data = {"ok": True, "clipboard": True}
        print_result(data, args.json, ["✅ 已复制到剪贴板"])
        return 0

    fmt = (args.format or out.suffix.lstrip(".") or "png").lower()
    if fmt != "png":
        cmd += ["-t", fmt]
        if out.suffix.lower() != f".{fmt}":
            out = out.with_suffix(f".{fmt}")
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd.append(str(out))
    run(cmd)
    if not out.exists():
        raise RecError("截图失败：文件未生成")
    size = out.stat().st_size
    data = {"ok": True, "image": str(out), "size": size, "size_human": human_size(size)}
    print_result(data, args.json, [f"✅ 截图已保存：{out}（{human_size(size)}）"])
    return 0


# ---------------------------------------------------------------- 切片 / GIF / 压缩


def cmd_clip(args) -> int:
    preflight("ffmpeg")
    src = Path(args.input).expanduser().resolve()
    if not src.exists():
        raise RecError(f"输入文件不存在: {src}")
    info = probe(src)
    total = info["duration"]
    start = parse_time(args.start) if args.start else 0.0
    if args.end:
        end = parse_time(args.end)
    elif args.duration:
        end = start + parse_time(args.duration)
    else:
        end = total
    start = max(0.0, min(start, total))
    end = max(0.0, min(end, total))
    if end - start <= 0.05:
        raise RecError(f"切片区间无效：start={start:.2f}s end={end:.2f}s（源时长 {total:.2f}s）")

    out = Path(args.out).expanduser() if args.out else src.with_name(f"{src.stem}-clip{src.suffix}")
    out = unique_path(out)
    dur = end - start

    if args.fast:
        cmd = [ffmpeg(), "-y", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{dur:.3f}",
               "-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(out)]
    else:
        cmd = [ffmpeg(), "-y", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{dur:.3f}",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        if info["has_audio"]:
            cmd += ["-c:a", "aac", "-b:a", "192k"]
        cmd.append(str(out))
    run(cmd, timeout=3600)
    final = probe(out)
    data = {"ok": True, "video": str(out), "start": round(start, 3), "end": round(end, 3),
            "duration": final["duration"], "size": final["size"]}
    lines = [f"✅ 切片完成：{out}", f"   {start:.2f}s → {end:.2f}s（{final['duration']:.2f}s，{human_size(final['size'])}）"]
    if args.fast:
        lines.append("   注意：--fast 为流拷贝，起点会对齐到最近关键帧，可能比指定时间略早")
    print_result(data, args.json, lines)
    return 0


def make_gif(video: Path, width: int, fps: int, colors: int, out: Path | None,
             start: float | None = None, end: float | None = None) -> Path:
    info = probe(video)
    w = min(int(width), info["width"]) if info["width"] else int(width)
    out = out or video.with_suffix(".gif")
    out = unique_path(out)
    vf = (
        f"fps={int(fps)},scale={w}:-1:flags=lanczos,"
        f"split[s0][s1];[s0]palettegen=max_colors={int(colors)}[p];[s1][p]paletteuse=dither=bayer"
    )
    cmd = [ffmpeg(), "-y"]
    if start is not None:
        cmd += ["-ss", f"{float(start):.3f}"]
    cmd += ["-i", str(video)]
    if start is not None and end is not None:
        cmd += ["-t", f"{max(0.05, float(end) - float(start)):.3f}"]
    cmd += ["-vf", vf, "-loop", "0", str(out)]
    run(cmd, timeout=3600)
    return out


def cmd_gif(args) -> int:
    preflight("ffmpeg")
    src = Path(args.input).expanduser().resolve()
    if not src.exists():
        raise RecError(f"输入文件不存在: {src}")
    info = probe(src)
    start = parse_time(args.start) if args.start else 0.0
    end = parse_time(args.end) if args.end else info["duration"]
    if args.duration:
        end = start + parse_time(args.duration)
    if end <= start:
        raise RecError("GIF 区间无效")
    out = Path(args.out).expanduser() if args.out else src.with_name(f"{src.stem}.gif")
    print("🎞  正在生成 GIF …")
    gif = make_gif(src, args.width, args.fps, args.colors, out, start, end)
    size = gif.stat().st_size
    data = {"ok": True, "gif": str(gif), "size": size, "size_human": human_size(size),
            "width": args.width, "fps": args.fps}
    print_result(data, args.json, [f"✅ GIF 已生成：{gif}（{human_size(size)}）"])
    return 0


def cmd_compress(args) -> int:
    preflight("ffmpeg")
    src = Path(args.input).expanduser().resolve()
    if not src.exists():
        raise RecError(f"输入文件不存在: {src}")
    info = probe(src)
    vf = []
    if args.fps and args.fps > 0:
        vf.append(f"fps={args.fps}")
    if args.max_height and args.max_height > 0 and info["height"] > args.max_height:
        vf.append(f"scale=-2:{args.max_height}")
    out = Path(args.out).expanduser() if args.out else src.with_name(f"{src.stem}-min.mp4")
    out = unique_path(out)
    cmd = [ffmpeg(), "-y", "-i", str(src)]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += ["-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    if info["has_audio"]:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd.append(str(out))
    print("🎞  正在压缩 …")
    run(cmd, timeout=3600)
    final = probe(out)
    data = {"ok": True, "video": str(out), "source": str(src),
            "size": final["size"], "size_human": human_size(final["size"]),
            "source_size": info["size"], "duration": final["duration"]}
    print_result(data, args.json, [
        f"✅ 压缩完成：{out}",
        f"   {human_size(info['size'])} → {human_size(final['size'])} · {final['width']}x{final['height']}",
    ])
    return 0


# ---------------------------------------------------------------- 字幕


def extract_frame(video: Path, t: float, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    run([
        ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{max(0.0, t):.3f}", "-i", str(video),
        "-frames:v", "1", "-q:v", "2", str(out),
    ], timeout=120)
    return out


def scene_times(video: Path, threshold: float = 0.35) -> list:
    res = run([
        ffmpeg(), "-hide_banner", "-nostats", "-i", str(video),
        "-filter:v", f"select='gt(scene,{threshold})',showinfo",
        "-an", "-f", "null", "-",
    ], check=False, timeout=3600)
    times = [float(x) for x in re.findall(r"pts_time:([0-9.]+)", res.stderr or "")]
    return times


def key_events(events: list) -> list:
    """把事件流压缩成「关键操作点」：点击、快捷键组合、应用切换。"""
    points = []
    last_click = -99.0
    for e in events:
        typ = e.get("type")
        t = float(e.get("rel") or 0)
        if typ == "click":
            if t - last_click < 0.45:
                continue
            last_click = t
            points.append({"t": t, "kind": "click", "event": e})
        elif typ == "key":
            mods = e.get("mods") or []
            name = e.get("key") or ""
            if not name:
                continue
            if not (("cmd" in mods) or ("ctrl" in mods) or name in {"Return", "Escape", "Tab"}):
                continue
            points.append({"t": t, "kind": "key", "event": e})
        elif typ == "app":
            if points and points[-1]["kind"] == "app" and points[-1]["event"].get("app") == e.get("app"):
                continue
            points.append({"t": t, "kind": "app", "event": e})
    # 合并 0.8s 内的连续点，保留信息量更高的事件（click > key > app）
    merged = []
    for p in points:
        # 只合并同类型的密集事件（双击/连按），不同类事件保留（如点击后立刻切应用）
        if merged and p["kind"] == merged[-1]["kind"] and p["t"] - merged[-1]["t"] < 0.6:
            continue
        merged.append(p)
    return merged


def event_text(p: dict, ocr_near: str | None) -> str:
    e = p["event"]
    if p["kind"] == "click":
        if ocr_near:
            return f"点击「{ocr_near}」"
        app = e.get("app") or ""
        return f"点击（{app}）" if app else "点击"
    if p["kind"] == "key":
        mods = "".join(MOD_SYMBOLS.get(m, "") for m in (e.get("mods") or []))
        return f"按下 {mods}{e.get('key', '')}"
    app = e.get("app") or ""
    return f"切换到 {app}" if app else "切换应用"


def pick_text_near(boxes: list, px: float, py: float, width: float) -> str | None:
    best = None
    max_dist = max(140.0, width * 0.11)
    for b in boxes:
        txt = re.sub(r"\s+", " ", str(b.get("text") or "")).strip()
        if not txt or len(txt) > 40:
            continue
        if float(b.get("confidence") or 0) < 0.25:
            continue
        cx = float(b.get("x") or 0) + float(b.get("w") or 0) / 2
        cy = float(b.get("y") or 0) + float(b.get("h") or 0) / 2
        dist = math.hypot(cx - px, cy - py)
        if dist <= max_dist and (best is None or dist < best[0]):
            best = (dist, txt)
    return best[1] if best else None


def click_crop(px: float, py: float, w: int, h: int) -> tuple:
    cw = min(w, max(300, int(w * 0.24)))
    ch = min(h, max(150, int(h * 0.10)))
    x0 = max(0, min(int(px - cw / 2), w - cw))
    y0 = max(0, min(int(py - ch / 2), h - ch))
    return x0, y0, cw, ch


def build_auto_cues(video: Path, meta: dict, events: list, use_ocr: bool) -> list:
    info = probe(video)
    W, H = info["width"], info["height"]
    points = key_events(events)
    raw = []
    tmpdir = Path(tempfile.mkdtemp(prefix="msr-cues-"))
    try:
        for i, p in enumerate(points):
            t = p["t"]
            near = None
            if use_ocr and p["kind"] == "click":
                e = p["event"]
                pix = point_to_pixel(float(e.get("x", 0)), float(e.get("y", 0)), meta)
                if pix:
                    frame = extract_frame(video, t, tmpdir / f"f{i:03d}.png")
                    boxes = ocr_image(frame, click_crop(pix[0], pix[1], W, H))
                    near = pick_text_near(boxes, pix[0], pix[1], W)
            raw.append({"t": t, "text": event_text(p, near), "kind": p["kind"]})
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    cues = []
    for c in raw:
        if cues and c["text"] == cues[-1]["text"] and c["t"] - cues[-1]["t"] < 4:
            continue
        gap = 0.35 if c.get("kind") == "app" else 0.8
        if cues and round(c["t"] - cues[-1]["t"], 3) < gap:
            continue
        cues.append(dict(c))

    out = []
    for i, c in enumerate(cues):
        start = max(0.0, c["t"] - 0.25)
        end = min(info["duration"], c["t"] + 2.2)
        if i + 1 < len(cues):
            next_start = max(0.0, cues[i + 1]["t"] - 0.25)
            end = min(end, next_start - 0.1)
        if end - start < 0.6:
            end = min(info["duration"], start + 0.6)
        if end - start >= 0.4:
            out.append({"start": round(start, 3), "end": round(end, 3), "text": c["text"]})
    return out


def write_srt(cues: list, path: Path):
    lines = []
    for i, c in enumerate(cues, 1):
        lines.append(str(i))
        lines.append(f"{srt_time(c['start'])} --> {srt_time(c['end'])}")
        lines.append(c["text"])
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


SYMBOL_CHARS = set("⌘⇧⌥⌃⇥⌫")
SYMBOL_FONT_CANDIDATES = [
    "/System/Library/Fonts/Apple Symbols.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/SFNS.ttf",
]


def load_fonts(size: int, font_path: str | None = None):
    """主字体（中文）+ 符号字体（⌘⇧⌥⌃⇥⌫），符号字体用于补齐主字体缺字。"""
    from PIL import ImageFont
    primary_path = font_path
    if not primary_path:
        primary_path = next((p for p in FONT_CANDIDATES if Path(p).exists()), None)
    if not primary_path:
        for p in sorted(Path("/System/Library/Fonts").glob("*.tt*")):
            try:
                ImageFont.truetype(str(p), size, index=0)
                primary_path = str(p)
                break
            except Exception:
                continue
    if not primary_path:
        raise RecError("找不到可用字体，可用 --font 指定一个 ttf/ttc")
    primary = ImageFont.truetype(primary_path, size, index=0)
    symbol = primary
    for p in SYMBOL_FONT_CANDIDATES:
        if Path(p).exists():
            try:
                symbol = ImageFont.truetype(p, size, index=0)
                break
            except Exception:
                continue
    return primary, symbol


def _font_for(ch: str, primary, symbol):
    return symbol if ch in SYMBOL_CHARS else primary


def measure_text(text: str, primary, symbol) -> float:
    return sum(_font_for(ch, primary, symbol).getlength(ch) for ch in text)


def wrap_text(text: str, primary, symbol, max_w: float) -> list:
    lines = []
    for para in text.split("\n"):
        if not para.strip():
            continue
        cur = ""
        for ch in para:
            if measure_text(cur + ch, primary, symbol) <= max_w:
                cur += ch
            else:
                if cur:
                    lines.append(cur)
                cur = ch
        if cur:
            lines.append(cur)
    return lines[:4]


def draw_mixed(draw, text: str, center_x: float, baseline_y: float, primary, symbol, fill):
    width = measure_text(text, primary, symbol)
    x = center_x - width / 2
    for ch in text:
        f = _font_for(ch, primary, symbol)
        draw.text((x, baseline_y), ch, font=f, fill=fill, anchor="ls")
        x += f.getlength(ch)


def hex_color(value: str, default=(255, 255, 255)) -> tuple:
    v = (value or "").strip().lstrip("#")
    if len(v) == 6:
        try:
            return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            pass
    return default


def render_cue_png(text: str, W: int, H: int, font_path: str | None, font_size: int,
                   position: str, bg_opacity: float, fg: tuple, out: Path):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    primary, symbol = load_fonts(font_size, font_path)
    max_text_w = W * 0.86
    lines = wrap_text(text, primary, symbol, max_text_w) or [text]
    line_h = int(font_size * 1.32)
    block_h = line_h * len(lines)
    pad_x = int(font_size * 0.75)
    pad_y = int(font_size * 0.5)
    text_w = max(measure_text(l, primary, symbol) for l in lines)
    box_w = int(text_w + pad_x * 2)
    box_h = block_h + pad_y * 2
    x0 = (W - box_w) // 2
    margin = int(H * 0.055)
    y0 = H - box_h - margin if position == "bottom" else margin
    if bg_opacity > 0:
        radius = int(font_size * 0.38)
        draw.rounded_rectangle(
            [x0, y0, x0 + box_w, y0 + box_h],
            radius=radius,
            fill=(0, 0, 0, int(255 * max(0.0, min(1.0, bg_opacity)))),
        )
    for i, line in enumerate(lines):
        baseline = y0 + pad_y + i * line_h + int(font_size * 0.95)
        draw_mixed(draw, line, W / 2, baseline, primary, symbol, (fg[0], fg[1], fg[2], 255))
    img.save(out)


def burn_subtitles(video: Path, cues: list, out: Path, args) -> dict:
    info = probe(video)
    src_w, src_h = info["width"], info["height"]
    if not src_w or not src_h:
        raise RecError("无法读取视频分辨率")
    target_h = min(src_h, args.max_height) if args.max_height and args.max_height > 0 else src_h
    target_w = max(2, int(round(src_w * target_h / src_h / 2)) * 2)
    if target_w % 2:
        target_w -= 1

    font_size = args.font_size or max(28, int(target_h * 0.042))
    fg = hex_color(args.color)
    tmpdir = Path(tempfile.mkdtemp(prefix="msr-subs-"))
    pngs = []
    try:
        for i, cue in enumerate(cues):
            png = tmpdir / f"cue_{i:04d}.png"
            render_cue_png(
                cue["text"], target_w, target_h, args.font, font_size,
                args.position, 0.0 if args.no_bg else args.bg_opacity, fg, png,
            )
            pngs.append(png)

        inputs = [str(video)]
        parts = [f"[0:v]scale={target_w}:{target_h},fps={args.fps}[v0]"]
        prev = "v0"
        for i, (cue, png) in enumerate(zip(cues, pngs), 1):
            inputs += ["-loop", "1", "-i", str(png)]
            parts.append(
                f"[{prev}][{i}:v]overlay=0:0:enable='between(t,{cue['start']:.3f},{cue['end']:.3f})'[v{i}]"
            )
            prev = f"v{i}"

        cmd = [ffmpeg(), "-y", "-i", str(video)] + inputs[1:]
        cmd += ["-filter_complex", ";".join(parts), "-map", f"[{prev}]", "-t", f"{info['duration']:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", str(args.crf),
                "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        if info["has_audio"]:
            cmd += ["-map", "0:a?", "-c:a", "copy"]
        cmd.append(str(out))
        print(f"🎞  正在烧录 {len(cues)} 条字幕 …")
        run(cmd, timeout=3600)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return probe(out)


def cmd_subtitle_draft(args) -> int:
    preflight("ffmpeg")
    video = Path(args.video).expanduser().resolve()
    if not video.exists():
        raise RecError(f"视频不存在: {video}")
    meta = load_meta(video)
    info = probe(video)
    duration = info["duration"]

    events = []
    if args.events:
        ep = Path(args.events).expanduser().resolve()
        events = read_events(ep, float(meta.get("video_start_epoch", 0)), duration)
    elif meta.get("events_file"):
        ep = Path(meta["events_file"]).expanduser()
        if not ep.is_absolute():
            ep = video.parent / ep
        if ep.exists():
            events = read_events(ep, float(meta.get("video_start_epoch", 0)), duration)

    points = key_events(events) if events else []
    if args.scene or not points:
        print("🔍 未找到点击事件，改用画面变化检测抽帧 …")
        times = scene_times(video)
        if not times:
            step = max(1.0, float(args.interval))
            times = [i * step for i in range(1, int(duration / step) + 1)]
        points = [{"t": t, "kind": "scene", "event": {}} for t in times]

    # 去重 + 截断
    uniq = []
    for p in points:
        if uniq and p["t"] - uniq[-1]["t"] < 0.8:
            continue
        uniq.append(p)
    if args.max and len(uniq) > args.max:
        step = len(uniq) / args.max
        uniq = [uniq[int(i * step)] for i in range(args.max)]

    outdir = Path(args.out).expanduser().resolve() if args.out else video.with_name(f"{video.stem}-subtitle")
    kf_dir = outdir / "keyframes"
    outdir.mkdir(parents=True, exist_ok=True)
    kf_dir.mkdir(parents=True, exist_ok=True)

    manifest_points = []
    for i, p in enumerate(uniq, 1):
        t = max(0.0, min(float(p["t"]), max(0.0, duration - 0.05)))
        frame = extract_frame(video, t, kf_dir / f"kf_{i:03d}_{t:07.2f}s.png")
        ocr_boxes = []
        if args.ocr:
            try:
                ocr_boxes = ocr_image(frame)
            except RecError:
                ocr_boxes = []
        manifest_points.append({
            "index": i,
            "t": round(t, 3),
            "frame": str(frame),
            "kind": p["kind"],
            "event": p.get("event") or {},
            "ocr": [
                {"text": b.get("text"), "confidence": round(float(b.get("confidence") or 0), 3)}
                for b in sorted(ocr_boxes, key=lambda x: -float(x.get("confidence") or 0))[:12]
            ],
        })

    default_end = min(duration, 3.0)
    manifest = {
        "video": str(video),
        "duration": duration,
        "points": manifest_points,
        "instructions": [
            "用 Read 工具逐张查看 keyframes 下的图片（与 points[i].frame 对应）",
            "结合 points[i].kind / event / ocr 理解每个关键操作点",
            "写出 " + str(outdir / "subtitles.json") + '：[{"start": 秒, "end": 秒, "text": "解释性字幕"}]',
            f"再运行：python3 scripts/screen_rec.py subtitle burn {video} --sub {outdir / 'subtitles.json'}",
        ],
    }
    (outdir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    template = [{"start": round(max(0.0, p["t"] - 0.25), 3),
                 "end": round(min(duration, p["t"] + 2.2), 3), "text": ""} for p in manifest_points]
    (outdir / "subtitles.template.json").write_text(
        json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    data = {"ok": True, "dir": str(outdir), "manifest": str(outdir / "manifest.json"),
            "keyframes": [p["frame"] for p in manifest_points], "points": len(manifest_points)}
    lines = [
        f"✅ 已抽取 {len(manifest_points)} 个关键操作点 → {kf_dir}",
        f"   清单：{outdir / 'manifest.json'}",
        f"   模板：{outdir / 'subtitles.template.json'}",
        "   下一步：用 Read 查看关键帧图片 → 填写 subtitles.json → subtitle burn",
        f"   示例：python3 scripts/screen_rec.py subtitle burn {video} --sub {outdir / 'subtitles.json'}",
    ]
    print_result(data, args.json, lines)
    return 0


def cmd_subtitle_auto(args) -> int:
    preflight("ffmpeg")
    video = Path(args.video).expanduser().resolve()
    if not video.exists():
        raise RecError(f"视频不存在: {video}")
    meta = load_meta(video)
    info = probe(video)

    events_path = None
    if args.events:
        events_path = Path(args.events).expanduser().resolve()
    elif meta.get("events_file"):
        events_path = Path(meta["events_file"]).expanduser()
        if not events_path.is_absolute():
            events_path = video.parent / events_path

    if not events_path or not events_path.exists():
        raise RecError(
            "自动字幕需要事件数据（录制时默认开启的 .events.jsonl）。\n"
            "没有事件时请改用 AI 模式：subtitle draft → 读关键帧写字幕 → subtitle burn"
        )

    events = read_events(events_path, float(meta.get("video_start_epoch", 0)), info["duration"])
    if not events:
        raise RecError(
            "事件文件为空（可能录制时未授权输入监控）。\n"
            "检查权限：python3 scripts/screen_rec.py permissions\n"
            "或改用 subtitle draft + AI 写字幕。"
        )

    cues = build_auto_cues(video, meta, events, not args.no_ocr)
    if not cues:
        raise RecError("没有生成任何字幕（事件里没有可用的点击/快捷键）")

    out = Path(args.out).expanduser().resolve() if args.out else video.with_name(f"{video.stem}.srt")
    if out.suffix.lower() not in (".srt", ".json"):
        out = out.with_suffix(".srt")
    out = unique_path(out)
    if out.suffix.lower() == ".json":
        out.write_text(json.dumps(cues, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        write_srt(cues, out)

    result = {"ok": True, "subtitles": str(out), "cues": len(cues)}
    lines = [f"✅ 已生成 {len(cues)} 条字幕：{out}"]
    for c in cues[:8]:
        lines.append(f"   {c['start']:6.2f} → {c['end']:6.2f}  {c['text']}")
    if len(cues) > 8:
        lines.append(f"   … 其余 {len(cues) - 8} 条见文件")

    if args.burn:
        burned = unique_path(video.with_name(f"{video.stem}-sub.mp4"))
        burn_subtitles(video, cues, burned, args)
        result["video"] = str(burned)
        lines.append(f"🎬 已烧录字幕：{burned}")

    print_result(result, args.json, lines)
    return 0


def cmd_subtitle_burn(args) -> int:
    preflight("ffmpeg", "pillow")
    video = Path(args.video).expanduser().resolve()
    sub = Path(args.sub).expanduser().resolve()
    if not video.exists():
        raise RecError(f"视频不存在: {video}")
    if not sub.exists():
        raise RecError(f"字幕文件不存在: {sub}")
    cues = load_cues(sub)
    if not cues:
        raise RecError("字幕文件里没有有效条目")
    out = Path(args.out).expanduser().resolve() if args.out else video.with_name(f"{video.stem}-sub.mp4")
    out = unique_path(out)
    final = burn_subtitles(video, cues, out, args)
    data = {"ok": True, "video": str(out), "cues": len(cues),
            "duration": final["duration"], "size": final["size"]}
    print_result(data, args.json, [
        f"✅ 字幕已烧录：{out}",
        f"   {len(cues)} 条字幕 · {final['width']}x{final['height']} · {human_size(final['size'])}",
    ])
    return 0


# ---------------------------------------------------------------- 系统权限


def cmd_permissions(args) -> int:
    if sys.platform != "darwin":
        raise RecError("mac-screen-record 仅支持 macOS")
    layout = screen_info(request=args.request)
    rows = []
    for key in ("screen_capture", "listen_event", "accessibility", "microphone"):
        title, use, url = PERMISSION_PANES[key]
        ok, label = permission_status(layout, key)
        rows.append({"key": key, "title": title, "use": use, "url": url,
                     "ok": ok, "label": label})

    core_keys = {"screen_capture", "listen_event"}
    core_ok = all(r["ok"] for r in rows if r["key"] in core_keys)

    def mark_for(r):
        if r["ok"] is True:
            return "✅"
        if r["ok"] is None:
            return "⚠️"
        return "❌" if r["key"] in core_keys else "⚠️"

    lines = ["🔐 系统权限检查", ""]
    for r in rows:
        lines.append(f"{mark_for(r)} {r['title']}：{r['label']}")
        lines.append(f"   用途：{r['use']}")
        if not r["ok"]:
            lines.append(f"   开启：系统设置 → 隐私与安全性 → {r['title']}")
            lines.append("        勾选运行本命令的终端/宿主 App，然后完全退出并重开它")
            lines.append(f"   面板：{r['url']}")

    missing = [r for r in rows if not r["ok"]]
    target = next((r for r in missing if r["key"] in core_keys), None) or (missing[0] if missing else None)
    if args.open and target:
        try:
            subprocess.run(["open", target["url"]])
            lines.append("")
            lines.append(f"已打开「{target['title']}」设置面板，请手动勾选后重启终端/宿主 App。")
        except Exception as e:
            lines.append(f"打开设置面板失败：{e}")
    elif missing:
        lines.append("")
        lines.append("一键打开设置面板：python3 scripts/screen_rec.py permissions --open")

    if args.request:
        lines.append("已触发系统授权弹窗；若没看到，请用 --open 打开设置面板手动勾选。")
    lines.append("")
    lines.append("提示：勾选权限后必须完全退出并重开终端/宿主 App 才生效。")

    data = {"ok": core_ok, "permissions": rows, "displays": layout.get("displays")}
    print_result(data, args.json, lines)
    return 0 if core_ok else 1


# ---------------------------------------------------------------- 环境准备


def cmd_ensure(args) -> int:
    if sys.platform != "darwin":
        raise RecError("mac-screen-record 仅支持 macOS")

    checks = []

    def add(name, ok, detail, level="required"):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "level": level})

    def progress(msg):
        print(msg, file=sys.stderr)

    progress("🩺 mac-screen-record 环境准备" + ("（仅检查，不安装）" if args.check else ""))
    progress("")

    add("macOS", True, platform.mac_ver()[0] or "macOS")

    sc = which("screencapture") or "/usr/sbin/screencapture"
    add("screencapture（录屏/截图）", Path(sc).exists(), sc)

    def ffmpeg_state():
        try:
            ff = ffmpeg()
            ver = run([ff, "-version"]).stdout.splitlines()[0][:70]
            enc = run([ff, "-hide_banner", "-encoders"]).stdout
            if "libx264" not in enc:
                return False, f"{ff} 缺少 libx264 编码器（建议升级 ffmpeg）"
            return True, f"{ff}（{ver}）"
        except RecError as e:
            return False, str(e)

    ok, detail = ffmpeg_state()
    if not ok and not args.check:
        brew = which("brew")
        if brew:
            progress("⬇️  ffmpeg 缺失，执行 brew install ffmpeg（可能需要几分钟）…")
            try:
                run_stream([brew, "install", "ffmpeg"], timeout=1800)
            except RecError as e:
                progress(f"⚠️  ffmpeg 安装失败：{e}")
        else:
            add("Homebrew", False, "未安装 Homebrew，无法自动安装 ffmpeg；请先安装 https://brew.sh")
        ok, detail = ffmpeg_state()
    add("ffmpeg / ffprobe（转码/切片/GIF/字幕）", ok, detail)

    def pillow_state():
        try:
            from PIL import __version__ as v
            where = "技能缓存" if str(PYDEPS_DIR) in sys.path else "系统环境"
            return True, f"Pillow {v}（{where}）"
        except Exception:
            return False, f"未安装（字幕烧录需要；将装到 {PYDEPS_DIR}）"

    ok, detail = pillow_state()
    if not ok and not args.check:
        progress(f"⬇️  Pillow 缺失，安装到 {PYDEPS_DIR} …")
        try:
            run_stream([
                sys.executable, "-m", "pip", "install", "--upgrade",
                "--target", str(PYDEPS_DIR), "pillow", "--disable-pip-version-check",
            ], timeout=600)
            import importlib
            importlib.invalidate_caches()
            if str(PYDEPS_DIR) not in sys.path:
                sys.path.insert(0, str(PYDEPS_DIR))
        except RecError as e:
            progress(f"⚠️  Pillow 安装失败：{e}")
        ok, detail = pillow_state()
    add("Pillow（字幕烧录）", ok, detail)

    compiler = swiftc()
    add("Xcode Command Line Tools（事件/OCR/悬浮窗）", bool(compiler), compiler or "未安装")
    if not compiler and not args.check:
        xc = which("xcode-select")
        if xc:
            progress("⬇️  触发 Xcode Command Line Tools 安装（会弹出系统安装器）…")
            try:
                subprocess.run([xc, "--install"], timeout=60)
            except Exception:
                pass
            progress("   请在弹窗中完成安装，然后重新运行 ensure")
        else:
            progress("   请手动安装：xcode-select --install")

    if compiler:
        for name in ("screen_info", "ocr", "event_tap", "rec_overlay"):
            try:
                p = ensure_helper(name)
                add(f"辅助程序 {name}", True, str(p), level="optional")
            except RecError as e:
                add(f"辅助程序 {name}", False, str(e), level="optional")

    font = next((p for p in FONT_CANDIDATES if Path(p).exists()), None)
    add("中文字体", bool(font), font or "未找到", level="optional")

    try:
        layout = screen_info()
        screen_ok = bool(layout.get("screen_capture"))
        listen_ok = bool(layout.get("listen_event"))
        add("屏幕录制权限", screen_ok,
            "已授权" if screen_ok else
            "系统设置 → 隐私与安全性 → 屏幕录制（macOS 15+「屏幕与系统音频录制」），勾选终端/宿主 App 后完全重启它",
            level="permission")
        add("输入监控权限", listen_ok,
            "已授权" if listen_ok else
            "系统设置 → 隐私与安全性 → 输入监控/辅助功能，勾选终端/宿主 App 后完全重启它（事件采集/智能字幕需要）",
            level="permission")
    except RecError as e:
        add("权限检查", False, f"无法检查：{e}", level="permission")

    required_ok = all(c["ok"] for c in checks if c["level"] == "required")
    perms_ok = all(c["ok"] for c in checks if c["level"] == "permission")

    lines = ["🩺 mac-screen-record 环境准备" + ("（仅检查）" if args.check else ""), ""]
    for c in checks:
        if c["ok"]:
            mark = "✅"
        elif c["level"] == "optional":
            mark = "ℹ️"
        elif c["level"] == "permission":
            mark = "⚠️"
        else:
            mark = "❌"
        lines.append(f"{mark} {c['name']}: {c['detail']}")
    lines.append("")
    lines.append("✅ 必需依赖已就绪。" if required_ok else "❌ 仍有必需依赖缺失，按上面提示处理后重试。")
    if not perms_ok:
        lines.append("⚠️  权限未完全授权：录屏/截图可用，但事件采集、智能字幕、录制悬浮窗会降级或不可用。")
        lines.append("   检查/打开设置面板：python3 scripts/screen_rec.py permissions --open")
    lines.append(f"缓存目录：{CACHE_DIR}")

    data = {"ok": required_ok, "checks": checks, "cache": str(CACHE_DIR), "pydeps": str(PYDEPS_DIR)}
    print_result(data, args.json, lines)
    return 0 if required_ok else 1


# ---------------------------------------------------------------- doctor


def cmd_doctor(args) -> int:
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    sc = which("screencapture") or "/usr/sbin/screencapture"
    add("screencapture", Path(sc).exists(), sc)

    try:
        ff_path = ffmpeg()
        ff_ver = run([ff_path, "-version"]).stdout.splitlines()[0][:80]
        add("ffmpeg", True, f"{ff_path}（{ff_ver}）")
    except RecError as e:
        add("ffmpeg", False, str(e))
    try:
        add("ffprobe", True, ffprobe())
    except RecError as e:
        add("ffprobe", False, str(e))

    try:
        from PIL import __version__ as pil_version  # noqa
        add("Pillow（字幕烧录，可选）", True, f"Pillow {pil_version}")
    except ImportError:
        add("Pillow（字幕烧录，可选）", False, "未安装：运行 ensure 自动安装")

    compiler = swiftc()
    add("swiftc（事件/OCR 辅助）", bool(compiler), compiler or "xcode-select --install")

    font = next((p for p in FONT_CANDIDATES if Path(p).exists()), None)
    add("中文字体", bool(font), font or "未找到系统字体")

    try:
        layout = screen_info()
        add("屏幕录制权限", layout.get("screen_capture"), "系统设置 → 隐私与安全性 → 屏幕录制")
        add("输入监控权限（事件采集）", layout.get("listen_event"), "系统设置 → 隐私与安全性 → 输入监控/辅助功能")
        add("屏幕布局", True, ", ".join(
            f'#{d["index"]} {d["pixel_w"]}x{d["pixel_h"]}@{d["scale"]}x' for d in layout.get("displays", [])
        ))
    except RecError as e:
        add("屏幕信息", False, str(e))

    try:
        has_libass = "subtitles" in run([ffmpeg(), "-hide_banner", "-filters"]).stdout.split()
        add("ffmpeg subtitle 滤镜（可选）", True, "可用（会优先用更快的 libass 路径）" if has_libass
            else "不可用（不影响，默认走 PIL overlay 方案）")
    except RecError:
        pass

    ok_all = all(c["ok"] for c in checks if "可选" not in c["name"])
    data = {"ok": ok_all, "checks": checks, "cache": str(CACHE_DIR)}
    lines = ["🩺 mac-screen-record 自检", ""]
    for c in checks:
        mark = "✅" if c["ok"] else ("ℹ️" if "可选" in c["name"] else "❌")
        lines.append(f"{mark} {c['name']}: {c['detail']}")
    lines += ["", f"缓存目录：{CACHE_DIR}",
              "首次使用或换机：先运行 python3 scripts/screen_rec.py ensure 自动准备依赖",
              "权限检查/打开设置：python3 scripts/screen_rec.py permissions --open"]
    if not ok_all:
        lines.append("有检查未通过，按上面提示处理后重试。")
    print_result(data, args.json, lines)
    return 0 if ok_all else 1


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="输出 JSON（便于脚本/Agent 解析）")

    p = argparse.ArgumentParser(
        prog="screen_rec.py",
        description="macOS 命令行录屏 / 截图 / 切片 / GIF / 压缩 / 智能字幕",
        parents=[common],
    )
    p.add_argument("--version", action="version", version=f"mac-screen-record {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    # permissions
    pm = sub.add_parser("permissions", parents=[common], help="检查系统权限并引导开启")
    pm.add_argument("--open", action="store_true", help="打开缺失权限对应的系统设置面板")
    pm.add_argument("--request", action="store_true", help="触发系统授权弹窗（屏幕录制/输入监控）")

    # ensure
    en = sub.add_parser("ensure", aliases=["setup", "install"], parents=[common],
                        help="检查并安装依赖、编译辅助程序（首次使用先跑）")
    en.add_argument("--check", action="store_true", help="只检查，不安装")

    # doctor
    sub.add_parser("doctor", parents=[common], help="环境、依赖与权限自检（只读）")

    # record
    r = sub.add_parser("record", parents=[common], help="录制屏幕，默认输出 mp4")
    r.add_argument("-d", "--duration", type=float, help="录制时长（秒）；不填则 Ctrl+C 停止")
    r.add_argument("--region", help="录制区域 x,y,w,h（全局坐标，单位 point）")
    r.add_argument("--display", type=int, help="录制指定屏幕（1=主屏）")
    r.add_argument("--audio", default="none", help="none | mic（默认输入）| 数字设备 ID")
    r.add_argument("--cursor", action="store_true", help="录制鼠标指针")
    r.add_argument("--clicks", action="store_true", help="在视频中显示点击效果")
    r.add_argument("--no-events", action="store_true", help="不采集点击/快捷键事件")
    r.add_argument("--countdown", type=int, default=0, help="延时开始（秒）")
    r.add_argument("-o", "--out", help="输出文件或目录（默认 ~/Desktop/Recordings）")
    r.add_argument("--keep-mov", action="store_true", help="保留 screencapture 原始 .mov")
    r.add_argument("--no-compress", action="store_true", help="不压缩，仅 remux 成 mp4")
    r.add_argument("--crf", type=int, default=23, help="x264 CRF，越小越清晰/越大（默认 23）")
    r.add_argument("--max-height", type=int, default=1080, help="最大高度（默认 1080，0=不缩放）")
    r.add_argument("--fps", type=int, default=30, help="输出帧率（默认 30，0=保持原样）")
    r.add_argument("--gif", action="store_true", help="同时生成 GIF")
    r.add_argument("--gif-width", type=int, default=960, help="GIF 宽度（默认 960）")
    r.add_argument("--gif-fps", type=int, default=10, help="GIF 帧率（默认 10）")
    r.add_argument("--gif-colors", type=int, default=128, help="GIF 调色板颜色数（默认 128）")
    r.add_argument("--force", action="store_true", help="跳过屏幕录制权限预检")
    r.add_argument("--no-overlay", action="store_true", help="不显示录制计时悬浮窗（默认显示）")
    r.add_argument("--overlay-visible", action="store_true", help="让悬浮窗进入录制画面（默认不进入）")
    r.add_argument("--overlay-position", choices=["top-right", "top-left", "bottom-right", "bottom-left"],
                   default="top-right", help="悬浮窗位置（默认右上角）")
    r.add_argument("--overlay-total", type=int, help="悬浮窗显示的总时长秒数（默认取 --duration）")
    r.add_argument("--overlay-delay", type=float, default=0.9, help="悬浮窗延迟出现秒数（用于对齐首帧）")

    # shot
    s = sub.add_parser("shot", parents=[common], help="截图")
    s.add_argument("--region", help="区域 x,y,w,h")
    s.add_argument("--display", type=int, help="指定屏幕")
    s.add_argument("--window", action="store_true", help="交互式选择窗口（需要人工点选）")
    s.add_argument("--delay", type=int, help="延时秒数")
    s.add_argument("--format", default="png", help="png/jpg/pdf/tiff")
    s.add_argument("--clipboard", action="store_true", help="复制到剪贴板（不落盘）")
    s.add_argument("--cursor", action="store_true", help="包含鼠标指针")
    s.add_argument("-o", "--out", help="输出文件或目录")
    s.add_argument("--force", action="store_true", help="跳过屏幕录制权限预检")

    # clip
    c = sub.add_parser("clip", parents=[common], help="视频切片")
    c.add_argument("input", help="输入视频")
    c.add_argument("--start", help="起点（秒或 HH:MM:SS）")
    c.add_argument("--end", help="终点（秒或 HH:MM:SS）")
    c.add_argument("--duration", help="时长（与 --end 二选一）")
    c.add_argument("--fast", action="store_true", help="流拷贝（快，但起点对齐关键帧）")
    c.add_argument("-o", "--out", help="输出文件")

    # gif
    g = sub.add_parser("gif", parents=[common], help="视频转 GIF")
    g.add_argument("input", help="输入视频")
    g.add_argument("--start", help="起点")
    g.add_argument("--end", help="终点")
    g.add_argument("--duration", help="时长")
    g.add_argument("--width", type=int, default=960, help="宽度（默认 960）")
    g.add_argument("--fps", type=int, default=10, help="帧率（默认 10）")
    g.add_argument("--colors", type=int, default=128, help="调色板颜色数")
    g.add_argument("-o", "--out", help="输出文件")

    # compress
    z = sub.add_parser("compress", parents=[common], help="压缩转 mp4")
    z.add_argument("input", help="输入视频")
    z.add_argument("--crf", type=int, default=24, help="CRF（默认 24）")
    z.add_argument("--max-height", type=int, default=1080, help="最大高度（默认 1080，0=不缩放）")
    z.add_argument("--fps", type=int, default=30, help="帧率（默认 30，0=保持）")
    z.add_argument("--preset", default="veryfast", help="x264 preset")
    z.add_argument("-o", "--out", help="输出文件")

    # subtitle
    st = sub.add_parser("subtitle", parents=[common], help="智能字幕：draft / auto / burn")
    st_sub = st.add_subparsers(dest="subcommand", required=True)

    d = st_sub.add_parser("draft", parents=[common], help="抽关键帧 + 事件清单，供 Agent 读图写字幕")
    d.add_argument("video", help="视频文件")
    d.add_argument("--events", help="事件 jsonl（默认读 <video>.meta.json 指向的文件）")
    d.add_argument("--scene", action="store_true", help="强制使用画面变化检测抽帧")
    d.add_argument("--interval", type=float, default=3.0, help="无场景变化时的采样间隔（默认 3s）")
    d.add_argument("--max", type=int, default=40, help="最多抽多少帧（默认 40）")
    d.add_argument("--ocr", action="store_true", help="对每帧做 OCR 并写入清单（帮助理解界面）")
    d.add_argument("-o", "--out", help="输出目录（默认 <视频>-subtitle）")

    a = st_sub.add_parser("auto", parents=[common], help="事件 + OCR 自动生成字幕")
    a.add_argument("video", help="视频文件")
    a.add_argument("--events", help="事件 jsonl")
    a.add_argument("--no-ocr", action="store_true", help="不做 OCR，只用事件与应用名")
    a.add_argument("--burn", action="store_true", help="生成后立即烧录")
    a.add_argument("--out", help="输出 .srt / .json")
    a.add_argument("--max-height", type=int, default=1080, help="烧录时最大高度")
    a.add_argument("--fps", type=int, default=30, help="烧录时帧率")
    a.add_argument("--crf", type=int, default=20, help="烧录时 CRF")
    a.add_argument("--font", help="字体文件路径")
    a.add_argument("--font-size", type=int, help="字号（默认按分辨率自动）")
    a.add_argument("--position", choices=["bottom", "top"], default="bottom", help="字幕位置")
    a.add_argument("--color", default="#FFFFFF", help="文字颜色（默认白）")
    a.add_argument("--bg-opacity", type=float, default=0.65, help="背景不透明度（0~1）")
    a.add_argument("--no-bg", action="store_true", help="不要背景框")

    b = st_sub.add_parser("burn", parents=[common], help="把 SRT/JSON 字幕烧录进视频")
    b.add_argument("video", help="视频文件")
    b.add_argument("--sub", required=True, help="SRT 或 JSON 字幕文件")
    b.add_argument("-o", "--out", help="输出文件（默认 <视频>-sub.mp4）")
    b.add_argument("--max-height", type=int, default=1080, help="输出最大高度（默认 1080）")
    b.add_argument("--fps", type=int, default=30, help="输出帧率（默认 30）")
    b.add_argument("--crf", type=int, default=20, help="CRF（默认 20）")
    b.add_argument("--font", help="字体文件路径")
    b.add_argument("--font-size", type=int, help="字号（默认按分辨率自动）")
    b.add_argument("--position", choices=["bottom", "top"], default="bottom", help="字幕位置")
    b.add_argument("--color", default="#FFFFFF", help="文字颜色")
    b.add_argument("--bg-opacity", type=float, default=0.65, help="背景不透明度")
    b.add_argument("--no-bg", action="store_true", help="不要背景框")

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "json"):
        args.json = False
    try:
        if args.command == "permissions":
            return cmd_permissions(args)
        if args.command in ("ensure", "setup", "install"):
            return cmd_ensure(args)
        if args.command == "doctor":
            return cmd_doctor(args)
        if args.command == "record":
            return cmd_record(args)
        if args.command == "shot":
            return cmd_shot(args)
        if args.command == "clip":
            return cmd_clip(args)
        if args.command == "gif":
            return cmd_gif(args)
        if args.command == "compress":
            return cmd_compress(args)
        if args.command == "subtitle":
            if args.subcommand == "draft":
                return cmd_subtitle_draft(args)
            if args.subcommand == "auto":
                return cmd_subtitle_auto(args)
            if args.subcommand == "burn":
                return cmd_subtitle_burn(args)
        parser.error("未知命令")
    except RecError as e:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        else:
            print(f"❌ {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
