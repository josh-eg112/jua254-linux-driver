// Captured BGRA pixel buffers -> CGImages for the JPEG encoder (macOS only), without the two costs
// a profile showed: images are marked opaque (an alpha-carrying image makes ImageIO flatten every
// pixel against a background first), and portrait frames are turned by VideoToolbox on the GPU
// (vImage in memory on macOS 12) instead of a Core Image render, whose GPU-to-CPU readback is done
// by the CPU on Intel graphics.

import Accelerate
import CoreGraphics
import CoreVideo
import VideoToolbox

enum Frames {
    /// The pixel buffer as an opaque image, sharing its memory: the buffer stays retained and locked
    /// until the image is released.
    static func image(_ pixels: CVPixelBuffer) -> CGImage? {
        guard CVPixelBufferGetPixelFormatType(pixels) == kCVPixelFormatType_32BGRA,
              CVPixelBufferLockBaseAddress(pixels, .readOnly) == kCVReturnSuccess else { return nil }
        let width = CVPixelBufferGetWidth(pixels), height = CVPixelBufferGetHeight(pixels)
        let rowBytes = CVPixelBufferGetBytesPerRow(pixels)
        guard let base = CVPixelBufferGetBaseAddress(pixels) else {
            CVPixelBufferUnlockBaseAddress(pixels, .readOnly); return nil
        }
        let retained = Unmanaged.passRetained(pixels)
        guard let provider = CGDataProvider(dataInfo: retained.toOpaque(), data: base, size: rowBytes * height,
                                            releaseData: { info, _, _ in
            let buffer = Unmanaged<CVPixelBuffer>.fromOpaque(info!).takeRetainedValue()
            CVPixelBufferUnlockBaseAddress(buffer, .readOnly)
        }) else {
            CVPixelBufferUnlockBaseAddress(pixels, .readOnly); retained.release(); return nil
        }
        return make(provider, width: width, height: height, rowBytes: rowBytes, space: colorSpace(of: pixels))
    }

    /// The pixel buffer turned a quarter turn (as it appears on screen), as a new opaque image.
    static func rotated(_ pixels: CVPixelBuffer, clockwise: Bool) -> CGImage? {
        guard CVPixelBufferGetPixelFormatType(pixels) == kCVPixelFormatType_32BGRA,
              CVPixelBufferLockBaseAddress(pixels, .readOnly) == kCVReturnSuccess else { return nil }
        defer { CVPixelBufferUnlockBaseAddress(pixels, .readOnly) }
        let width = CVPixelBufferGetWidth(pixels), height = CVPixelBufferGetHeight(pixels)
        guard let base = CVPixelBufferGetBaseAddress(pixels),
              let out = malloc(width * height * 4) else { return nil }
        var source = vImage_Buffer(data: base, height: vImagePixelCount(height), width: vImagePixelCount(width),
                                   rowBytes: CVPixelBufferGetBytesPerRow(pixels))
        var turned = vImage_Buffer(data: out, height: vImagePixelCount(width), width: vImagePixelCount(height),
                                   rowBytes: height * 4)
        let rotation = UInt8(clockwise ? kRotate90DegreesClockwise : kRotate90DegreesCounterClockwise)
        let black: [UInt8] = [0, 0, 0, 0]
        guard vImageRotate90_ARGB8888(&source, &turned, rotation, black, vImage_Flags(kvImageNoFlags)) == kvImageNoError,
              let provider = CGDataProvider(dataInfo: nil, data: out, size: width * height * 4,
                                            releaseData: { _, data, _ in free(UnsafeMutableRawPointer(mutating: data)) }) else {
            free(out); return nil
        }
        return make(provider, width: height, height: width, rowBytes: height * 4, space: colorSpace(of: pixels))
    }

