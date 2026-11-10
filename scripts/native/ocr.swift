// Apple Vision 中文 OCR，输出 JSON 数组（像素坐标，左上原点）。
// 用法: ocr <image> [x y w h]
// 依赖系统 Vision 框架，无需网络与额外权限。

import Foundation
import Vision
import AppKit

let args = CommandLine.arguments
guard args.count >= 2, let img = NSImage(contentsOfFile: args[1]) else {
    FileHandle.standardError.write("usage: ocr <image> [x y w h]\n".data(using: .utf8)!)
    exit(64)
}

var proposed = NSRect(origin: .zero, size: img.size)
guard let full = img.cgImage(forProposedRect: &proposed, context: nil, hints: nil) else {
    exit(65)
}

var cg = full
var ox: CGFloat = 0
var oy: CGFloat = 0
if args.count >= 6,
   let x = Double(args[2]), let y = Double(args[3]),
   let w = Double(args[4]), let h = Double(args[5]) {
    let W = CGFloat(full.width), H = CGFloat(full.height)
    let cx = max(0, min(CGFloat(x), W - 1))
    let cy = max(0, min(CGFloat(y), H - 1))
    let cw = max(1, min(CGFloat(w), W - cx))
    let ch = max(1, min(CGFloat(h), H - cy))
    if let cropped = full.cropping(to: CGRect(x: cx, y: cy, width: cw, height: ch)) {
        cg = cropped
        ox = cx
        oy = cy
    }
}

let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
req.usesLanguageCorrection = true
req.recognitionLanguages = ["zh-Hans", "en-US"]

let handler = VNImageRequestHandler(cgImage: cg, options: [:])
do {
    try handler.perform([req])
} catch {
    FileHandle.standardError.write("ocr failed: \(error)\n".data(using: .utf8)!)
    exit(66)
}

var out: [[String: Any]] = []
for obs in (req.results ?? []) {
    guard let c = obs.topCandidates(1).first else { continue }
    let bb = obs.boundingBox
    let W = CGFloat(cg.width), H = CGFloat(cg.height)
    out.append([
        "text": c.string,
        "confidence": Double(c.confidence),
        "x": Double(ox + bb.minX * W),
        "y": Double(oy + (1 - bb.maxY) * H),
        "w": Double(bb.width * W),
        "h": Double(bb.height * H)
    ])
}

let data = try JSONSerialization.data(withJSONObject: out, options: [])
print(String(data: data, encoding: .utf8)!)
