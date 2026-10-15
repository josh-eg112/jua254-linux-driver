// Screen capture for the JUA254 (macOS only): a 1920x1080 virtual display (private CGVirtualDisplay
// API, declared in CGVirtualDisplay.h) or an existing display, captured with ScreenCaptureKit and
// encoded to JPEG by ImageIO on several threads. JPEGs are handed on in capture order; stale ones
// are dropped. Ported from macos/sck_capture.swift.

import CoreGraphics
import CoreMedia
import CoreVideo
import Foundation
import ImageIO
import ScreenCaptureKit
import VideoToolbox

final class ScreenCapture: NSObject, SCStreamOutput, SCStreamDelegate {
    static let width = 1920, height = 1080

    enum Source { case extended, mirror(CGDirectDisplayID) }

    private let fps: Double
    private let quality: Double
    private let encoders: Int
    private let onJPEG: (Data) -> Void
    private let onError: (String) -> Void
    private var virtualDisplay: CGVirtualDisplay?
    private var scStream: SCStream?
    private let encodeQueue = DispatchQueue(label: "jua254.encode", attributes: .concurrent)
    private let orderQueue = DispatchQueue(label: "jua254.order")
    private let lock = NSLock()
    private var inFlight = 0, sequence = 0, lastDelivered = 0
    private(set) var busyDrops = 0
    /// JPEG quality in use: starts at `quality`, drops when detailed frames exceed the adapter's
    /// upload slot, and creeps back up while frames are comfortably small. Guarded by `lock`.
    private var adaptiveQuality: Double
    private var reencodes = 0
    // Counters for the stats log, guarded by `lock`.
    private var delivered = 0, encoded = 0, encodeMs = 0.0
    func counters() -> (delivered: Int, busyDrops: Int, encoded: Int, encodeMs: Double, quality: Double, reencodes: Int) {
        lock.lock(); defer { lock.unlock() }
        return (delivered, busyDrops, encoded, encodeMs, adaptiveQuality, reencodes)
    }

