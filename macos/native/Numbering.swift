// Which number (and so which virtual display, "JUA254" or "JUA254 2", and arrangement) and which
// resolution an adapter's monitor gets. Settings follow the monitor (identified by its EDID), or
// the adapter's USB port for a monitor without a readable EDID. Pure logic, tested by
// tests/main.swift on Linux and macOS.

import Foundation

/// A key `known` uses: the monitor's key when it has one, otherwise the port description.
/// A key keeps the number it had last time when that number is free; otherwise it gets the lowest
/// free number. A lone adapter (`alone`, by default when nothing is in use) is always number 1.
/// `known` maps keys to remembered numbers and is updated.
func displayNumber(for key: String, known: inout [String: Int], inUse: Set<Int>, alone: Bool? = nil) -> Int {
    if let remembered = known[key], !inUse.contains(remembered), !(alone ?? inUse.isEmpty) || remembered == 1 {
        return remembered
    }
    let number = (1...).first { !inUse.contains($0) }!
    known[key] = number
    return number
}

/// A monitor as its EDID base block describes it.
struct Monitor: Equatable {
    /// Manufacturer, product code and serial, e.g. "EDID DEL A0B1 7MT0123A1B2C": the key its
    /// settings are kept under. Two monitors of one model without serials share a key.
    let key: String
    /// The monitor's name descriptor, e.g. "DELL P2210", when it has one.
    let name: String?

    /// nil unless `edid` is a valid 128-byte EDID base block (header and checksum).
    init?(edid: Data) {
        let b = [UInt8](edid)
        guard b.count >= 128, b[0..<8] == [0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00],
              b[0..<128].reduce(0, { ($0 + Int($1)) & 0xFF }) == 0 else { return nil }
        let id = UInt16(b[8]) << 8 | UInt16(b[9])
        let letters = [id >> 10, id >> 5, id].map { $0 & 0x1F }
        guard letters.allSatisfy({ (1...26).contains($0) }) else { return nil }
        let manufacturer = String(letters.map { Character(UnicodeScalar(UInt8($0) + 64)) })
        let product = String(UInt16(b[10]) | UInt16(b[11]) << 8, radix: 16, uppercase: true)
        let number = UInt32(b[12]) | UInt32(b[13]) << 8 | UInt32(b[14]) << 16 | UInt32(b[15]) << 24
        // Display descriptors (18 bytes at 54, 72, 90 and 108): 0xFF serial text, 0xFC name.
        var text: [UInt8: String] = [:]
        for offset in stride(from: 54, to: 126, by: 18) where b[offset] == 0 && b[offset + 1] == 0 {
            let bytes = b[(offset + 5)..<(offset + 18)].prefix { $0 != 0x0A }.filter { (0x20...0x7E).contains($0) }
            let value = String(decoding: bytes, as: UTF8.self).trimmingCharacters(in: .whitespaces)
            if !value.isEmpty && text[b[offset + 3]] == nil { text[b[offset + 3]] = value }
        }
        let serial = text[0xFF] ?? (number != 0 ? String(number) : nil)
        key = "EDID \(manufacturer) " + String(repeating: "0", count: max(0, 4 - product.count)) + product
            + (serial.map { " \($0)" } ?? "")
        name = text[0xFC]
    }
}

/// Before settings followed monitors, resolutions were kept by display number ("resolutions").
/// Gives each port (in `numbers`, port to display number) the resolution of the number it had.
func portModes(fromNumbered byNumber: [String: String], numbers: [String: Int]) -> [String: String] {
    var modes: [String: String] = [:]
    for (port, number) in numbers { modes[port] = byNumber[String(number)] }
    return modes
}

/// The first time a monitor (`key`) is seen, it takes over the settings of the port it is in: the
/// port's display number (kept for monitors without EDID) and its resolution (moved, so the next
/// new monitor in that port starts from the default).
func adoptPortSettings(monitor key: String, port: String, numbers: inout [String: Int], modes: inout [String: String]) {
    guard key != port, numbers[key] == nil else { return }
    numbers[key] = numbers[port]
    if let mode = modes.removeValue(forKey: port), modes[key] == nil { modes[key] = mode }
}
