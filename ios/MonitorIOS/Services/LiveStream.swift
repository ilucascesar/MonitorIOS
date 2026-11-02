import AVFoundation
import CoreMedia
import QuartzCore
import UIKit
import VideoToolbox

// Peças do ao vivo de baixa latência: protocolo do servidor, decodificação por hardware
// (VideoToolbox), exibição e áudio G.711. O modelo que junta tudo é o LivePlayerModel.

// MARK: - Protocolo (ver server/app/streaming/hub.py)

enum LiveWire {
    static let videoConfig: UInt8 = 1
    static let video: UInt8 = 2
    static let audioConfig: UInt8 = 3
    static let audio: UInt8 = 4
    static let flagKey: UInt8 = 1
    static let flagCatchUp: UInt8 = 2
    static let headerSize = 10

    struct Message {
        let kind: UInt8
        let flags: UInt8
        let timestamp: UInt32
        let compositionOffset: Int32
        let payload: Data

        var isKey: Bool { flags & LiveWire.flagKey != 0 }
        var isCatchUp: Bool { flags & LiveWire.flagCatchUp != 0 }
        /// Tempo de apresentação em milissegundos.
        var pts: Int64 { Int64(timestamp) + Int64(compositionOffset) }
    }

    static func parse(_ data: Data) -> Message? {
        guard data.count >= headerSize else { return nil }
        let b = [UInt8](data.prefix(headerSize))
        let ts = UInt32(b[2]) << 24 | UInt32(b[3]) << 16 | UInt32(b[4]) << 8 | UInt32(b[5])
        let cts = Int32(bitPattern: UInt32(b[6]) << 24 | UInt32(b[7]) << 16 | UInt32(b[8]) << 8 | UInt32(b[9]))
        let start = data.startIndex + headerSize
        return Message(kind: b[0], flags: b[1], timestamp: ts, compositionOffset: cts,
                       payload: Data(data[start..<data.endIndex]))
    }
}

// MARK: - Parâmetros do codec (avcC / hvcC)

enum ParameterSets {
    /// (tamanho do prefixo dos NALs, [VPS/SPS/PPS]) a partir do registro de configuração.
    static func parse(codec: UInt8, record: [UInt8]) -> (lengthSize: Int, sets: [[UInt8]])? {
        var sets: [[UInt8]] = []
        func read16(_ p: Int) -> Int { Int(record[p]) << 8 | Int(record[p + 1]) }
        if codec == 1 {
            guard record.count > 6 else { return nil }
            let lengthSize = Int(record[4] & 0x03) + 1
            var pos = 5
            for group in 0..<2 {
                guard pos < record.count else { return nil }
                let count = Int(record[pos] & (group == 0 ? 0x1F : 0xFF))
                pos += 1
                for _ in 0..<count {
                    guard pos + 2 <= record.count else { return nil }
                    let n = read16(pos)
                    guard pos + 2 + n <= record.count else { return nil }
                    sets.append(Array(record[(pos + 2)..<(pos + 2 + n)]))
                    pos += 2 + n
                }
            }
            return (lengthSize, sets)
        }
        guard record.count > 23 else { return nil }
        let lengthSize = Int(record[21] & 0x03) + 1
        var pos = 23
        for _ in 0..<Int(record[22]) {
            guard pos + 3 <= record.count else { return nil }
            let count = read16(pos + 1)
            pos += 3
            for _ in 0..<count {
                guard pos + 2 <= record.count else { return nil }
                let n = read16(pos)
                guard pos + 2 + n <= record.count else { return nil }
                sets.append(Array(record[(pos + 2)..<(pos + 2 + n)]))
                pos += 2 + n
            }
        }
        return (lengthSize, sets)
    }

