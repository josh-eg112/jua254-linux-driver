// JUA254 Display (native): a self-contained menu-bar app that drives JUA254 USB display adapters on
// macOS. No Python or source folder needed: capture, JPEG, framing and USB (libusb) are all in this
// binary. Each plugged-in adapter gets its own session and virtual display ("JUA254", "JUA254 2"),
// started when it is plugged in and stopped when it is unplugged.
// Log: ~/Library/Logs/JUA254/display.log. Built by macos/native/build.sh.

import AppKit
import ServiceManagement

/// One plugged-in adapter: its session (when running) and its status, retry and stats state.
final class Slot {
    let location: AdapterLocation
    let number: Int
    var engine: Engine?
    var status = "Starting…"
    var symbol = "display"
    var failuresInRow = 0
    var nextAttempt = Date.distantPast
    var lastSent = 0, lastSample = Date()
    var lastStats = Engine.Stats()
    var ticksSinceStats = 0

    init(location: AdapterLocation, number: Int) { self.location = location; self.number = number }
    var label: String { number == 1 ? "JUA254" : "JUA254 \(number)" }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let menu = NSMenu()
    var statusLines: [NSMenuItem] = []
    let startStopItem = NSMenuItem(title: "Start", action: #selector(startStop), keyEquivalent: "")
    let extendItem = NSMenuItem(title: "Extended Desktop", action: #selector(chooseExtend), keyEquivalent: "")
    let mirrorItem = NSMenuItem(title: "Mirror Main Display", action: #selector(chooseMirror), keyEquivalent: "")
    let autoItem = NSMenuItem(title: "Start When Adapter Is Plugged In", action: #selector(toggleAuto), keyEquivalent: "")
    let loginItem = NSMenuItem(title: "Open at Login", action: #selector(toggleLogin), keyEquivalent: "")

    let defaults = UserDefaults.standard
    let logURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/JUA254/display.log")
    var logHandle: FileHandle?
    var slots: [AdapterLocation: Slot] = [:]
    var notice = "Starting…"    // shown when no adapter is attached
    var stoppedByUser = false   // a manual Stop holds until every adapter is unplugged or Start is chosen
    var waitingForPermission = false
    var quitting = false
    var terminationSignal: DispatchSourceSignal?

    var mirrorMode: Bool {
        get { defaults.bool(forKey: "mirror") }
        set { defaults.set(newValue, forKey: "mirror") }
    }
    var autoStart: Bool {
        get { defaults.object(forKey: "autoStart") as? Bool ?? true }
        set { defaults.set(newValue, forKey: "autoStart") }
    }
    var running: [Slot] { slots.values.filter { $0.engine != nil } }
    var ordered: [Slot] { slots.values.sorted { $0.number < $1.number } }

    func number(for location: AdapterLocation) -> Int {
        var known = defaults.dictionary(forKey: "displayNumbers") as? [String: Int] ?? [:]
        let number = displayNumber(for: location.description, known: &known, inUse: Set(slots.values.map { $0.number }))
        defaults.set(known, forKey: "displayNumbers")
        return number
    }

    /// Quitting (menu, logout, or SIGTERM) waits until every adapter's output is disabled and its
    /// interface released: killing the app mid-transfer can hang an adapter until it is replugged.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !running.isEmpty else { return .terminateNow }
        quitting = true
        stopAll { NSApp.reply(toApplicationShouldTerminate: true) }
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
        let attached = Set(Adapter.attached())
        for slot in slots.values where !attached.contains(slot.location) {
            log("\(slot.label) unplugged (port \(slot.location))")
            slots[slot.location] = nil
            stop(slot)
        }
        for location in attached.sorted() where slots[location] == nil {
            let slot = Slot(location: location, number: number(for: location))
            slots[location] = slot
            log("\(slot.label) plugged in (port \(location))")
        }
        if slots.isEmpty {
            stoppedByUser = false
            notice = "Waiting for the adapter"
        }
        let now = Date()
        for slot in ordered {
            if let engine = slot.engine {
                let sent = engine.sent
                let rate = Double(sent - slot.lastSent) / max(0.001, now.timeIntervalSince(slot.lastSample))
                slot.lastSent = sent; slot.lastSample = now
                slot.ticksSinceStats += 1
                if slot.ticksSinceStats >= 5 { slot.ticksSinceStats = 0; logStats(slot, engine) }  // every ~10 s
                if slot.symbol == "display.2" {
                    slot.status = (mirrorMode ? "Mirroring" : "Extended desktop") + String(format: " — %.1f frames/s", rate)
                }
            } else if !stoppedByUser && !waitingForPermission && autoStart && now >= slot.nextAttempt && !quitting {
                start(slot)
            } else if stoppedByUser || !autoStart {
                slot.status = "Connected — stopped"; slot.symbol = "display"
            }
        }
        refreshMenu()
    }

    /// One line per adapter per ~10 s: where frames go and where the time is spent.
    func logStats(_ slot: Slot, _ engine: Engine) {
        let now = engine.stats(), was = slot.lastStats
        slot.lastStats = now
        let seconds = 10.0
        let encoded = now.encoded - was.encoded, sent = now.sent - was.sent
        let avg = { (total: Double, count: Int) in count > 0 ? total / Double(count) : 0 }
        log(slot.label + String(format: " 10 s: macOS gave %d frames (%.1f/s), %d skipped while encoders busy, %d encoded (avg %.1f ms); "
                   + "%d replaced by newer before sending, %d sent (%.1f/s, avg USB %.1f ms, avg pacing wait %.1f ms), %d too large; "
                   + "JPEG quality %.2f, %d re-encoded smaller",
                   now.delivered - was.delivered, Double(now.delivered - was.delivered) / seconds,
                   now.busyDrops - was.busyDrops, encoded, avg(now.encodeMs - was.encodeMs, encoded),
                   now.replaced - was.replaced, sent, Double(sent) / seconds,
                   avg(now.usbMs - was.usbMs, sent), avg(now.waitMs - was.waitMs, sent), now.tooLarge - was.tooLarge,
                   now.quality, now.reencodes - was.reencodes))
    }

    func start(_ slot: Slot) {
        guard slot.engine == nil else { return }
        let engine = Engine(mode: mirrorMode ? .mirror : .extended, location: slot.location, displayNumber: slot.number)
        engine.log = { [weak self, weak slot] text in self?.log("\(slot?.label ?? "JUA254"): \(text)") }
        engine.onStatus = { [weak self, weak slot] text, active in
            DispatchQueue.main.async {
                guard let slot = slot else { return }
                slot.status = text
                slot.symbol = active ? "display.2" : "display"
                if active { slot.failuresInRow = 0 }
                self?.refreshMenu()
            }
        }
        engine.onFinished = { [weak self, weak slot, weak engine] reason in
            DispatchQueue.main.async { if let slot = slot { self?.finished(slot, engine, reason) } }
        }
        slot.engine = engine
        slot.lastSent = 0; slot.lastSample = Date(); slot.lastStats = Engine.Stats(); slot.ticksSinceStats = 0
        log("--- \(slot.label) starting (\(mirrorMode ? "mirror" : "extended desktop"), port \(slot.location)) ---")
        engine.start()
        refreshMenu()
    }

    /// Stop one session on a background queue: closing the adapter can wait for an in-flight transfer.
    func stop(_ slot: Slot, then next: (() -> Void)? = nil) {
        guard let engine = slot.engine else { next?(); return }
        slot.engine = nil
        DispatchQueue.global().async {
            engine.stop()
            DispatchQueue.main.async {
                self.log("--- \(slot.label) stopped ---")
                slot.status = "Connected — stopped"; slot.symbol = "display"
                next?()
                self.refreshMenu()
            }
        }
    }

    func stopAll(then next: @escaping () -> Void) {
        let group = DispatchGroup()
        for slot in running {
            group.enter()
            stop(slot) { group.leave() }
        }
        group.notify(queue: .main, execute: next)
    }

    func finished(_ slot: Slot, _ finishedEngine: Engine?, _ reason: String?) {
        guard let finishedEngine = finishedEngine, finishedEngine === slot.engine else { return }
        slot.engine = nil
        log("--- \(slot.label) stopped: \(reason ?? "ok") ---")
        if quitting { return }  // applicationShouldTerminate's stopAll finishes the quit
        let message = reason ?? "Stopped"
        let attached = slots[slot.location] === slot
        if message.contains("Screen Recording") {
            waitingForPermission = true
            NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")!)
        } else if message.contains("disconnected") {
            // Unplugged (the next tick removes the slot) or re-enumerating: retry soon, backing off if it
            // keeps happening; a stuck adapter needs a replug.
            backOff(slot)
            slot.status = slot.failuresInRow >= 3 ? "Not responding — unplug and replug it" : "Reconnecting…"
            slot.symbol = slot.failuresInRow >= 3 ? "exclamationmark.triangle" : "display"
        } else if message.contains("not responding") || message.contains("LIBUSB_ERROR_IO") {
            // A timed-out or failing adapter usually means its firmware hung: only a replug helps.
            backOff(slot)
            slot.status = slot.failuresInRow >= 2 ? "Not responding — unplug and replug it" : "Reconnecting…"
            slot.symbol = slot.failuresInRow >= 2 ? "exclamationmark.triangle" : "display"
        } else if attached {
            backOff(slot)
            slot.status = message; slot.symbol = "exclamationmark.triangle"
        }
        refreshMenu()
    }

    /// Wait 3, 6, 12, 24, then 30 s between automatic restarts of one adapter.
    func backOff(_ slot: Slot) {
        slot.failuresInRow += 1
        slot.nextAttempt = Date().addingTimeInterval(min(30, 3 * pow(2, Double(slot.failuresInRow - 1))))
    }

    // MARK: - Menu

    @objc func startStop() {
        if !running.isEmpty {
            stoppedByUser = true
            stopAll {}
        } else if !slots.isEmpty {
            stoppedByUser = false
            waitingForPermission = false
            for slot in ordered { slot.failuresInRow = 0; slot.nextAttempt = .distantPast; start(slot) }
        } else {
            notice = "Plug in the adapter first"
        }
        refreshMenu()
    }

    @objc func chooseExtend() { setMode(mirror: false) }
    @objc func chooseMirror() { setMode(mirror: true) }

    func setMode(mirror: Bool) {
        guard mirror != mirrorMode else { return }
        mirrorMode = mirror
        let restart = running
        if !restart.isEmpty { stopAll { for slot in restart where self.slots[slot.location] === slot { self.start(slot) } } }
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
        // Status: one line per adapter (prefixed with its name when there are several), or a notice.
        var lines: [(String, String)] = []
        if waitingForPermission {
            lines.append(("Allow Screen Recording for JUA254 Display, then reopen it", "exclamationmark.triangle"))
        }
        if slots.isEmpty {
            lines.append((notice, "display"))
        } else {
            for slot in ordered { lines.append((slots.count > 1 ? "\(slot.label): \(slot.status)" : slot.status, slot.symbol)) }
        }
        for item in statusLines { menu.removeItem(item) }
        statusLines = lines.enumerated().map { index, line in
            let item = NSMenuItem(title: line.0, action: nil, keyEquivalent: "")
            item.isEnabled = false
            menu.insertItem(item, at: index)
            return item
        }
        let symbols = lines.map { $0.1 }
        let symbol = symbols.contains("exclamationmark.triangle") ? "exclamationmark.triangle"
            : symbols.contains("display.2") ? "display.2" : "display"
        statusItem.button?.image = NSImage(systemSymbolName: symbol, accessibilityDescription: "JUA254 Display")
        statusItem.button?.toolTip = "JUA254 Display — " + lines.map { $0.0 }.joined(separator: "; ")

        startStopItem.title = running.isEmpty ? "Start" : "Stop"
        extendItem.state = mirrorMode ? .off : .on
        mirrorItem.state = mirrorMode ? .on : .off
        autoItem.state = autoStart ? .on : .off
        if #available(macOS 13.0, *) { loginItem.state = SMAppService.mainApp.status == .enabled ? .on : .off }
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
