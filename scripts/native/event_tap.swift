// 采集全局点击 / 快捷键 / 前台应用切换事件，输出 JSONL。
// 只记录键码与修饰键，不记录字符内容（不构成键盘记录）。
// 用法: event_tap <out.jsonl>
// 权限: 系统设置 → 隐私与安全性 → 输入监控（或辅助功能）

import Foundation
import Cocoa

let outPath = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "-"
if outPath != "-" && !FileManager.default.fileExists(atPath: outPath) {
    FileManager.default.createFile(atPath: outPath, contents: nil)
}
let out = outPath == "-" ? FileHandle.standardOutput : (FileHandle(forWritingAtPath: outPath) ?? FileHandle.standardOutput)

func emit(_ dict: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: dict, options: [.sortedKeys]) else { return }
    out.write(data)
    out.write("\n".data(using: .utf8)!)
}

let keyNames: [Int64: String] = [
    36: "Return", 48: "Tab", 49: "Space", 51: "Delete", 53: "Escape", 76: "Enter",
    115: "Home", 119: "End", 116: "PageUp", 121: "PageDown",
    123: "Left", 124: "Right", 125: "Down", 126: "Up",
    43: ",", 47: ".", 44: "/", 45: "-", 46: "="
]

let keyLetters: [Int64: String] = [
    0: "A", 1: "S", 2: "D", 3: "F", 4: "H", 5: "G", 6: "Z", 7: "X", 8: "C", 9: "V",
    11: "B", 12: "Q", 13: "W", 14: "E", 15: "R", 16: "Y", 17: "T",
    31: "O", 32: "U", 34: "I", 35: "P", 37: "L", 38: "J", 40: "K",
    41: "N", 42: "M", 18: "1", 19: "2", 20: "3", 21: "4", 23: "5", 22: "6",
    26: "7", 28: "8", 25: "9", 29: "0"
]

func frontmost() -> (String, String) {
    if let app = NSWorkspace.shared.frontmostApplication {
        return (app.localizedName ?? "", app.bundleIdentifier ?? "")
    }
    return ("", "")
}

func mods(_ flags: CGEventFlags) -> [String] {
    var m: [String] = []
    if flags.contains(.maskCommand) { m.append("cmd") }
    if flags.contains(.maskShift) { m.append("shift") }
    if flags.contains(.maskAlternate) { m.append("opt") }
    if flags.contains(.maskControl) { m.append("ctrl") }
    return m
}

var gTap: CFMachPort? = nil

let callback: CGEventTapCallBack = { _, type, event, _ in
    if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
        if let t = gTap { CGEvent.tapEnable(tap: t, enable: true) }
        return Unmanaged.passUnretained(event)
    }
    let ts = Date().timeIntervalSince1970
    let loc = event.location
    let (appName, bundleId) = frontmost()
    switch type {
    case .leftMouseDown, .rightMouseDown, .otherMouseDown:
        let button: String
        switch type {
        case .leftMouseDown: button = "left"
        case .rightMouseDown: button = "right"
        default: button = "other"
        }
        emit(["t": ts, "type": "click", "button": button,
              "x": Double(loc.x), "y": Double(loc.y),
              "app": appName, "bundle": bundleId])
    case .keyDown:
        let code = event.getIntegerValueField(.keyboardEventKeycode)
        let m = mods(event.flags)
        let name = keyNames[code] ?? keyLetters[code] ?? ""
        let isCombo = m.contains("cmd") || m.contains("ctrl")
        // 无修饰键的普通字母不记录，避免变相成为键盘记录
        if !name.isEmpty && (isCombo || keyNames[code] != nil) {
            emit(["t": ts, "type": "key", "code": code, "key": name,
                  "mods": m, "app": appName, "bundle": bundleId])
        }
    default:
        break
    }
    return Unmanaged.passUnretained(event)
}

var mask: CGEventMask = 0
for t in [CGEventType.leftMouseDown, .rightMouseDown, .otherMouseDown, .keyDown] {
    mask |= CGEventMask(1) << CGEventMask(t.rawValue)
}

guard let tap = CGEvent.tapCreate(
    tap: .cgSessionEventTap,
    place: .headInsertEventTap,
    options: .listenOnly,
    eventsOfInterest: mask,
    callback: callback,
    userInfo: nil
) else {
    FileHandle.standardError.write("EVENTTAP_DENIED listen=\(CGPreflightListenEventAccess())\n".data(using: .utf8)!)
    exit(2)
}
gTap = tap

FileHandle.standardError.write("EVENTTAP_READY\n".data(using: .utf8)!)

// 前台应用切换事件（0.5s 轮询）
var lastApp = ""
let (initialApp, initialBundle) = frontmost()
if !initialApp.isEmpty {
    lastApp = initialApp
    emit(["t": Date().timeIntervalSince1970, "type": "app", "app": initialApp, "bundle": initialBundle])
}
let timer = Timer(timeInterval: 0.5, repeats: true) { _ in
    let (name, bundle) = frontmost()
    if !name.isEmpty && name != lastApp {
        lastApp = name
        emit(["t": Date().timeIntervalSince1970, "type": "app", "app": name, "bundle": bundle])
    }
}
RunLoop.current.add(timer, forMode: .common)

let src = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
CFRunLoopAddSource(CFRunLoopGetCurrent(), src, .commonModes)
CGEvent.tapEnable(tap: tap, enable: true)

signal(SIGTERM) { _ in exit(0) }
signal(SIGINT) { _ in exit(0) }
CFRunLoopRun()
