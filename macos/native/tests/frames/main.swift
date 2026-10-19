// macOS-only check for Frames.swift: the opaque image keeps every pixel, and the vImage rotation
// turns frames exactly as the Core Image path it replaced (CIImage.oriented(.right / .left)), so a
// monitor set up with Rotate Left or Rotate Right stays the right way up.

import CoreGraphics
import CoreImage
import CoreVideo
import Foundation
import ImageIO

var failures = 0
func check(_ ok: Bool, _ what: String) { if !ok { failures += 1; print("FAIL: \(what)") } }

// 3x2 frame, one distinct colour per pixel (B, G, R, A in memory).
let colours: [[UInt8]] = [[0, 0, 255, 255], [0, 255, 0, 255], [255, 0, 0, 255],
                          [255, 255, 255, 255], [0, 0, 0, 255], [0, 255, 255, 255]]
let width = 3, height = 2
var made: CVPixelBuffer?
CVPixelBufferCreate(nil, width, height, kCVPixelFormatType_32BGRA,
                    [kCVPixelBufferCGImageCompatibilityKey: true] as CFDictionary, &made)
let buffer = made!
CVPixelBufferLockBaseAddress(buffer, [])
let base = CVPixelBufferGetBaseAddress(buffer)!.assumingMemoryBound(to: UInt8.self)
let stride = CVPixelBufferGetBytesPerRow(buffer)
for y in 0..<height { for x in 0..<width { for c in 0..<4 { base[y * stride + x * 4 + c] = colours[y * width + x][c] } } }
CVPixelBufferUnlockBaseAddress(buffer, [])

/// The image's pixels as colour names (each channel thresholded, so colour management can't matter).
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

let plain = Frames.image(buffer)!
check(plain.alphaInfo == .noneSkipFirst, "the frame image is opaque")
check(names(plain) == names(viaCoreImage(.up)), "the opaque image keeps every pixel: \(names(plain))")
for (clockwise, orientation) in [(true, CGImagePropertyOrientation.right), (false, .left)] {
    let turned = Frames.rotated(buffer, clockwise: clockwise)!
    check(turned.width == height && turned.height == width && turned.alphaInfo == .noneSkipFirst, "rotated size and opacity")
    check(names(turned) == names(viaCoreImage(orientation)),
          "rotation \(clockwise ? "right" : "left"): \(names(turned)) vs Core Image \(names(viaCoreImage(orientation)))")
}
// The JPEG encoder accepts the opaque images.
let jpeg = NSMutableData()
let destination = CGImageDestinationCreateWithData(jpeg, "public.jpeg" as CFString, 1, nil)!
CGImageDestinationAddImage(destination, Frames.rotated(buffer, clockwise: false)!, [kCGImageDestinationLossyCompressionQuality: 0.8] as CFDictionary)
check(CGImageDestinationFinalize(destination) && jpeg.length > 0, "an opaque rotated frame encodes to JPEG")

if failures > 0 { print("frames: \(failures) FAILED"); exit(1) }
print("frames: opaque image and both rotations match Core Image")
