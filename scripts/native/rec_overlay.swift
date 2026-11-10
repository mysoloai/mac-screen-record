// 录制状态悬浮窗：显示红点 + REC + 已录时长（可带总时长）。
// 默认 sharingType = .none：用户能看到，但不会出现在 screencapture 的截图/录屏里。
// --visible 时改为 .readOnly，让悬浮窗进入录制画面。
//
// 用法: rec_overlay [--total 秒] [--position top-right|top-left|bottom-right|bottom-left]
//                   [--screen N] [--delay 0.9] [--duration 秒] [--visible]

import Cocoa

var total: Int? = nil
var visible = false
var position = "top-right"
var delay: Double = 0.9
var autoExit: Double? = nil
var screenIndex = 1
var title = "REC"

let rawArgs = Array(CommandLine.arguments.dropFirst())
var i = 0
while i < rawArgs.count {
    switch rawArgs[i] {
    case "--total":
        if i + 1 < rawArgs.count, let v = Int(rawArgs[i + 1]) { total = v; i += 1 }
    case "--position":
        if i + 1 < rawArgs.count { position = rawArgs[i + 1]; i += 1 }
    case "--screen":
        if i + 1 < rawArgs.count, let v = Int(rawArgs[i + 1]) { screenIndex = v; i += 1 }
    case "--delay":
        if i + 1 < rawArgs.count, let v = Double(rawArgs[i + 1]) { delay = v; i += 1 }
    case "--duration":
        if i + 1 < rawArgs.count, let v = Double(rawArgs[i + 1]) { autoExit = v; i += 1 }
    case "--title":
        if i + 1 < rawArgs.count { title = rawArgs[i + 1]; i += 1 }
    case "--visible":
        visible = true
    default:
        break
    }
    i += 1
}

func fmt(_ seconds: Int) -> String {
    let v = max(0, seconds)
    if v < 3600 {
        return String(format: "%02d:%02d", v / 60, v % 60)
    }
    return String(format: "%d:%02d:%02d", v / 3600, (v % 3600) / 60, v % 60)
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)

let W: CGFloat = 238
let H: CGFloat = 54

let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: W, height: H),
                   styleMask: [.borderless], backing: .buffered, defer: false)
win.level = .statusBar
win.isOpaque = false
win.backgroundColor = .clear
win.hasShadow = true
win.ignoresMouseEvents = true
win.animationBehavior = .none
// 关键：none = 不进入屏幕捕获；readOnly = 会被录进去
win.sharingType = visible ? .readOnly : .none
win.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]

let content = NSView(frame: NSRect(x: 0, y: 0, width: W, height: H))
content.wantsLayer = true
content.layer?.backgroundColor = NSColor(calibratedWhite: 0.04, alpha: 0.76).cgColor
content.layer?.cornerRadius = H / 2
content.layer?.borderWidth = 1
content.layer?.borderColor = NSColor.white.withAlphaComponent(0.14).cgColor
content.layer?.masksToBounds = true

let dot = NSView(frame: NSRect(x: 18, y: (H - 10) / 2, width: 10, height: 10))
dot.wantsLayer = true
dot.layer?.backgroundColor = NSColor(calibratedRed: 1.0, green: 0.28, blue: 0.28, alpha: 1.0).cgColor
dot.layer?.cornerRadius = 5
content.addSubview(dot)

let label = NSTextField(labelWithString: "\(title) 00:00")
label.font = NSFont.monospacedDigitSystemFont(ofSize: 20, weight: .semibold)
label.textColor = NSColor.white
label.frame = NSRect(x: 38, y: (H - 26) / 2, width: W - 54, height: 26)
label.alignment = .left
content.addSubview(label)
win.contentView = content

func place() {
    var screen = NSScreen.main
    if screenIndex >= 1 && screenIndex <= NSScreen.screens.count {
        screen = NSScreen.screens[screenIndex - 1]
    }
    guard let f = screen?.visibleFrame else { return }
    var x = f.maxX - W - 20
    var y = f.maxY - H - 20
    switch position {
    case "top-left":
        x = f.minX + 20
        y = f.maxY - H - 20
    case "bottom-left":
        x = f.minX + 20
        y = f.minY + 20
    case "bottom-right":
        x = f.maxX - W - 20
        y = f.minY + 20
    default:
        break
    }
    win.setFrameOrigin(NSPoint(x: x, y: y))
}

var startDate = Date()
var lastSecond = -1
var tick = 0

func refresh() {
    let elapsed = Int(Date().timeIntervalSince(startDate))
    if elapsed != lastSecond {
        lastSecond = elapsed
        if let t = total {
            label.stringValue = "\(title) \(fmt(elapsed)) / \(fmt(t))"
        } else {
            label.stringValue = "\(title) \(fmt(elapsed))"
        }
    }
    tick += 1
    dot.layer?.opacity = (tick % 4 < 2) ? 1.0 : 0.35
}

let timer = Timer(timeInterval: 0.2, repeats: true) { _ in refresh() }
RunLoop.current.add(timer, forMode: .common)

// 父进程意外退出时不留下孤儿悬浮窗
let watchdog = Timer(timeInterval: 1.0, repeats: true) { _ in
    if getppid() == 1 { exit(0) }
}
RunLoop.current.add(watchdog, forMode: .common)

DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
    place()
    startDate = Date()
    lastSecond = -1
    refresh()
    win.orderFrontRegardless()
}

if let d = autoExit {
    DispatchQueue.main.asyncAfter(deadline: .now() + delay + d) { exit(0) }
}

signal(SIGTERM) { _ in exit(0) }
signal(SIGINT) { _ in exit(0) }
app.run()
