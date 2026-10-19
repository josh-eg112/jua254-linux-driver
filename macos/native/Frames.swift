// Captured BGRA pixel buffers -> frames for the JPEG encoder (macOS only). Landscape frames are
// encoded straight from the capture buffer; portrait ones are turned by VideoToolbox on the GPU
// (vImage in memory on macOS 12) instead of a Core Image render, whose GPU-to-CPU readback is done
// by the CPU on Intel graphics.

import Accelerate
import CoreVideo
import VideoToolbox

/// BGRA pixels in memory (the alpha byte unused), ready for the encoder. Whatever holds them (a
/// locked capture buffer, or memory of their own) is released with the frame.
final class Frame {
    let base: UnsafeRawPointer
    let width: Int, height: Int, rowBytes: Int
    private let release: () -> Void

    init(base: UnsafeRawPointer, width: Int, height: Int, rowBytes: Int, release: @escaping () -> Void) {
        self.base = base; self.width = width; self.height = height; self.rowBytes = rowBytes; self.release = release
    }
    deinit { release() }
}

enum Frames {
    /// The pixel buffer's pixels, in place: the buffer stays retained and locked until the frame is released.
    static func frame(_ pixels: CVPixelBuffer) -> Frame? {
        guard CVPixelBufferGetPixelFormatType(pixels) == kCVPixelFormatType_32BGRA,
              CVPixelBufferLockBaseAddress(pixels, .readOnly) == kCVReturnSuccess else { return nil }
        guard let base = CVPixelBufferGetBaseAddress(pixels) else {
            CVPixelBufferUnlockBaseAddress(pixels, .readOnly); return nil
        }
        return Frame(base: base, width: CVPixelBufferGetWidth(pixels), height: CVPixelBufferGetHeight(pixels),
                     rowBytes: CVPixelBufferGetBytesPerRow(pixels)) { CVPixelBufferUnlockBaseAddress(pixels, .readOnly) }
    }

    /// The pixel buffer turned a quarter turn (as it appears on screen), as a new frame.
    static func rotated(_ pixels: CVPixelBuffer, clockwise: Bool) -> Frame? {
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
        guard vImageRotate90_ARGB8888(&source, &turned, rotation, black, vImage_Flags(kvImageNoFlags)) == kvImageNoError else {
            free(out); return nil
        }
        return Frame(base: out, width: height, height: width, rowBytes: height * 4) { free(out) }
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

    func rotated(_ pixels: CVPixelBuffer, clockwise: Bool) -> Frame? {
        if #available(macOS 13.0, *), let turned = gpuRotated(pixels, clockwise: clockwise) {
            lastOnGPU = true
            return Frames.frame(turned)
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
        // the encoder releases the frame made from them.
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
        return turned
    }

    deinit {
        if #available(macOS 13.0, *), let session = session { VTPixelRotationSessionInvalidate(session as! VTPixelRotationSession) }
    }
}
