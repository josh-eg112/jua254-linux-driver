// The JUA254 over libusb: open/identity/HDMI/mode checks, mode set, VRAM query, frame sends and
// release. A port of open_device(), query_vram_mb(), write_group_timed() and release_device() in
// linux/jua254_stream.py, sending only the requests the Linux driver sends. Builds on Linux too.

import CLibUSB
import Foundation

/// Where an adapter is plugged in: USB bus and port path, e.g. "20-3" or "20-1.2". Stable while it
/// stays in that port, so two adapters keep their own sessions and virtual displays.
struct AdapterLocation: Hashable, Comparable, CustomStringConvertible {
    let bus: UInt8
    let ports: [UInt8]
    var description: String { "\(bus)-" + ports.map(String.init).joined(separator: ".") }
    static func < (a: AdapterLocation, b: AdapterLocation) -> Bool {
        a.bus != b.bus ? a.bus < b.bus : a.ports.lexicographicallyPrecedes(b.ports)
    }
}

final class Adapter {
    /// Every attached JUA254, by location, without opening any of them.
    static func attached() -> [AdapterLocation] {
        var context: OpaquePointer?
        guard libusb_init(&context) == 0, let context = context else { return [] }
        defer { libusb_exit(context) }
        return withAdapters(context) { $0.map { $0.1 } }.sorted()
    }

    /// Calls `body` with each attached JUA254 device and its location; the list is freed afterwards.
    private static func withAdapters<T>(_ context: OpaquePointer, _ body: ([(OpaquePointer, AdapterLocation)]) throws -> T) rethrows -> T {
        var list: UnsafeMutablePointer<OpaquePointer?>?
        let count = libusb_get_device_list(context, &list)
        guard count > 0, let devices = list else { return try body([]) }
        defer { libusb_free_device_list(devices, 1) }
        var found: [(OpaquePointer, AdapterLocation)] = []
        for i in 0..<Int(count) {
            guard let device = devices[i] else { continue }
            var descriptor = libusb_device_descriptor()
            guard libusb_get_device_descriptor(device, &descriptor) == 0,
                  descriptor.idVendor == JUA254.vendorID, JUA254.models[descriptor.idProduct] != nil else { continue }
            var ports = [UInt8](repeating: 0, count: 8)
            let depth = libusb_get_port_numbers(device, &ports, Int32(ports.count))
            found.append((device, AdapterLocation(bus: libusb_get_bus_number(device), ports: Array(ports.prefix(max(0, Int(depth)))))))
        }
        return try body(found)
    }

    private var context: OpaquePointer?
    private var handle: OpaquePointer?
    private var claimed = false

    /// Opens and claims the adapter at `location` (or the first one); nil when it is not attached.
    private init?(context: OpaquePointer, location: AdapterLocation?) throws {
        var opening: OpaquePointer?
        let status: Int32 = Self.withAdapters(context) { adapters in
            // The adapter at `location`, or the first supported one when no location is given.
            guard let device = adapters.first(where: { location == nil || $0.1 == location })?.0 else {
                return Int32(LIBUSB_ERROR_NOT_FOUND.rawValue)
            }
            return libusb_open(device, &opening)
        }
        if status == Int32(LIBUSB_ERROR_NOT_FOUND.rawValue) { return nil }
        if status != 0 { throw Self.error(status, "open") }
        guard let opened = opening else { return nil }
        // Own the context only once the device is open: deinit runs even when init returns nil.
        self.context = context
        handle = opened
        let configured = libusb_set_configuration(opened, 1)
        if configured != 0 && configured != Int32(LIBUSB_ERROR_BUSY.rawValue) { throw usbError(configured, "set configuration") }
        try check(libusb_claim_interface(opened, JUA254.interface), "claim interface")
        claimed = true
    }

