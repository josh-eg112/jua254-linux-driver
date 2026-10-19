// JUA254 Display (native): a self-contained menu-bar app that drives JUA254 USB display adapters on
// macOS. No Python or source folder needed: capture, JPEG, framing and USB (libusb) are all in this
// binary. Each plugged-in adapter gets its own session and virtual display ("JUA254", "JUA254 2"),
// started when it is plugged in and stopped when it is unplugged. Its number and resolution follow
// the monitor on it (by EDID), so moving a monitor to another adapter keeps its settings.
// Log: ~/Library/Logs/JUA254/display.log. Built by macos/native/build.sh.

import AppKit
import ServiceManagement

/// One plugged-in adapter: its session (when running) and its status, retry and stats state.
final class Slot {
    let location: AdapterLocation
    /// Set from the port when plugged in, then from the monitor each time a session starts.
    var number: Int
    /// What the settings are kept under: the monitor's EDID key, or the port when the monitor has no
    /// readable EDID or has not been read yet.
    var key: String
    /// True once the monitor has been read, so `number` is settled and others must avoid it.
    var identified = false
    /// Reading the monitor's EDID before starting a session.
    var probing = false
    var engine: Engine?
    var status = "Starting…"
    var symbol = "display"
    var failuresInRow = 0
    var nextAttempt = Date.distantPast
    var lastSent = 0, lastSample = Date()
    var lastStats = Engine.Stats()
    var ticksSinceStats = 0
    /// The output mode this session was started with.
    var videoMode = VideoMode.standard