    static func formatDescription(codec: UInt8, record: [UInt8]) -> CMVideoFormatDescription? {
        guard let parsed = parse(codec: codec, record: record), !parsed.sets.isEmpty else { return nil }
        let lengthSize = parsed.lengthSize
        let sets = parsed.sets
        let buffers: [UnsafeMutablePointer<UInt8>] = sets.map { set in
            let p = UnsafeMutablePointer<UInt8>.allocate(capacity: max(set.count, 1))
            p.initialize(from: set, count: set.count)
            return p
        }
        defer { buffers.forEach { $0.deallocate() } }
        let pointers = buffers.map { UnsafePointer($0) }
        let sizes = sets.map { $0.count }
        var format: CMFormatDescription?
        let status: OSStatus
        if codec == 2 {
            status = CMVideoFormatDescriptionCreateFromHEVCParameterSets(
                allocator: kCFAllocatorDefault, parameterSetCount: pointers.count, parameterSetPointers: pointers,
                parameterSetSizes: sizes, nalUnitHeaderLength: Int32(lengthSize), extensions: nil,
                formatDescriptionOut: &format)
        } else {
            status = CMVideoFormatDescriptionCreateFromH264ParameterSets(
                allocator: kCFAllocatorDefault, parameterSetCount: pointers.count, parameterSetPointers: pointers,
                parameterSetSizes: sizes, nalUnitHeaderLength: Int32(lengthSize), formatDescriptionOut: &format)
        }
        return status == noErr ? format : nil
    }
}

// MARK: - Decodificação por hardware

/// Decodifica quadros H.264/H.265 com o VideoToolbox. Saída num thread do sistema.
final class VideoDecoder {
    var onFrame: ((CVPixelBuffer, Double) -> Void)?
    private(set) var format: CMVideoFormatDescription?
    private var session: VTDecompressionSession?
    private var needsKeyframe = true

    var dimensions: CGSize {
        guard let format else { return .zero }
        let d = CMVideoFormatDescriptionGetDimensions(format)
        return CGSize(width: Int(d.width), height: Int(d.height))
    }

    /// Nova configuração do decodificador (início do fluxo ou câmera reiniciada).
    @discardableResult
    func configure(codec: UInt8, record: [UInt8]) -> Bool {
        guard let fmt = ParameterSets.formatDescription(codec: codec, record: record) else { return false }
        if let old = format, session != nil, CMFormatDescriptionEqual(old, otherFormatDescription: fmt) {
            return true
        }
        invalidate()
        format = fmt
        return makeSession()
    }

    private func makeSession() -> Bool {
        guard let format else { return false }
        let attrs: [CFString: Any] = [
            kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
            kCVPixelBufferIOSurfacePropertiesKey: [:] as [String: Any],
        ]
        var s: VTDecompressionSession?
        let status = VTDecompressionSessionCreate(allocator: kCFAllocatorDefault, formatDescription: format,
                                                  decoderSpecification: nil, imageBufferAttributes: attrs as CFDictionary,
                                                  outputCallback: nil, decompressionSessionOut: &s)
        guard status == noErr, let s else { return false }
        VTSessionSetProperty(s, key: kVTDecompressionPropertyKey_RealTime, value: kCFBooleanTrue)
        session = s
        needsKeyframe = true
        return true
    }

    /// ``display == false``: só atualiza o estado do decodificador (quadros já passados do GOP).
    /// ``target``: horário (CACurrentMediaTime) em que o quadro deve aparecer.
    func decode(_ data: Data, pts: Int64, isKey: Bool, display: Bool, target: Double) {
        if needsKeyframe {
            guard isKey else { return }
            needsKeyframe = false
        }
        guard let session, let format, let sample = Self.makeSample(data, format: format, pts: pts) else { return }
        var flags: VTDecodeFrameFlags = [._EnableAsynchronousDecompression, ._1xRealTimePlayback]
        if !display { flags.insert(._DoNotOutputFrame) }
        let status = VTDecompressionSessionDecodeFrame(session, sampleBuffer: sample, flags: flags, infoFlagsOut: nil) {
            [weak self] decodeStatus, _, image, _, _ in
            guard decodeStatus == noErr, let image, display else { return }
            self?.onFrame?(image, target)
        }
        if status != noErr {
            // Sessão invalidada (app voltou do segundo plano, GPU ocupada): recria e espera um keyframe.
            if let s = self.session { VTDecompressionSessionInvalidate(s) }
            self.session = nil
            _ = makeSession()
        }
    }