    static func encode(_ image: CGImage, quality: Double) -> Data? {
        let data = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(data, "public.jpeg" as CFString, 1, nil) else { return nil }
        CGImageDestinationAddImage(destination, image, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
        return CGImageDestinationFinalize(destination) ? data as Data : nil
    }

    /// Encode so the JPEG fits the adapter (JUA254.capacity), lowering quality for detailed frames.
    private func encodeToFit(_ image: CGImage) -> Data? {
        lock.lock(); var q = adaptiveQuality; lock.unlock()
        var jpeg = Self.encode(image, quality: q)
        var retried = false
        while let data = jpeg, data.count > JUA254.capacity, q > 0.15 {
            q = max(0.15, q - 0.15)
            retried = true
            jpeg = Self.encode(image, quality: q)
        }
        lock.lock()
        if retried {
            adaptiveQuality = q; reencodes += 1
        } else if let data = jpeg, data.count < JUA254.capacity * 6 / 10, adaptiveQuality < quality {
            adaptiveQuality = min(quality, adaptiveQuality + 0.01)
        }
        lock.unlock()
        return jpeg
    }

    /// 1 for the first adapter, 2 for the second...: names the virtual display ("JUA254", "JUA254 2")
    /// and gives it its own serial number, so macOS remembers each one's arrangement.
    private let displayNumber: Int

    init(fps: Double = 60, quality: Double = 0.8, encoders: Int = 3, displayNumber: Int = 1,
         onJPEG: @escaping (Data) -> Void, onError: @escaping (String) -> Void) {
        self.displayNumber = displayNumber
        self.fps = fps; self.quality = quality; self.encoders = encoders; self.adaptiveQuality = quality
        self.onJPEG = onJPEG; self.onError = onError
    }

    /// Start capturing. `ready` gets the captured display's ID.
    func start(_ source: Source, ready: @escaping (CGDirectDisplayID) -> Void) {
        switch source {
        case .extended:
            guard let id = createVirtualDisplay() else { onError("macOS refused to create the virtual display"); return }
            capture(id, ready: ready)
        case .mirror(let id):
            capture(id, ready: ready)
        }
    }

    func stop() {
        scStream?.stopCapture(completionHandler: nil)
        scStream = nil
        virtualDisplay = nil  // releasing it removes the display
    }

    private func createVirtualDisplay() -> CGDirectDisplayID? {
        let descriptor = CGVirtualDisplayDescriptor()
        descriptor.queue = DispatchQueue(label: "jua254.virtual-display")
        descriptor.name = displayNumber == 1 ? "JUA254" : "JUA254 \(displayNumber)"
        descriptor.maxPixelsWide = UInt32(Self.width)
        descriptor.maxPixelsHigh = UInt32(Self.height)
        descriptor.sizeInMillimeters = CGSize(width: 527, height: 296)  // a 24" 16:9 panel
        descriptor.vendorID = UInt32(JUA254.vendorID)
        descriptor.productID = UInt32(JUA254.productID)
        descriptor.serialNum = UInt32(displayNumber)
        descriptor.terminationHandler = { [weak self] _, _ in self?.onError("the virtual display was removed") }
        let settings = CGVirtualDisplaySettings()
        settings.hiDPI = 0
        settings.modes = [CGVirtualDisplayMode(width: UInt(Self.width), height: UInt(Self.height), refreshRate: 60)]
        guard let display = CGVirtualDisplay(descriptor: descriptor), display.apply(settings), display.displayID != 0 else { return nil }
        virtualDisplay = display
        return display.displayID
    }

    private func capture(_ wanted: CGDirectDisplayID, attempt: Int = 0, ready: @escaping (CGDirectDisplayID) -> Void) {
        SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: true) { [weak self] content, error in
            guard let self = self else { return }
            guard let content = content else {
                self.onError("Screen Recording permission is needed: \(error?.localizedDescription ?? "no shareable content")")
                return
            }
            guard let display = content.displays.first(where: { $0.displayID == wanted }) else {
                // A new virtual display takes a moment to become shareable.
                if attempt < 50 {
                    DispatchQueue.global().asyncAfter(deadline: .now() + 0.1) { self.capture(wanted, attempt: attempt + 1, ready: ready) }
                } else {
                    self.onError("display \(wanted) not found")
                }
                return
            }
            let configuration = SCStreamConfiguration()
            configuration.width = Self.width
            configuration.height = Self.height
            configuration.pixelFormat = kCVPixelFormatType_32BGRA
            configuration.minimumFrameInterval = CMTime(seconds: 1 / self.fps, preferredTimescale: 600)
            configuration.queueDepth = 8  // frames being encoded still hold their capture buffers
            configuration.showsCursor = true
            configuration.backgroundColor = CGColor.black
            if #available(macOS 14.0, *) { configuration.preservesAspectRatio = true }
            let stream = SCStream(filter: SCContentFilter(display: display, excludingWindows: []), configuration: configuration, delegate: self)
            do {
                try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: DispatchQueue(label: "jua254.frames"))
            } catch {
                self.onError(error.localizedDescription)
                return
            }
            stream.startCapture { error in
                if let error = error { self.onError("Screen Recording permission is needed: \(error.localizedDescription)"); return }
                ready(display.displayID)
            }
            self.scStream = stream
        }
    }

    // MARK: - Frames

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, sampleBuffer.isValid,
              let attachments = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]],
              let rawStatus = attachments.first?[.status] as? Int,
              SCFrameStatus(rawValue: rawStatus) == .complete,
              let pixels = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        lock.lock()
        delivered += 1
        if inFlight >= encoders { busyDrops += 1; lock.unlock(); return }  // a newer frame will follow
        inFlight += 1; sequence += 1
        let order = sequence
        lock.unlock()
        var image: CGImage?
        VTCreateCGImageFromCVPixelBuffer(pixels, options: nil, imageOut: &image)
        encodeQueue.async {
            let started = Date()
            var jpeg: Data?
            if let image = image, image.width == Self.width, image.height == Self.height {
                jpeg = self.encodeToFit(image)
            }
            let ms = Date().timeIntervalSince(started) * 1000
            self.orderQueue.async {
                self.lock.lock()
                self.inFlight -= 1
                if jpeg != nil { self.encoded += 1; self.encodeMs += ms }
                self.lock.unlock()
                guard let jpeg = jpeg, order > self.lastDelivered else { return }  // failed, or a newer frame went out
                self.lastDelivered = order
                self.onJPEG(jpeg)
            }
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        onError(error.localizedDescription)
    }
}
