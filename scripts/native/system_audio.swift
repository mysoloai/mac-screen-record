// 用 ScreenCaptureKit 采集「电脑里播放的声音」（系统音频），输出 m4a。
// 不需要安装 BlackHole 等虚拟声卡；需要 macOS 13+ 与屏幕录制权限。
//
// 用法: system_audio <out.m4a> [--duration 秒] [--check]
// 启动成功后向 stderr 打印: AUDIO_STARTED <epoch 秒>

import Foundation
import ScreenCaptureKit
import AVFoundation
import CoreMedia
import CoreGraphics

func fail(_ msg: String, code: Int32) -> Never {
    FileHandle.standardError.write("AUDIO_ERROR \(msg)\n".data(using: .utf8)!)
    exit(code)
}

if CommandLine.arguments.contains("--check") {
    if #available(macOS 13.0, *) {
        let obj: [String: Any] = [
            "available": true,
            "screen_capture": CGPreflightScreenCaptureAccess()
        ]
        let data = try! JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])
        print(String(data: data, encoding: .utf8)!)
        exit(0)
    } else {
        let obj: [String: Any] = ["available": false, "reason": "ScreenCaptureKit audio requires macOS 13+"]
        let data = try! JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])
        print(String(data: data, encoding: .utf8)!)
        exit(2)
    }
}

guard #available(macOS 13.0, *) else {
    fail("ScreenCaptureKit audio requires macOS 13+", code: 2)
}

var outPath = "/tmp/mac-screen-record-system.m4a"
var duration: Double = 0
var args = Array(CommandLine.arguments.dropFirst())
var i = 0
while i < args.count {
    switch args[i] {
    case "--duration":
        if i + 1 < args.count, let v = Double(args[i + 1]) { duration = v; i += 1 }
    case "--check":
        break
    default:
        if !args[i].hasPrefix("--") { outPath = args[i] }
    }
    i += 1
}

let outputURL = URL(fileURLWithPath: outPath)
try? FileManager.default.removeItem(at: outputURL)

@available(macOS 13.0, *)
final class AudioRecorder: NSObject, SCStreamOutput, SCStreamDelegate {
    private let writer: AVAssetWriter
    private let input: AVAssetWriterInput
    private var stream: SCStream?
    private var sessionStarted = false
    private var samples: Int = 0

    init(output: URL) throws {
        writer = try AVAssetWriter(outputURL: output, fileType: .m4a)
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: 48000,
            AVNumberOfChannelsKey: 2,
            AVEncoderBitRateKey: 160000
        ]
        input = AVAssetWriterInput(mediaType: .audio, outputSettings: settings)
        input.expectsMediaDataInRealTime = true
        writer.add(input)
        guard writer.startWriting() else {
            throw NSError(domain: "sck", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "AVAssetWriter startWriting failed"])
        }
    }

    func start() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        guard let display = content.displays.first else {
            throw NSError(domain: "sck", code: 2,
                          userInfo: [NSLocalizedDescriptionKey: "no display found"])
        }
        let filter = SCContentFilter(display: display, excludingWindows: [])
        let config = SCStreamConfiguration()
        config.capturesAudio = true
        // 不排除自身进程音频：技能本身不发声，保留全部系统声音更稳妥
        config.excludesCurrentProcessAudio = false
        config.sampleRate = 48000
        config.channelCount = 2
        config.width = 16
        config.height = 16
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)

        let s = SCStream(filter: filter, configuration: config, delegate: self)
        try s.addStreamOutput(self, type: .audio, sampleHandlerQueue: DispatchQueue(label: "sck.audio"))
        self.stream = s
        try await s.startCapture()
        FileHandle.standardError.write("AUDIO_STARTED \(Date().timeIntervalSince1970)\n".data(using: .utf8)!)
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid, CMSampleBufferDataIsReady(sampleBuffer) else { return }
        if !sessionStarted {
            sessionStarted = true
            writer.startSession(atSourceTime: CMSampleBufferGetPresentationTimeStamp(sampleBuffer))
        }
        if writer.status == .writing && input.isReadyForMoreMediaData {
            if input.append(sampleBuffer) { samples += 1 }
        }
    }

    func stop() async {
        if let s = stream { try? await s.stopCapture() }
        input.markAsFinished()
        if sessionStarted && samples > 0 {
            await writer.finishWriting()
        } else {
            writer.cancelWriting()
            FileHandle.standardError.write("AUDIO_EMPTY\n".data(using: .utf8)!)
        }
    }
}

let recorder: AudioRecorder
do {
    recorder = try AudioRecorder(output: outputURL)
} catch {
    fail("init failed: \(error)", code: 3)
}

var finished = false
func finish() {
    if finished { return }
    finished = true
    Task {
        await recorder.stop()
        exit(0)
    }
    // 兜底：stopCapture/finishWriting 偶发挂起时也保证进程退出，避免占住 SCK 音频
    DispatchQueue.main.asyncAfter(deadline: .now() + 4) { exit(0) }
}

for sig in [SIGTERM, SIGINT] {
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler { finish() }
    src.resume()
    signal(sig, SIG_IGN)
}

Task {
    do {
        try await recorder.start()
    } catch {
        fail("start failed: \(error)", code: 4)
    }
}

if duration > 0 {
    DispatchQueue.main.asyncAfter(deadline: .now() + duration + 1.0) { finish() }
}

RunLoop.current.run()
