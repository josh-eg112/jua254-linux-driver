// JPEG encoding with libjpeg-turbo (CTurbo/turbo.c), straight from a frame's BGRA pixels: baseline,
// 4:2:0, a restart marker every 120 MCUs (as ImageIO wrote them), into a buffer the encoder owns.
// On an Intel Mac this takes about half the CPU of ImageIO, whose AppleJPEG encoder and BGRA
// permute were most of the app's CPU time. Not thread-safe: each encoding thread needs its own.

import CTurbo
import Foundation

final class JPEGEncoder {
    enum Result { case jpeg(Data), tooLarge, failed }

    private let encoder: OpaquePointer
    /// The largest JPEG it will make; a bigger one stops early as `.tooLarge`.
    let capacity: Int

    init?(capacity: Int = JUA254.capacity) {
        guard let encoder = turbo_encoder_new(capacity) else { return nil }
        self.encoder = encoder; self.capacity = capacity
    }
    deinit { turbo_encoder_free(encoder) }

    /// `quality` is on ImageIO's 0-1 scale, which the rest of the app (and its log) uses.
    func encode(_ frame: Frame, quality: Double) -> Result {
        let size = turbo_encode(encoder, frame.base.assumingMemoryBound(to: UInt8.self), frame.rowBytes,
                                Int32(frame.width), Int32(frame.height), Int32(Self.turboQuality(quality)), 120)
        if size == TURBO_TOO_LARGE { return .tooLarge }
        guard size > 0 else { return .failed }
        return .jpeg(Data(bytes: turbo_output(encoder), count: size))
    }

    /// ImageIO quality -> libjpeg quality giving about the same JPEG size, measured on desktop, text
    /// and moving 1920x1080 frames (the same Q for all three, sizes within 1-2%; at 1.0 ImageIO is
    /// larger still), and linear in between. ImageIO's scale bottoms out below 0.05. The two scales
    /// differ because Apple's quantisation tables aren't libjpeg's.
    static let qualityScale: [(imageIO: Double, turbo: Double)] =
        [(0.0, 21), (0.15, 23), (0.30, 40), (0.45, 70), (0.65, 88), (0.80, 93), (0.90, 95), (0.95, 96), (1.0, 100)]

    static func turboQuality(_ quality: Double) -> Int {
        let q = min(1, max(0, quality))
        let upper = qualityScale.firstIndex { $0.imageIO >= q } ?? qualityScale.count - 1
        guard upper > 0 else { return Int(qualityScale[0].turbo) }
        let a = qualityScale[upper - 1], b = qualityScale[upper]
        return Int((a.turbo + (q - a.imageIO) / (b.imageIO - a.imageIO) * (b.turbo - a.turbo)).rounded())
    }
}
