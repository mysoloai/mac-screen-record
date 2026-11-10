// 输出屏幕布局与系统权限状态（JSON）。
// 坐标口径：CoreGraphics 全局显示坐标（主屏左上为原点，y 向下），与 CGEvent 一致。
// --request: 触发系统授权弹窗（屏幕录制 / 输入监控；麦克风由 screencapture -g 自行触发）

import Foundation
import CoreGraphics
import AppKit
import AVFoundation
import ApplicationServices

let request = CommandLine.arguments.contains("--request")

if request {
    _ = CGRequestScreenCaptureAccess()
    _ = CGRequestListenEventAccess()
}

var out: [String: Any] = [:]
var displays: [[String: Any]] = []

let maxDisplays: UInt32 = 16
var ids = [CGDirectDisplayID](repeating: 0, count: Int(maxDisplays))
var count: UInt32 = 0
CGGetActiveDisplayList(maxDisplays, &ids, &count)
let mainID = CGMainDisplayID()

for i in 0..<Int(count) {
    let id = ids[i]
    let b = CGDisplayBounds(id)
    let pw = CGDisplayPixelsWide(id)
    let ph = CGDisplayPixelsHigh(id)
    let scale = b.size.width > 0 ? Double(pw) / Double(b.size.width) : 1.0
    displays.append([
        "index": i + 1,
        "x": Double(b.origin.x),
        "y": Double(b.origin.y),
        "w": Double(b.size.width),
        "h": Double(b.size.height),
        "pixel_w": Int(pw),
        "pixel_h": Int(ph),
        "scale": scale,
        "main": id == mainID
    ])
}

out["displays"] = displays
out["screen_capture"] = CGPreflightScreenCaptureAccess()
out["listen_event"] = CGPreflightListenEventAccess()
out["accessibility"] = AXIsProcessTrusted()

switch AVCaptureDevice.authorizationStatus(for: .audio) {
case .authorized: out["microphone"] = "authorized"
case .denied: out["microphone"] = "denied"
case .restricted: out["microphone"] = "restricted"
case .notDetermined: out["microphone"] = "not_determined"
@unknown default: out["microphone"] = "unknown"
}

let data = try JSONSerialization.data(withJSONObject: out, options: [.sortedKeys])
print(String(data: data, encoding: .utf8)!)