    init(location: AdapterLocation, number: Int) { self.location = location; self.number = number; key = location.description }
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
    let rotateLeftItem = NSMenuItem(title: "Rotate Left", action: #selector(chooseRotateLeft), keyEquivalent: "")
    let rotateRightItem = NSMenuItem(title: "Rotate Right", action: #selector(chooseRotateRight), keyEquivalent: "")
    let resolutionMenu = NSMenu()

    let defaults = UserDefaults.standard
    let logURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/JUA254/display.log")
    var logHandle: FileHandle?
    var slots: [AdapterLocation: Slot] = [:]
    var notice = "Starting…"    // shown when no adapter is attached
    var stoppedByUser = false   // a manual Stop holds until every adapter is unplugged or Start is chosen
    var waitingForPermission = false
    var quitting = false
    var terminationSignal: DispatchSourceSignal?
    /// EDID reads in flight; quitting waits for them so no adapter is left mid-transfer.
    let probes = DispatchGroup()

    var mirrorMode: Bool {
        get { defaults.bool(forKey: "mirror") }
        set { defaults.set(newValue, forKey: "mirror") }
    }
    var autoStart: Bool {
        get { defaults.object(forKey: "autoStart") as? Bool ?? true }
        set { defaults.set(newValue, forKey: "autoStart") }
    }
    /// How a portrait desktop (e.g. 1080x1920, picked in System Settings > Displays) is turned for a
    /// monitor on its side. If the picture is upside down, choose the other one.
    var portraitRotation: ScreenCapture.PortraitRotation {
        get { ScreenCapture.PortraitRotation(rawValue: defaults.string(forKey: "portraitRotation") ?? "") ?? .right }
        set { defaults.set(newValue.rawValue, forKey: "portraitRotation") }
    }
    /// The output mode chosen for each monitor (by Slot.key: EDID key, or port), for monitors that
    /// can't show 1920x1080: "1680x1050@60". Missing means 1920x1080@60.
    var videoModes: [String: String] {
        get { defaults.dictionary(forKey: "videoModes") as? [String: String] ?? [:] }
        set { defaults.set(newValue, forKey: "videoModes") }
    }
    func videoMode(for key: String) -> VideoMode { videoModes[key].flatMap(VideoMode.init) ?? .standard }
    func setVideoMode(_ mode: VideoMode, for key: String) { videoModes[key] = mode == .standard ? nil : mode.description }
    /// Remembered display numbers, by monitor key or port.
    var displayNumbers: [String: Int] {
        get { defaults.dictionary(forKey: "displayNumbers") as? [String: Int] ?? [:] }
        set { defaults.set(newValue, forKey: "displayNumbers") }
    }

    /// Resolutions used to be kept by display number ("resolutions"), so they followed the adapter's
    /// port: give them to those ports, from where the first monitor seen in each takes them over.
    func migrateResolutions() {
        guard let old = defaults.dictionary(forKey: "resolutions") as? [String: String] else { return }
        var modes = videoModes
        for (port, mode) in portModes(fromNumbered: old, numbers: displayNumbers) where modes[port] == nil { modes[port] = mode }
        videoModes = modes
        defaults.removeObject(forKey: "resolutions")
        log("moved resolutions by display number \(old) to ports \(modes)")
    }
    /// The modes each adapter's table offers (by display number), remembered from its last start.
    func offeredModes(for number: Int) -> [VideoMode] {
        (defaults.dictionary(forKey: "offeredModes")?[String(number)] as? [String] ?? []).compactMap(VideoMode.init)
    }
    func rememberOfferedModes(_ modes: [VideoMode], for number: Int) {
        var known = defaults.dictionary(forKey: "offeredModes") ?? [:]
        known[String(number)] = modes.map { $0.description }
        defaults.set(known, forKey: "offeredModes")
    }

    /// Adapters with a session, or about to have one (reading their monitor).
    var running: [Slot] { slots.values.filter { $0.engine != nil || $0.probing } }
    var ordered: [Slot] { slots.values.sorted { $0.number < $1.number } }

    /// A provisional number for a newly plugged-in adapter, from its port (not saved): the monitor
    /// on it decides when its session starts (settle).
    func number(for location: AdapterLocation) -> Int {
        var known = displayNumbers
        return displayNumber(for: location.description, known: &known, inUse: Set(slots.values.map { $0.number }))
    }

    /// Give `slot` the settings key and number of the monitor on it (`monitor` nil: none readable,
    /// so the port's), taking over the port's settings the first time a monitor is seen.
    func settle(_ slot: Slot, monitor: Monitor?) {
        let port = slot.location.description
        let key = monitor?.key ?? port
        var known = displayNumbers, modes = videoModes
        adoptPortSettings(monitor: key, port: port, numbers: &known, modes: &modes)
        let others = slots.values.filter { $0 !== slot && $0.identified }
        let was = slot.label
        slot.number = displayNumber(for: key, known: &known, inUse: Set(others.map { $0.number }), alone: slots.count == 1)
        displayNumbers = known
        videoModes = modes
        if key != slot.key || !slot.identified {
            log("\(was) (port \(port)): " + (monitor.map { "monitor \($0.name ?? "without a name") [\($0.key)]" } ?? "no EDID, settings follow the port")
                + (slot.label != was ? ", now \(slot.label)" : ""))
        }
        slot.key = key
        slot.identified = true
    }

    /// Quitting (menu, logout, or SIGTERM) waits until every adapter's output is disabled and its
    /// interface released: killing the app mid-transfer can hang an adapter until it is replugged.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !running.isEmpty else { return .terminateNow }
        quitting = true
        stopAll { self.probes.notify(queue: .main) { NSApp.reply(toApplicationShouldTerminate: true) } }
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
        migrateResolutions()

        for item in [startStopItem, extendItem, mirrorItem, autoItem, loginItem, rotateLeftItem, rotateRightItem] { item.target = self }
        menu.addItem(.separator())
        menu.addItem(startStopItem)
        menu.addItem(.separator())
        menu.addItem(extendItem)
        menu.addItem(mirrorItem)
        let rotationMenu = NSMenu()
        rotationMenu.addItem(rotateLeftItem)
        rotationMenu.addItem(rotateRightItem)
        let rotationItem = NSMenuItem(title: "Portrait Rotation", action: nil, keyEquivalent: "")
        rotationItem.submenu = rotationMenu
        rotationItem.toolTip = "For a monitor turned on its side: choose its portrait size (e.g. 1080 \u{00D7} 1920) in System Settings > Displays"
        menu.addItem(rotationItem)
        let resolutionItem = NSMenuItem(title: "Resolution", action: nil, keyEquivalent: "")
        resolutionItem.submenu = resolutionMenu
        resolutionItem.toolTip = "The adapter's output resolution: choose what its monitor supports"
        menu.addItem(resolutionItem)
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
                engine.refreshOrientation(rotation: portraitRotation)
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

    /// Read the monitor's EDID (on a background queue: it can wait for HDMI), settle the slot's
    /// number and settings from it, then start the session.
    func start(_ slot: Slot) {
        guard slot.engine == nil, !slot.probing else { return }
        slot.probing = true
        slot.status = "Reading the monitor…"; slot.symbol = "display"
        probes.enter()
        let location = slot.location
        DispatchQueue.global().async {
            var result: Result<Data?, Error>
            do {
                result = .success(try Adapter.readEDID(waitingForHDMI: 10, at: location, waiting: {
                    DispatchQueue.main.async { slot.status = "Waiting for the HDMI monitor…"; self.refreshMenu() }
                }))
            } catch { result = .failure(error) }
            DispatchQueue.main.async {
                defer { self.probes.leave() }
                slot.probing = false
                guard !self.quitting, !self.stoppedByUser, self.slots[slot.location] === slot else {
                    if !self.quitting { slot.status = "Connected — stopped" }
                    self.refreshMenu()
                    return
                }
                switch result {
                case .success(let edid):
                    let monitor = edid.flatMap(Monitor.init(edid:))
                    if monitor == nil { self.log("\(slot.label): no valid EDID (\(edid?.count ?? 0) bytes)") }
                    self.settle(slot, monitor: monitor)
                    self.startSession(slot)
                case .failure(AdapterError.noHDMI):
                    self.failed(slot, "No HDMI monitor on the adapter — check the cable")
                case .failure(let error):
                    if case AdapterError.timeout = error { self.failed(slot, "Adapter not responding — unplug and replug it") }
                    else { self.failed(slot, "\(error)") }
                }
            }
        }
        refreshMenu()
    }

    func startSession(_ slot: Slot) {
        guard slot.engine == nil else { return }
        slot.videoMode = videoMode(for: slot.key)
        let engine = Engine(mode: mirrorMode ? .mirror : .extended, location: slot.location, displayNumber: slot.number,
                            videoMode: slot.videoMode)
        engine.onModes = { [weak self, weak slot] modes in
            DispatchQueue.main.async {
                guard let slot = slot else { return }
                self?.rememberOfferedModes(modes, for: slot.number)
                self?.refreshMenu()
            }
        }
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
        log("--- \(slot.label) starting (\(mirrorMode ? "mirror" : "extended desktop"), \(slot.videoMode), port \(slot.location), \(slot.key)) ---")
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
        failed(slot, reason ?? "Stopped")
    }

    /// A session (or reading its monitor) ended with `message`: retry later, or ask for a replug.
    func failed(_ slot: Slot, _ message: String) {
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

    @objc func chooseRotateLeft() { setRotation(.left) }
    @objc func chooseRotateRight() { setRotation(.right) }

    func setRotation(_ rotation: ScreenCapture.PortraitRotation) {
        portraitRotation = rotation
        for slot in running { slot.engine?.refreshOrientation(rotation: rotation) }
        refreshMenu()
    }

    @objc func chooseResolution(_ sender: NSMenuItem) {
        guard let choice = sender.representedObject as? [Any], let location = choice[0] as? AdapterLocation,
              let mode = (choice[1] as? String).flatMap(VideoMode.init), let slot = slots[location] else { return }
        setVideoMode(mode, for: slot.key)
        log("\(slot.label) resolution set to \(mode) (\(slot.key))")
        // Restart that adapter's session (if it is running) in the new mode.
        if slot.engine != nil, slot.videoMode != mode {
            stop(slot) { if self.slots[slot.location] === slot { slot.failuresInRow = 0; self.start(slot) } }
        } else {
            slot.failuresInRow = 0; slot.nextAttempt = .distantPast  // a failed mode can be retried at once
        }
        refreshMenu()
    }

    /// One group per attached adapter: the modes its table offers (once it has been started), with
    /// the chosen one ticked. Wider modes first.
    func refreshResolutionMenu() {
        resolutionMenu.removeAllItems()
        if slots.isEmpty {
            let item = NSMenuItem(title: "Plug in the adapter first", action: nil, keyEquivalent: "")
            item.isEnabled = false
            resolutionMenu.addItem(item)
            return
        }
        for slot in ordered {
            let chosen = videoMode(for: slot.key)
            var modes = offeredModes(for: slot.number)
            if !modes.contains(chosen) { modes.append(chosen) }
            if !modes.contains(.standard) { modes.append(.standard) }
            modes.sort { ($0.width * $0.height, $0.refresh) > ($1.width * $1.height, $1.refresh) }
            if slots.count > 1 {
                if resolutionMenu.numberOfItems > 0 { resolutionMenu.addItem(.separator()) }
                let header = NSMenuItem(title: slot.label, action: nil, keyEquivalent: "")
                header.isEnabled = false
                resolutionMenu.addItem(header)
            }
            for mode in modes {
                var title = "\(mode.width) \u{00D7} \(mode.height), \(mode.refresh) Hz"
                if mode == .standard { title += " (default)" }
                let item = NSMenuItem(title: title, action: #selector(chooseResolution(_:)), keyEquivalent: "")
                item.target = self
                item.representedObject = [slot.location, mode.description] as [Any]
                item.state = mode == chosen ? .on : .off
                if slots.count > 1 { item.indentationLevel = 1 }
                resolutionMenu.addItem(item)
            }
        }
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

    func menuWillOpen(_ menu: NSMenu) { refreshMenu(); refreshResolutionMenu() }

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
        rotateLeftItem.state = portraitRotation == .left ? .on : .off
        rotateRightItem.state = portraitRotation == .right ? .on : .off
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