    func invalidate() {
        if let session {
            VTDecompressionSessionWaitForAsynchronousFrames(session)
            VTDecompressionSessionInvalidate(session)
        }
        session = nil
        needsKeyframe = true
    }

    static func makeSample(_ data: Data, format: CMVideoFormatDescription, pts: Int64) -> CMSampleBuffer? {
        let n = data.count
        guard n > 0 else { return nil }
        var block: CMBlockBuffer?
        guard CMBlockBufferCreateWithMemoryBlock(allocator: kCFAllocatorDefault, memoryBlock: nil, blockLength: n,
                                                 blockAllocator: kCFAllocatorDefault, customBlockSource: nil,
                                                 offsetToData: 0, dataLength: n, flags: kCMBlockBufferAssureMemoryNowFlag,
                                                 blockBufferOut: &block) == kCMBlockBufferNoErr, let block else { return nil }
        let copied = data.withUnsafeBytes { raw -> OSStatus in
            guard let base = raw.baseAddress else { return -1 }
            return CMBlockBufferReplaceDataBytes(with: base, blockBuffer: block, offsetIntoDestination: 0, dataLength: n)
        }
        guard copied == kCMBlockBufferNoErr else { return nil }
        var timing = CMSampleTimingInfo(duration: .invalid, presentationTimeStamp: CMTime(value: pts, timescale: 1000),
                                        decodeTimeStamp: .invalid)
        var size = n
        var sample: CMSampleBuffer?
        guard CMSampleBufferCreateReady(allocator: kCFAllocatorDefault, dataBuffer: block, formatDescription: format,
                                        sampleCount: 1, sampleTimingEntryCount: 1, sampleTimingArray: &timing,
                                        sampleSizeEntryCount: 1, sampleSizeArray: &size, sampleBufferOut: &sample) == noErr
        else { return nil }
        return sample
    }
}

// MARK: - Relógio de exibição (buffer de oscilação adaptativo)

/// Decide quando cada quadro aparece. A base é o menor atraso de chegada observado nos
/// últimos segundos (o caminho mais rápido da rede); a margem cresce quando quadros chegam
/// tarde e volta a diminuir com a rede estável. Resultado: imagem fluida com o mínimo de atraso.
struct JitterClock {
    private var samples: [(at: Double, delay: Double)] = []
    private(set) var margin: Double = 0.12
    private var lastAdjust: Double = 0

    mutating func reset() {
        samples.removeAll()
        margin = 0.12
    }

    mutating func target(ptsSeconds: Double, arrival: Double) -> Double {
        let delay = arrival - ptsSeconds
        samples.append((arrival, delay))
        if samples.count > 8, let first = samples.first, arrival - first.at > 4 {
            samples.removeAll { arrival - $0.at > 4 }
        }
        let base = samples.map { $0.delay }.min() ?? delay
        return ptsSeconds + base + margin
    }

    mutating func frameWasLate(now: Double) {
        margin = min(margin + 0.03, 0.8)
        lastAdjust = now
    }

    mutating func relax(now: Double) {
        if now - lastAdjust > 4 {
            margin = max(margin - 0.02, 0.08)
            lastAdjust = now
        }
    }
}

// MARK: - Exibição

