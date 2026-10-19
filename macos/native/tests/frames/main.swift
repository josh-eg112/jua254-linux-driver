// macOS-only check for Frames.swift and Encoder.swift: a frame keeps every pixel, and the vImage
// rotation turns frames exactly as the Core Image path it replaced (CIImage.oriented(.right / .left)),
// so a monitor set up with Rotate Left or Rotate Right stays the right way up. The GPU Rotator
// (macOS 13+) must match too, including when the rotation is switched while it runs. The
// libjpeg-turbo encoder's JPEGs must pass adapterJPEG at every size the adapters run.

import CoreGraphics
import CoreImage
import CoreVideo
import Foundation
import VideoToolbox

var failures = 0
func check(_ ok: Bool, _ what: String) { if !ok { failures += 1; print("FAIL: \(what)") } }

// 3x2 frame, one distinct colour per pixel (B, G, R, A in memory).
let colours: [[UInt8]] = [[0, 0, 255, 255], [0, 255, 0, 255], [255, 0, 0, 255],
                          [255, 255, 255, 255], [0, 0, 0, 255], [0, 255, 255, 255]]
let width = 3, height = 2
var made: CVPixelBuffer?
CVPixelBufferCreate(nil, width, height, kCVPixelFormatType_32BGRA,  // IOSurface-backed, like captured frames
                    [kCVPixelBufferCGImageCompatibilityKey: true, kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary, &made)
let buffer = made!
CVPixelBufferLockBaseAddress(buffer, [])
let base = CVPixelBufferGetBaseAddress(buffer)!.assumingMemoryBound(to: UInt8.self)
let stride = CVPixelBufferGetBytesPerRow(buffer)
for y in 0..<height { for x in 0..<width { for c in 0..<4 { base[y * stride + x * 4 + c] = colours[y * width + x][c] } } }
CVPixelBufferUnlockBaseAddress(buffer, [])

/// The frame's pixels as colour names (each channel thresholded, so colour management can't matter).
func names(_ frame: Frame) -> [[String]] {
    let info = CGBitmapInfo(rawValue: CGBitmapInfo.byteOrder32Little.rawValue | CGImageAlphaInfo.noneSkipFirst.rawValue)
    let provider = CGDataProvider(dataInfo: nil, data: frame.base, size: frame.rowBytes * frame.height, releaseData: { _, _, _ in })!
    let image = CGImage(width: frame.width, height: frame.height, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: frame.rowBytes,
                        space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: info, provider: provider, decode: nil,
                        shouldInterpolate: false, intent: .defaultIntent)!
    return names(image)
}
func names(_ image: CGImage) -> [[String]] {
    let w = image.width, h = image.height
    var pixels = [UInt8](repeating: 0, count: w * h * 4)
    let context = CGContext(data: &pixels, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                            space: CGColorSpace(name: CGColorSpace.sRGB)!,
                            bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!  // R, G, B, x
    context.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
    return (0..<h).map { y in (0..<w).map { x in
        let p = pixels[(y * w + x) * 4..<(y * w + x) * 4 + 3].map { $0 > 128 ? "1" : "0" }
        return p.joined()
    } }
}

let ci = CIContext(options: [.cacheIntermediates: false])
func viaCoreImage(_ orientation: CGImagePropertyOrientation) -> CGImage {
    let turned = CIImage(cvPixelBuffer: buffer).oriented(orientation)
    return ci.createCGImage(turned, from: turned.extent)!
}

let plain = Frames.frame(buffer)!
check(plain.width == width && plain.height == height, "frame size")
check(names(plain) == names(viaCoreImage(.up)), "the frame keeps every pixel: \(names(plain))")
for (clockwise, orientation) in [(true, CGImagePropertyOrientation.right), (false, .left)] {
    let turned = Frames.rotated(buffer, clockwise: clockwise)!
    check(turned.width == height && turned.height == width, "rotated size")
    check(names(turned) == names(viaCoreImage(orientation)),
          "rotation \(clockwise ? "right" : "left"): \(names(turned)) vs Core Image \(names(viaCoreImage(orientation)))")
}
let rotator = Rotator()
for clockwise in [true, false, true] {  // right, then left, then right again in one session
    let orientation: CGImagePropertyOrientation = clockwise ? .right : .left
    let turned = rotator.rotated(buffer, clockwise: clockwise)!
    if #available(macOS 13.0, *) { check(rotator.lastOnGPU, "macOS 13+ turns frames on the GPU") }
    check(turned.width == height && turned.height == width, "Rotator size")
    check(names(turned) == names(viaCoreImage(orientation)),
          "Rotator \(clockwise ? "right" : "left"): \(names(turned)) vs Core Image \(names(viaCoreImage(orientation)))")
}
// libjpeg-turbo's JPEGs pass adapterJPEG (baseline, 4:2:0, DRI 120, the right size) for every output
// size, portrait frames included, at the qualities encodeToFit uses; a frame too detailed for the
// adapter's slot comes back as tooLarge, and a smaller quality then fits.
func bgraBuffer(_ w: Int, _ h: Int, _ draw: (CGContext) -> Void) -> CVPixelBuffer {
    var made: CVPixelBuffer?
    CVPixelBufferCreate(nil, w, h, kCVPixelFormatType_32BGRA, [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary, &made)
    CVPixelBufferLockBaseAddress(made!, [])
    let context = CGContext(data: CVPixelBufferGetBaseAddress(made!), width: w, height: h, bitsPerComponent: 8,
                            bytesPerRow: CVPixelBufferGetBytesPerRow(made!), space: CGColorSpace(name: CGColorSpace.sRGB)!,
                            bitmapInfo: CGBitmapInfo.byteOrder32Little.rawValue | CGImageAlphaInfo.noneSkipFirst.rawValue)!
    draw(context)
    CVPixelBufferUnlockBaseAddress(made!, [])
    return made!
}
func desktop(_ c: CGContext) {  // a gradient with coloured blocks: an ordinary desktop's worth of detail
    let w = CGFloat(c.width), h = CGFloat(c.height)
    for i in 0..<64 {
        c.setFillColor(red: CGFloat(i % 8) / 8, green: CGFloat(i / 8) / 8, blue: 0.5, alpha: 1)
        c.fill(CGRect(x: CGFloat(i % 8) * w / 8, y: CGFloat(i / 8) * h / 8, width: w / 8 - 4, height: h / 8 - 4))
    }
}
var seed: UInt64 = 1
func noise(_ c: CGContext) {  // grey with faint random speckle: too detailed to fit at high quality
    let p = c.data!.assumingMemoryBound(to: UInt8.self)
    for i in 0..<c.bytesPerRow * c.height { seed = seed &* 6364136223846793005 &+ 1442695040888963407; p[i] = 112 + UInt8(truncatingIfNeeded: seed >> 59) }
}
let encoder = JPEGEncoder()!
for mode in [VideoMode.standard, VideoMode("1680x1050@60")!, VideoMode("1280x720@60")!] {
    let landscape = Frames.frame(bgraBuffer(mode.width, mode.height, desktop))!
    let portrait = Frames.rotated(bgraBuffer(mode.height, mode.width, desktop), clockwise: true)!
    for (frame, kind) in [(landscape, "landscape"), (portrait, "portrait")] {
        for quality in [0.8, 0.65, 0.45, 0.15] {
            guard case .jpeg(let jpeg) = encoder.encode(frame, quality: quality) else {
                check(false, "\(mode) \(kind) q\(quality): no JPEG"); continue
            }
            do {
                _ = try adapterJPEG(jpeg, allowRestart: true, width: mode.width, height: mode.height)
                check(jpeg.contains(Data([0xFF, 0xDD, 0x00, 0x04, 0x00, 120])), "\(mode) \(kind) q\(quality): restart interval 120")
            } catch {
                check(false, "\(mode) \(kind) q\(quality): adapterJPEG rejects turbo's JPEG: \(error)")
            }
        }
    }
}
let detailed = Frames.frame(bgraBuffer(1920, 1080, noise))!
if case .tooLarge = encoder.encode(detailed, quality: 0.8) {} else { check(false, "a noise frame at q0.8 is too large") }
if case .jpeg(let small) = encoder.encode(detailed, quality: 0.15) {
    check((try? adapterJPEG(small)) != nil, "the noise frame fits at q0.15 and passes adapterJPEG")
} else { check(false, "a noise frame fits at q0.15") }
// The encoder still works after stopping early.
if case .jpeg = encoder.encode(plain, quality: 0.8) {} else { check(false, "encodes again after a frame was too large") }
check(JPEGEncoder.turboQuality(0.80) == 93 && JPEGEncoder.turboQuality(0.65) == 88 && JPEGEncoder.turboQuality(0.45) == 70,
      "ImageIO 0.80/0.65/0.45 map to libjpeg Q93/88/70")

if failures > 0 { print("frames: \(failures) FAILED"); exit(1) }
print("frames: frames and both rotations (vImage and GPU) match Core Image; libjpeg-turbo's JPEGs pass adapterJPEG")
