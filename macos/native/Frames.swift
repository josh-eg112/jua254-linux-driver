// Captured BGRA pixel buffers -> CGImages for the JPEG encoder (macOS only), without the two costs
// a profile showed: images are marked opaque (an alpha-carrying image makes ImageIO flatten every
// pixel against a background first), and portrait frames are turned with vImage in memory instead
// of a Core Image render, whose GPU-to-CPU readback is done by the CPU on Intel graphics.

import Accelerate
import CoreGraphics
import CoreVideo

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

    private static func colorSpace(of pixels: CVPixelBuffer) -> CGColorSpace {
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
