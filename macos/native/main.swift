// JUA254 Display (native): a self-contained menu-bar app that drives the JUA254 USB display
// adapter on macOS. No Python or source folder needed: capture, JPEG, framing and USB (libusb) are
// all in this binary. It starts when the adapter is plugged in and stops when it is unplugged.
// Log: ~/Library/Logs/JUA254/display.log. Built by macos/native/build.sh.

import AppKit
import IOKit
import ServiceManagement

/// True while a JUA254 is attached, from the IORegistry (no device access needed).
func adapterPresent() -> Bool {
    let matching = IOServiceMatching("IOUSBHostDevice") as NSMutableDictionary
    matching["idVendor"] = Int(JUA254.vendorID)
    matching["idProduct"] = Int(JUA254.productID)
    var iterator: io_iterator_t = 0
    guard IOServiceGetMatchingServices(kIOMainPortDefault, matching as CFDictionary, &iterator) == KERN_SUCCESS else { return false }
    defer { IOObjectRelease(iterator) }
    let service = IOIteratorNext(iterator)
    guard service != 0 else { return false }
    IOObjectRelease(service)
    return true
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let menu = NSMenu()
    let statusLine = NSMenuItem(title: "Starting…", action: nil, keyEquivalent: "")
    let startStopItem = NSMenuItem(title: "Start", action: #selector(startStop), keyEquivalent: "")
    let extendItem = NSMenuItem(title: "Extended Desktop", action: #selector(chooseExtend), keyEquivalent: "")
    let mirrorItem = NSMenuItem(title: "Mirror Main Display", action: #selector(chooseMirror), keyEquivalent: "")
    let autoItem = NSMenuItem(title: "Start When Adapter Is Plugged In", action: #selector(toggleAuto), keyEquivalent: "")
    let loginItem = NSMenuItem(title: "Open at Login", action: #selector(toggleLogin), keyEquivalent: "")

    let defaults = UserDefaults.standard
    let logURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/JUA254/display.log")
    var logHandle: FileHandle?
    var engine: Engine?
    var stoppedByUser = false   // a manual Stop holds until the adapter is unplugged or Start is chosen
    var waitingForPermission = false
    var failuresInRow = 0
    var nextAttempt = Date.distantPast
    var lastSent = 0, lastSample = Date()
    var ticksSinceRateLog = 0
    var lastStats = Engine.Stats()
    var quitting = false

    var mirrorMode: Bool {
        get { defaults.bool(forKey: "mirror") }
        set { defaults.set(newValue, forKey: "mirror") }
    }
    var autoStart: Bool {
        get { defaults.object(forKey: "autoStart") as? Bool ?? true }
        set { defaults.set(newValue, forKey: "autoStart") }
    }

    var terminationSignal: DispatchSourceSignal?

    /// Quitting (menu, logout, or SIGTERM) waits until the adapter's output is disabled and the
    /// interface released: killing the app mid-transfer can hang the adapter until it is replugged.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard engine != nil else { return .terminateNow }
        quitting = true
        stopEngine()
        return .terminateLater
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        signal(SIGTERM, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        source.setEventHandler { NSApp.terminate(nil) }
        source.resume()
        terminationSignal = source

        try? FileManager.default.createDirectory(at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: logURL.path) { FileManager.default.createFile(atPath: logURL.path, contents: nil) }
        logHandle = try? FileHandle(forWritingTo: logURL)
        logHandle?.seekToEndOfFile()
        log("JUA254 Display \(Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?") started")

        for item in [startStopItem, extendItem, mirrorItem, autoItem, loginItem] { item.target = self }
        statusLine.isEnabled = false
        menu.addItem(statusLine)
        menu.addItem(.separator())
        menu.addItem(startStopItem)
        menu.addItem(.separator())
        menu.addItem(extendItem)
        menu.addItem(mirrorItem)
        menu.addItem(.separator())
        menu.addItem(autoItem)
        if #available(macOS 13.0, *) { menu.addItem(loginItem) }
        menu.addItem(.separator())
        let logItem = NSMenuItem(title: "Show Log", action: #selector(showLog), keyEquivalent: "")
        logItem.target = self
        menu.addItem(logItem)
        let quitItem = NSMenuItem(title: "Quit JUA254 Display", action: #selector(quit), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)
        menu.delegate = self
        statusItem.menu = menu

        Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.tick() }
        tick()
    }

    // MARK: - Lifecycle

    func tick() {
        let present = adapterPresent()
        if !present {
            stoppedByUser = false
            failuresInRow = 0
            nextAttempt = .distantPast
            if engine != nil { stopEngine() } else if !waitingForPermission { setStatus("Waiting for the adapter", symbol: "display") }
        } else if engine == nil && !stoppedByUser && !waitingForPermission && autoStart && Date() >= nextAttempt && !quitting {
            startEngine()
        } else if let engine = engine {
            let now = Date(), sent = engine.sent
            let rate = Double(sent - lastSent) / now.timeIntervalSince(lastSample)
            lastSent = sent; lastSample = now
            ticksSinceRateLog += 1
            if ticksSinceRateLog >= 5 {  // every ~10 s
                ticksSinceRateLog = 0
                logStats(engine)
            }
            if statusLine.title.hasPrefix("Extended") || statusLine.title.hasPrefix("Mirroring") {
                setStatus((mirrorMode ? "Mirroring" : "Extended desktop") + String(format: " — %.1f frames/s", rate), symbol: "display.2")
            }
        } else if stoppedByUser || !autoStart {
            setStatus("Adapter connected — stopped", symbol: "display")
        }
        refreshMenu()
    }

    /// One line per ~10 s: where frames go and where the time is spent, since the last line.
    func logStats(_ engine: Engine) {
        let now = engine.stats(), was = lastStats
        lastStats = now
        let seconds = 10.0
        let encoded = now.encoded - was.encoded, sent = now.sent - was.sent
        let avg = { (total: Double, count: Int) in count > 0 ? total / Double(count) : 0 }
        log(String(format: "10 s: macOS gave %d frames (%.1f/s), %d skipped while encoders busy, %d encoded (avg %.1f ms); "
                   + "%d replaced by newer before sending, %d sent (%.1f/s, avg USB %.1f ms, avg pacing wait %.1f ms), %d too large; "
                   + "JPEG quality %.2f, %d re-encoded smaller",
                   now.delivered - was.delivered, Double(now.delivered - was.delivered) / seconds,
                   now.busyDrops - was.busyDrops, encoded, avg(now.encodeMs - was.encodeMs, encoded),
                   now.replaced - was.replaced, sent, Double(sent) / seconds,
                   avg(now.usbMs - was.usbMs, sent), avg(now.waitMs - was.waitMs, sent), now.tooLarge - was.tooLarge,
                   now.quality, now.reencodes - was.reencodes))
    }

    func startEngine() {
        guard engine == nil else { return }
        let engine = Engine(mode: mirrorMode ? .mirror : .extended)
        engine.log = { [weak self] text in self?.log(text) }
        engine.onStatus = { [weak self] text, active in
            DispatchQueue.main.async {
                self?.setStatus(text, symbol: active ? "display.2" : "display")
                if active { self?.failuresInRow = 0 }
            }
        }
        engine.onFinished = { [weak self, weak engine] reason in
            DispatchQueue.main.async { self?.finished(engine, reason) }
        }
        self.engine = engine
        lastSent = 0; lastSample = Date(); lastStats = Engine.Stats(); ticksSinceRateLog = 0
        log("--- starting (\(mirrorMode ? "mirror" : "extended desktop")) ---")
        engine.start()
        refreshMenu()
    }

    /// Stop on a background queue: closing the adapter can wait for an in-flight USB transfer.
    func stopEngine(then next: (() -> Void)? = nil) {
        guard let engine = engine else { next?(); return }
        self.engine = nil
        DispatchQueue.global().async {
            engine.stop()
            DispatchQueue.main.async {
                self.log("--- stopped ---")
                if self.quitting { NSApp.reply(toApplicationShouldTerminate: true); return }
                self.setStatus(adapterPresent() ? "Adapter connected — stopped" : "Waiting for the adapter", symbol: "display")
                next?()
                self.refreshMenu()
            }
        }
    }

    func finished(_ finishedEngine: Engine?, _ reason: String?) {
        guard let finishedEngine = finishedEngine, finishedEngine === engine else { return }
        engine = nil
        log("--- stopped: \(reason ?? "ok") ---")
        if quitting { return }  // applicationShouldTerminate's stopEngine finishes the quit
        let message = reason ?? "Stopped"
        if message.contains("disconnected") {
            // Unplugged (the next tick shows "Waiting for the adapter") or re-enumerating: retry soon,
            // backing off if it keeps happening; a stuck adapter needs a replug.
            backOff()
            if failuresInRow >= 3 {
                setStatus("Adapter not responding — unplug and replug it", symbol: "exclamationmark.triangle")
            } else {
                setStatus("Reconnecting…", symbol: "display")
            }
        } else if message.contains("Screen Recording") {
            waitingForPermission = true
            setStatus("Allow Screen Recording for JUA254 Display, then reopen it", symbol: "exclamationmark.triangle")
            NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")!)
        } else if message.contains("not responding") || message.contains("LIBUSB_ERROR_IO") {
            // A timed-out or failing adapter usually means its firmware hung: only a replug helps.
            backOff()
            setStatus(failuresInRow >= 2 ? "Adapter not responding — unplug and replug it" : "Reconnecting…",
                      symbol: failuresInRow >= 2 ? "exclamationmark.triangle" : "display")
        } else if message.contains("HDMI") {
            setStatus(message, symbol: "exclamationmark.triangle")
            backOff()
        } else if adapterPresent() {
            setStatus(message, symbol: "exclamationmark.triangle")
            backOff()
        }
        refreshMenu()
    }

    /// Wait 3, 6, 12, 24, then 30 s between automatic restarts.
    func backOff() {
        failuresInRow += 1
        nextAttempt = Date().addingTimeInterval(min(30, 3 * pow(2, Double(failuresInRow - 1))))
    }

    // MARK: - Menu

    @objc func startStop() {
        if engine != nil {
            stoppedByUser = true
            stopEngine()
        } else if adapterPresent() {
            stoppedByUser = false
            waitingForPermission = false
            failuresInRow = 0
            startEngine()
        } else {
            setStatus("Plug in the adapter first", symbol: "display")
        }
        refreshMenu()
    }

    @objc func chooseExtend() { setMode(mirror: false) }
    @objc func chooseMirror() { setMode(mirror: true) }

    func setMode(mirror: Bool) {
        guard mirror != mirrorMode else { return }
        mirrorMode = mirror
        if engine != nil { stopEngine { self.startEngine() } }
        refreshMenu()
    }

    @objc func toggleAuto() {
        autoStart.toggle()
        refreshMenu()
    }

    @objc func toggleLogin() {
        guard #available(macOS 13.0, *) else { return }
        do {
            if SMAppService.mainApp.status == .enabled { try SMAppService.mainApp.unregister() }
            else { try SMAppService.mainApp.register() }
        } catch {
            let alert = NSAlert()
            alert.messageText = "Could not change Open at Login"
            alert.informativeText = error.localizedDescription
            alert.runModal()
        }
        refreshMenu()
    }

    @objc func showLog() { NSWorkspace.shared.open(logURL) }

    @objc func quit() { NSApp.terminate(nil) }

    func menuWillOpen(_ menu: NSMenu) { refreshMenu() }

    func refreshMenu() {
        startStopItem.title = engine != nil ? "Stop" : "Start"
        extendItem.state = mirrorMode ? .off : .on
        mirrorItem.state = mirrorMode ? .on : .off
        autoItem.state = autoStart ? .on : .off
        if #available(macOS 13.0, *) { loginItem.state = SMAppService.mainApp.status == .enabled ? .on : .off }
    }

    func setStatus(_ text: String, symbol: String) {
        statusLine.title = text
        statusItem.button?.image = NSImage(systemSymbolName: symbol, accessibilityDescription: "JUA254 Display")
        statusItem.button?.toolTip = "JUA254 Display — \(text)"
    }

    func log(_ text: String) {
        let line = "\(ISO8601DateFormatter().string(from: Date())) \(text)\n"
        DispatchQueue.main.async { self.logHandle?.write(line.data(using: .utf8)!) }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