/// Camada de vídeo para quadros já decodificados (exibidos na hora em que são entregues).
final class VideoRenderView: UIView {
    override class var layerClass: AnyClass { AVSampleBufferDisplayLayer.self }
    var displayLayer: AVSampleBufferDisplayLayer { layer as! AVSampleBufferDisplayLayer }

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = .black
        displayLayer.videoGravity = .resizeAspect
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) não usado") }

    func show(_ image: CVPixelBuffer) {
        var format: CMVideoFormatDescription?
        guard CMVideoFormatDescriptionCreateForImageBuffer(allocator: kCFAllocatorDefault, imageBuffer: image,
                                                           formatDescriptionOut: &format) == noErr, let format else { return }
        var timing = CMSampleTimingInfo(duration: .invalid, presentationTimeStamp: CMClockGetTime(CMClockGetHostTimeClock()),
                                        decodeTimeStamp: .invalid)
        var sample: CMSampleBuffer?
        guard CMSampleBufferCreateReadyWithImageBuffer(allocator: kCFAllocatorDefault, imageBuffer: image,
                                                       formatDescription: format, sampleTiming: &timing,
                                                       sampleBufferOut: &sample) == noErr, let sample else { return }
        if let attachments = CMSampleBufferGetSampleAttachmentsArray(sample, createIfNecessary: true),
           CFArrayGetCount(attachments) > 0 {
            let dict = unsafeBitCast(CFArrayGetValueAtIndex(attachments, 0), to: CFMutableDictionary.self)
            CFDictionarySetValue(dict, Unmanaged.passUnretained(kCMSampleAttachmentKey_DisplayImmediately).toOpaque(),
                                 Unmanaged.passUnretained(kCFBooleanTrue).toOpaque())
        }
        let renderer = displayLayer.sampleBufferRenderer
        if renderer.status == .failed || renderer.requiresFlushToResumeDecoding {
            renderer.flush()
        }
        renderer.enqueue(sample)
    }

    func clear() {
        displayLayer.sampleBufferRenderer.flush(removingDisplayedImage: true, completionHandler: nil)
    }
}

/// CADisplayLink exige um alvo NSObject; este repassa o tique para um bloco.
final class DisplayLinkProxy: NSObject {
    private var link: CADisplayLink?
    private let onTick: () -> Void

    init(fps: Float = 30, onTick: @escaping () -> Void) {
        self.onTick = onTick
        super.init()
        let link = CADisplayLink(target: self, selector: #selector(tick))
        link.preferredFrameRateRange = CAFrameRateRange(minimum: 15, maximum: fps, preferred: fps)
        link.add(to: .main, forMode: .common)
        self.link = link
    }

    @objc private func tick() { onTick() }

    func invalidate() {
        link?.invalidate()
        link = nil
    }
}

// MARK: - Áudio G.711 µ-law

/// Toca o áudio da câmera (G.711 µ-law 8 kHz). Mantém no máximo ~0,5 s na fila para não atrasar.
final class MulawAudioPlayer {
    private let engine = AVAudioEngine()
    private let node = AVAudioPlayerNode()
    private let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 8000, channels: 1, interleaved: false)!
    private var queued = 0
    private(set) var running = false

    static let table: [Float] = (0..<256).map { MulawAudioPlayer.decode(UInt8($0)) }

    static func decode(_ byte: UInt8) -> Float {
        let u = ~byte
        let sign = u & 0x80
        let exponent = Int((u >> 4) & 0x07)
        let mantissa = Int(u & 0x0F)
        let magnitude = (((mantissa << 3) + 0x84) << exponent) - 0x84
        return Float(sign != 0 ? -magnitude : magnitude) / 32768
    }

    init() {
        engine.attach(node)
        engine.connect(node, to: engine.mainMixerNode, format: format)
    }

    func start() {
        guard !running else { return }
        do {
            try AVAudioSession.sharedInstance().setActive(true)
            try engine.start()
            node.play()
            running = true
        } catch {
            running = false
        }
    }

    func stop() {
        guard running else { return }
        node.stop()
        engine.stop()
        running = false
        queued = 0
    }

    func play(_ data: Data) {
        guard running, queued < 12, !data.isEmpty,
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(data.count)),
              let channel = buffer.floatChannelData?[0] else { return }
        buffer.frameLength = AVAudioFrameCount(data.count)
        let table = Self.table
        data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            for i in 0..<raw.count { channel[i] = table[Int(raw[i])] }
        }
        queued += 1
        node.scheduleBuffer(buffer) { [weak self] in
            DispatchQueue.main.async {
                guard let self, self.queued > 0 else { return }
                self.queued -= 1
            }
        }
    }
}