    fileprivate static func colorSpace(of pixels: CVPixelBuffer) -> CGColorSpace {
        if let attachments = CVBufferCopyAttachments(pixels, .shouldPropagate),
           let space = CVImageBufferCreateColorSpaceFromAttachments(attachments)?.takeRetainedValue() {
            return space
        }
        return CGColorSpace(name: CGColorSpace.sRGB)!
    }

    private static func make(_ provider: CGDataProvider, width: Int, height: Int, rowBytes: Int, space: CGColorSpace) -> CGImage? {
        // BGRA in memory = 32-bit little-endian xRGB; the unused alpha byte is skipped.
        let info = CGBitmapInfo(rawValue: CGBitmapInfo.byteOrder32Little.rawValue | CGImageAlphaInfo.noneSkipFirst.rawValue)
        return CGImage(width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: rowBytes,
                       space: space, bitmapInfo: info, provider: provider, decode: nil, shouldInterpolate: false,
                       intent: .defaultIntent)
    }
}

/// Turns portrait frames a quarter turn with VTPixelRotationSession (macOS 13+), which does the work on
/// the GPU: about 1 ms of CPU per 1080x1920 frame against ~12 ms for vImage, with identical pixels.
/// Falls back to `Frames.rotated` on macOS 12 or if VideoToolbox fails. Use from one thread at a time.
final class Rotator {
    private var session: AnyObject?  // VTPixelRotationSession
    private var pool: CVPixelBufferPool?
    private var poolSize = (width: 0, height: 0)
    private var clockwise: Bool?
    /// Whether the last frame was turned on the GPU (false: the vImage fallback).
    private(set) var lastOnGPU = false

    func rotated(_ pixels: CVPixelBuffer, clockwise: Bool) -> CGImage? {
        if #available(macOS 13.0, *), let turned = gpuRotated(pixels, clockwise: clockwise) {
            lastOnGPU = true
            return Frames.image(turned)
        }
        lastOnGPU = false
        return Frames.rotated(pixels, clockwise: clockwise)
    }

    @available(macOS 13.0, *)
    private func gpuRotated(_ pixels: CVPixelBuffer, clockwise: Bool) -> CVPixelBuffer? {
        if session == nil {
            var created: VTPixelRotationSession?
            guard VTPixelRotationSessionCreate(nil, &created) == noErr, let created = created else { return nil }
            session = created
        }
        let session = self.session as! VTPixelRotationSession
        if self.clockwise != clockwise {
            let angle = clockwise ? kVTRotation_CW90 : kVTRotation_CCW90
            guard VTSessionSetProperty(session, key: kVTPixelRotationPropertyKey_Rotation, value: angle) == noErr else { return nil }
            self.clockwise = clockwise
        }
        // The turned frame swaps width and height; buffers come from a pool and go back to it once
        // the encoder releases the image made from them.
        let width = CVPixelBufferGetHeight(pixels), height = CVPixelBufferGetWidth(pixels)
        if pool == nil || poolSize != (width, height) {
            let attributes: [String: Any] = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
                                             kCVPixelBufferWidthKey as String: width, kCVPixelBufferHeightKey as String: height,
                                             kCVPixelBufferIOSurfacePropertiesKey as String: [:]]
            pool = nil
            guard CVPixelBufferPoolCreate(nil, nil, attributes as CFDictionary, &pool) == kCVReturnSuccess else { return nil }
            poolSize = (width, height)
        }
        var turned: CVPixelBuffer?
        guard let pool = pool, CVPixelBufferPoolCreatePixelBuffer(nil, pool, &turned) == kCVReturnSuccess,
              let turned = turned,
              VTPixelRotationSessionRotateImage(session, pixels, turned) == noErr else { return nil }
        CVBufferPropagateAttachments(pixels, turned)  // keeps the colour space Frames.image reads
        return turned
    }

    deinit {
        if #available(macOS 13.0, *), let session = session { VTPixelRotationSessionInvalidate(session as! VTPixelRotationSession) }
    }
}