    /// Open the adapter, verify it is a JUA254H with an HDMI sink and the 1080p60 mode entry, and
    /// set that mode, as open_device() does. Throws .noHDMI when no monitor is reported.
    static func open(at location: AdapterLocation? = nil) throws -> Adapter? {
        var context: OpaquePointer?
        guard libusb_init(&context) == 0, let context = context else { throw AdapterError.usb("libusb_init failed") }
        let made: Adapter?
        do { made = try Adapter(context: context, location: location) } catch { libusb_exit(context); throw error }
        guard let adapter = made else { libusb_exit(context); return nil }
        do {
            let project = try adapter.controlIn(request: 0xB0, value: 0, index: 3, length: 16)
            let name = String(decoding: project.prefix { $0 != 0 }, as: UTF8.self)
            guard JUA254.models.values.contains(name) else { throw AdapterError.protocolFailure("unsupported adapter model \(name)") }
            guard try adapter.controlIn(request: 0x87, value: 0, index: 0, length: 1) == Data([1]) else { throw AdapterError.noHDMI }
            let mode = try adapter.controlIn(request: 0x89, value: 0, index: 0x0340, length: 32)
            guard mode == JUA254.mode1080p60 else { throw AdapterError.protocolFailure("unexpected 1080p60 mode entry") }
            try adapter.controlOut(request: 0x03, value: 0)
            Thread.sleep(forTimeInterval: 0.25)
            try adapter.controlOut(request: 0x12, value: 0, data: mode)
            Thread.sleep(forTimeInterval: 0.25)
            try adapter.controlOut(request: 0x03, value: 0, index: 1)  // enable output: wIndex=1, as ctrl_transfer(0x40,0x03,0,1)
            return adapter
        } catch {
            adapter.close(disableOutput: false)  // release the claim now, or later opens stay busy
            throw error
        }
    }

    /// Open, waiting up to `seconds` while the adapter reports no HDMI sink (it does for about a
    /// second after being plugged in). `waiting` is called once if it has to wait.
    static func open(waitingForHDMI seconds: Double, at location: AdapterLocation? = nil, waiting: () -> Void = {}) throws -> Adapter? {
        let deadline = Date().addingTimeInterval(seconds)
        var announced = false
        while true {
            do { return try open(at: location) } catch AdapterError.noHDMI where Date() < deadline {
                if !announced { waiting(); announced = true }
                Thread.sleep(forTimeInterval: 0.5)
            }
        }
    }

    /// Adapter VRAM in MB (vendor request 0x88, read-only).
    func vramMB() throws -> Int { Int(try controlIn(request: 0x88, value: 0, index: 0, length: 1).first ?? 0) }

    /// Send one frame's transfers, reproducing the template's gaps between URBs.
    func send(_ group: [Transfer]) throws {
        for transfer in group {
            if transfer.delay > 0 { Thread.sleep(forTimeInterval: transfer.delay) }
            try bulkOut(transfer.data)
        }
    }

    /// Disable video output (unless asked not to), release the interface and close.
    func close(disableOutput: Bool = true) {
        if disableOutput { try? controlOut(request: 0x03, value: 0) }
        if let handle = handle {
            if claimed { libusb_release_interface(handle, JUA254.interface) }
            libusb_close(handle)
        }
        if let context = context { libusb_exit(context) }
        handle = nil; context = nil; claimed = false
    }

    deinit { close(disableOutput: false) }

    // MARK: - Transfers

    func controlIn(request: UInt8, value: UInt16, index: UInt16, length: Int) throws -> Data {
        var buffer = [UInt8](repeating: 0, count: length)
        let count = libusb_control_transfer(handle, 0xC0, request, value, index, &buffer, UInt16(length), 2000)
        if count < 0 { throw usbError(count, "control 0x\(String(request, radix: 16))") }
        return Data(buffer.prefix(Int(count)))
    }

    func controlOut(request: UInt8, value: UInt16, index: UInt16 = 0, data: Data = Data()) throws {
        var buffer = [UInt8](data)
        let count = buffer.withUnsafeMutableBufferPointer {
            libusb_control_transfer(handle, 0x40, request, value, index, $0.baseAddress, UInt16($0.count), 2000)
        }
        if count < 0 { throw usbError(count, "control 0x\(String(request, radix: 16))") }
        if Int(count) != buffer.count { throw AdapterError.usb("short control write") }
    }

    func bulkOut(_ data: Data) throws {
        var buffer = [UInt8](data)
        var transferred: Int32 = 0
        try check(libusb_bulk_transfer(handle, JUA254.bulkOutEndpoint, &buffer, Int32(buffer.count), &transferred, 5000), "bulk write")
        if Int(transferred) != buffer.count { throw AdapterError.usb("short USB write") }
    }

    private func check(_ status: Int32, _ what: String) throws {
        if status != 0 { throw usbError(status, what) }
    }

    private func usbError(_ status: Int32, _ what: String) -> AdapterError { Self.error(status, what) }

    private static func error(_ status: Int32, _ what: String) -> AdapterError {
        if status == Int32(LIBUSB_ERROR_NO_DEVICE.rawValue) { return .disconnected }  // unplugged or re-enumerating
        if status == Int32(LIBUSB_ERROR_TIMEOUT.rawValue) { return .timeout(what) }
        return .usb("\(what): \(String(cString: libusb_error_name(status)))")
    }
}
