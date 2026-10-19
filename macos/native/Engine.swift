// Runs one JUA254 session: open the adapter (waiting for HDMI), capture the screen, and send the
// newest JPEG each time the previous send finishes, paced to `fps`. All USB work happens on one
// serial queue. Replaces the Python mirror.py loop inside the native app.

import CoreGraphics
import Foundation

final class Engine {
    enum Mode { case extended, mirror }

    /// Status text for the menu; `active` is true while frames are flowing.
    var onStatus: (String, Bool) -> Void = { _, _ in }
    /// Called once when the session ends on its own (unplug, error); nil reason means a clean stop.
    var onFinished: (String?) -> Void = { _ in }
    var log: (String) -> Void = { _ in }

    let mode: Mode
    let fps: Double
    /// Which adapter (nil: the first one found) and its number in the menu and display name.
    let location: AdapterLocation?
    let displayNumber: Int
    /// The adapter's output mode, and the same size for the virtual display.
    let videoMode: VideoMode
    /// The modes the adapter offers, reported once it is open (for the Resolution menu).
    var onModes: ([VideoMode]) -> Void = { _ in }
    private let usbQueue = DispatchQueue(label: "jua254.usb")
    private let lock = NSLock()
    private var adapter: Adapter?
    private var framer: Framer?
    private var capture: ScreenCapture?
    private var pending: Data?
    private var sending = false
    private var stopped = false
    private var lastStart = Date.distantPast
    private(set) var sent = 0
    private(set) var tooLarge = 0
    // Counters for the stats log, guarded by `lock` (never wait on usbQueue: it can block for seconds).
    private var replaced = 0, usbMs = 0.0, waitMs = 0.0

    struct Stats {
        var delivered = 0, busyDrops = 0, encoded = 0, encodeMs = 0.0, quality = 0.0, reencodes = 0
        var replaced = 0, sent = 0, usbMs = 0.0, waitMs = 0.0, tooLarge = 0
    }

    /// Cumulative counters; the app logs the difference between two snapshots.
    func stats() -> Stats {
        var result = Stats()
        if let c = capture?.counters() {
            result.delivered = c.delivered; result.busyDrops = c.busyDrops; result.encoded = c.encoded; result.encodeMs = c.encodeMs
            result.quality = c.quality; result.reencodes = c.reencodes; result.tooLarge = c.tooLarge
        }
        lock.lock()
        result.replaced = replaced; result.sent = sent; result.usbMs = usbMs; result.waitMs = waitMs; result.tooLarge += tooLarge
        lock.unlock()
        return result
    }
    private var received = 0

    init(mode: Mode, fps: Double = 60, location: AdapterLocation? = nil, displayNumber: Int = 1, videoMode: VideoMode = .standard) {
        self.mode = mode; self.fps = fps; self.location = location; self.displayNumber = displayNumber; self.videoMode = videoMode
    }

    /// Keep capture in step with the display's orientation (see ScreenCapture.refreshOrientation).
    func refreshOrientation(rotation: ScreenCapture.PortraitRotation) {
        capture?.refreshOrientation(rotation: rotation)
    }

    func start() {
        usbQueue.async { self.open() }
    }

    /// Stop capture, disable the adapter's output and release it.
    func stop() {
        lock.lock(); let already = stopped; stopped = true; lock.unlock()
        guard !already else { return }
        capture?.stop()
        usbQueue.sync {
            adapter?.close()
            adapter = nil
        }
    }

    // MARK: - Session

    private func open() {
        onStatus("Opening the adapter…", false)
        do {
            guard let opened = try Adapter.open(waitingForHDMI: 10, at: location, mode: videoMode,
                                                waiting: { self.onStatus("Waiting for the HDMI monitor…", false) }) else {
                finish("JUA254 not found"); return
            }
            adapter = opened
            framer = try Framer(vramMB: opened.vramMB(), mode: videoMode)
            onModes(opened.modes)
        } catch AdapterError.noHDMI {
            finish("No HDMI monitor on the adapter — check the cable"); return
        } catch {
            finish(describe(error)); return
        }
        log("adapter open; capturing \(mode == .extended ? "a virtual display" : "the main display") at \(videoMode), up to \(Int(fps)) frames/s")
        let capture = ScreenCapture(fps: fps, displayNumber: displayNumber, size: videoMode, onJPEG: { [weak self] in self?.enqueue($0) },
                                    onError: { [weak self] in self?.finish($0) })
        capture.onLog = { [weak self] in self?.log($0) }
        self.capture = capture
        capture.start(mode == .extended ? .extended : .mirror(CGMainDisplayID())) { [weak self] display in
            guard let self = self else { return }
            self.log("capture started on display \(display)")
            DispatchQueue.global().asyncAfter(deadline: .now() + 5) { [weak self] in
                guard let self = self, !self.stopped else { return }
                self.log("after 5 s: \(self.received) frames captured, \(self.sent) sent to the adapter")
            }
            self.onStatus(self.mode == .extended ? "Extended desktop on" : "Mirroring", true)
        }
    }

    private func enqueue(_ jpeg: Data) {
        lock.lock()
        received += 1
        if received == 1 { log("first frame captured (\(jpeg.count) bytes)") }
        if pending != nil { replaced += 1 }  // the previous one never got sent
        pending = jpeg  // newest wins
        let start = !sending && !stopped
        if start { sending = true }
        lock.unlock()
        if start { usbQueue.async { self.drain() } }
    }

    private func drain() {
        while true {
            lock.lock()
            guard let jpeg = pending, !stopped else { sending = false; lock.unlock(); return }
            pending = nil
            lock.unlock()
            send(jpeg)
        }
    }

    private func send(_ jpeg: Data) {
        guard let adapter = adapter else { return }
        do {
            let clean = try adapterJPEG(jpeg, width: videoMode.width, height: videoMode.height)
            let wait = lastStart.addingTimeInterval(1 / fps).timeIntervalSinceNow
            if wait > 0 { Thread.sleep(forTimeInterval: wait) }
            lastStart = Date()
            guard var framer = framer else { return }
            let group = try framer.group(jpeg: clean)
            self.framer = framer
            try adapter.send(group)
            let ms = Date().timeIntervalSince(lastStart) * 1000
            lock.lock(); usbMs += ms; waitMs += max(0, wait) * 1000; sent += 1; lock.unlock()
            if sent == 1 { log("first frame sent (\(clean.count) bytes, \(group.count) USB writes)") }
        } catch JPEGCheck.tooLarge {
            lock.lock(); tooLarge += 1; lock.unlock()  // an extremely detailed frame; the next one will do
        } catch JPEGCheck.unsuitable(let reason) {
            finish("macOS produced a JPEG the adapter can't decode (\(reason))")
        } catch {
            finish(describe(error))
        }
    }

    private func finish(_ reason: String?) {
        lock.lock(); let already = stopped; lock.unlock()
        guard !already else { return }
        log("session ended: \(reason ?? "stopped")")
        DispatchQueue.global().async {
            self.stop()
            self.onFinished(reason)
        }
    }

    private func describe(_ error: Error) -> String {
        if let adapterError = error as? AdapterError, case .timeout = adapterError { return "Adapter not responding — unplug and replug it" }
        return "\(error)"
    }
}
