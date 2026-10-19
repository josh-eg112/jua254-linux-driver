// Checks the Swift port against golden vectors from the proven Python code
// (macos/native/tests/make_golden.py). Built and run by macos/native/test.sh on Linux or macOS.

import Foundation

var failures = 0
func check(_ condition: Bool, _ message: @autoclosure () -> String) {
    if !condition { failures += 1; print("FAIL: \(message())") }
}

func fnv(_ data: Data) -> String {
    var hash: UInt64 = 0xcbf29ce484222325
    for byte in data { hash = (hash ^ UInt64(byte)) &* 0x100000001b3 }
    return String(format: "%016llx", hash)
}

func payload(_ n: Int, _ size: Int) -> Data {
    Data((0..<size).map { UInt8(truncatingIfNeeded: $0 * 7 + n * 13) })
}

let golden = URL(fileURLWithPath: CommandLine.arguments[1])

// Framing: 80 frames of varied sizes through rotation, sized payloads and a wrapping ring.
let frames = try JSONSerialization.jsonObject(with: Data(contentsOf: golden.appendingPathComponent("framer.json"))) as! [[String: Any]]
var framer = try Framer(vramMB: 58)
for (n, frame) in frames.enumerated() {
    let size = frame["size"] as! Int
    let expected = frame["transfers"] as! [[Any]]
    let delays = frame["delays"] as! [Double]
    let group = try framer.group(jpeg: payload(n, size))
    check(group.count == expected.count, "frame \(n): \(group.count) transfers, expected \(expected.count)")
    for (i, transfer) in group.enumerated() where i < expected.count {
        check(transfer.data.count == expected[i][0] as! Int, "frame \(n) transfer \(i): length \(transfer.data.count)")
        check(fnv(transfer.data) == expected[i][1] as! String, "frame \(n) transfer \(i): bytes differ")
        check(abs(transfer.delay - delays[i]) < 1e-6, "frame \(n) transfer \(i): delay \(transfer.delay) vs \(delays[i])")
    }
}
print("framer: \(frames.count) frames checked")

// The size limit matches the sized envelope exactly.
check(sizedPayloadTotal(JUA254.capacity) <= Template.total && sizedPayloadTotal(JUA254.capacity + 1) > Template.total,
      "capacity \(JUA254.capacity) is not the largest JPEG that fits")
do {
    var limit = try Framer(vramMB: 58)
    _ = try limit.group(jpeg: payload(0, JUA254.capacity + 1))
    check(false, "a JPEG over capacity was framed")
} catch {}

// Other output modes: VRAM layout and framing match VideoMode/LiveFramer in the Python driver.
let modes = try JSONSerialization.jsonObject(with: Data(contentsOf: golden.appendingPathComponent("modes.json"))) as! [String: Any]
check(VideoMode(modeEntry: Data(hex: modes["mode_1080p60_entry"] as! String)) == .standard, "1080p60 mode entry decodes")
for item in modes["modes"] as! [[String: Any]] {
    let name = item["mode"] as! String
    guard let mode = VideoMode(name) else { check(false, "parse \(name)"); continue }
    check(mode.description == name && mode.isSupported, "\(name): parsed and supported")
    check(mode.ringBase == item["ring_base"] as! Int, "\(name): ring base \(mode.ringBase)")
    let slots = (item["slots"] as! [[Int]]).map { (UInt32($0[0]), UInt32($0[1])) }
    check(zip(mode.slots, slots).allSatisfy { $0 == $1 }, "\(name): buffer slots")
    var framer = try Framer(vramMB: 58, mode: mode)
    for (n, frame) in (item["frames"] as! [[String: Any]]).enumerated() {
        let expected = frame["transfers"] as! [[Any]]
        let group = try framer.group(jpeg: payload(n, frame["size"] as! Int))
        check(group.count == expected.count && zip(group, expected).allSatisfy { fnv($0.data) == $1[1] as! String },
              "\(name) frame \(n): bytes differ")
    }
}
check(!VideoMode(width: 1366, height: 768, refresh: 60).isSupported, "1366 wide (not a multiple of 16) is rejected")
print("modes: \((modes["modes"] as! [Any]).count) modes checked")

// JPEG checks: same accept/reject decisions and output bytes as adapter_jpeg().
let cases = try JSONSerialization.jsonObject(with: Data(contentsOf: golden.appendingPathComponent("jpeg.json"))) as! [[String: Any]]
for item in cases {
    let file = item["file"] as! String, allow = item["allow_restart"] as! Bool
    let input = try Data(contentsOf: golden.appendingPathComponent(file))
    let label = "\(file) allow_restart=\(allow)"
    do {
        let output = try adapterJPEG(input, allowRestart: allow)
        check(item["ok"] as? String == fnv(output), "\(label): accepted (\(fnv(output))), expected \(item)")
    } catch JPEGCheck.tooLarge(let size) {
        check(item["too_large"] as? Bool == true, "\(label): too large (\(size)), expected \(item)")
    } catch JPEGCheck.unsuitable(let reason) {
        check(item["unsuitable"] as? String == reason, "\(label): unsuitable '\(reason)', expected \(item)")
    }
}
print("jpeg: \(cases.count) cases checked")

// Display numbering for several adapters.
do {
    var known: [String: Int] = [:]
    check(displayNumber(for: "20-3", known: &known, inUse: []) == 1, "first adapter is 1")
    check(displayNumber(for: "20-4", known: &known, inUse: [1]) == 2, "second adapter is 2")
    check(displayNumber(for: "20-4", known: &known, inUse: [1]) == 2, "a port keeps its number")
    check(displayNumber(for: "20-4", known: &known, inUse: []) == 1, "a lone adapter is 1 in any port")
    known = ["20-3": 1, "20-4": 2]
    check(displayNumber(for: "20-3", known: &known, inUse: [2]) == 1, "remembered number reused when free")
    check(displayNumber(for: "20-9", known: &known, inUse: [1, 2]) == 3, "third adapter is 3")
    check(displayNumber(for: "20-3", known: &known, inUse: [1]) == 2, "taken number falls back to the lowest free")
    known = ["20-3": 3]
    check(displayNumber(for: "20-3", known: &known, inUse: [], alone: false) == 3,
          "the first of several adapters to read its monitor keeps its number")
    check(displayNumber(for: "20-3", known: &known, inUse: [], alone: true) == 1, "a lone adapter is 1 even if told so")
    print("numbering: checked")
}

// Monitor identity from EDID, and settings that follow the monitor.
func edid(manufacturer: String = "DEL", product: UInt16 = 0xA0B1, serial: UInt32 = 0,
          descriptors: [(UInt8, String)] = []) -> Data {
    var b = [UInt8](repeating: 0, count: 128)
    b[0..<8] = [0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00]
    let letters = manufacturer.unicodeScalars.map { UInt16($0.value - 64) }
    let id = letters[0] << 10 | letters[1] << 5 | letters[2]
    b[8] = UInt8(id >> 8); b[9] = UInt8(id & 0xFF)
    b[10] = UInt8(product & 0xFF); b[11] = UInt8(product >> 8)
    for i in 0..<4 { b[12 + i] = UInt8(truncatingIfNeeded: serial >> (8 * UInt32(i))) }
    b[18] = 1; b[19] = 3
    b[54] = 0x02; b[55] = 0x3A  // a detailed timing descriptor in the first slot
    for (n, (tag, text)) in descriptors.enumerated() {
        let offset = 72 + 18 * n
        b[offset + 3] = tag
        let bytes = Array(text.utf8.prefix(13)) + (text.utf8.count < 13 ? [0x0A] : [])
        for (i, byte) in (bytes + [UInt8](repeating: 0x20, count: 13 - bytes.count)).enumerated() { b[offset + 5 + i] = byte }
    }
    b[127] = UInt8((256 - b[0..<127].reduce(0) { ($0 + Int($1)) & 0xFF }) & 0xFF)
    return Data(b)
}
do {
    let full = Monitor(edid: edid(serial: 12345678, descriptors: [(0xFF, "7MT0123A1B2C"), (0xFC, "DELL P2210")]))
    check(full == Monitor(edid: edid(serial: 12345678, descriptors: [(0xFF, "7MT0123A1B2C"), (0xFC, "DELL P2210")])), "same EDID, same monitor")
    check(full?.key == "EDID DEL A0B1 7MT0123A1B2C", "key uses the serial text: \(full?.key ?? "nil")")
    check(full?.name == "DELL P2210", "name descriptor: \(full?.name ?? "nil")")
    check(Monitor(edid: edid(manufacturer: "GSM", product: 0x5B7F, serial: 0x01020304))?.key == "EDID GSM 5B7F 16909060",
          "key uses the numeric serial without serial text")
    check(Monitor(edid: edid(product: 0x00C1))?.key == "EDID DEL 00C1", "no serial: manufacturer and product only")
    check(Monitor(edid: edid(serial: 1))?.key != Monitor(edid: edid(serial: 2))?.key, "serials tell two of a model apart")
    var bad = edid(serial: 5); bad[20] ^= 1
    check(Monitor(edid: bad) == nil, "a bad checksum is rejected")
    check(Monitor(edid: Data(hex: "6402004347f23fff") + Data(count: 124)) == nil, "non-EDID bytes are rejected")
    check(Monitor(edid: edid().prefix(64)) == nil, "a short read is rejected")

    // The reported move: a 1680x1050 monitor from the adapter in 20-14.3.2 to the one in 20-14.3.3.
    var numbers = ["20-14.2": 1, "20-14.3.2": 2, "20-14.3.3": 3]
    var modes = portModes(fromNumbered: ["2": "1680x1050@60"], numbers: numbers)
    check(modes == ["20-14.3.2": "1680x1050@60"], "old resolutions go to the port that had the number: \(modes)")
    let small = "EDID DEL A0B1 SMALL", big = "EDID GSM 5B7F BIG"
    adoptPortSettings(monitor: small, port: "20-14.3.2", numbers: &numbers, modes: &modes)
    adoptPortSettings(monitor: big, port: "20-14.3.3", numbers: &numbers, modes: &modes)
    check(numbers[small] == 2 && numbers[big] == 3, "first seen, a monitor takes its port's number")
    check(modes == [small: "1680x1050@60"], "and its port's resolution moves to it: \(modes)")
    // Swap the two monitors between adapters; seen again, they keep their own settings.
    adoptPortSettings(monitor: small, port: "20-14.3.3", numbers: &numbers, modes: &modes)
    adoptPortSettings(monitor: big, port: "20-14.3.2", numbers: &numbers, modes: &modes)
    check(modes[small] == "1680x1050@60" && modes[big] == nil, "resolution follows the monitor: \(modes)")
    let bigNumber = displayNumber(for: big, known: &numbers, inUse: [], alone: false)
    let smallNumber = displayNumber(for: small, known: &numbers, inUse: [bigNumber], alone: false)
    check(smallNumber == 2 && bigNumber == 3, "numbers follow the monitor: \(smallNumber), \(bigNumber)")
    // A monitor without EDID uses the port, which keeps its own settings.
    adoptPortSettings(monitor: "20-14.2", port: "20-14.2", numbers: &numbers, modes: &modes)
    check(numbers["20-14.2"] == 1 && modes["20-14.2"] == nil, "a port key is left as it is")
    print("monitors: checked")
}

print(failures == 0 ? "ALL PASSED" : "\(failures) FAILURES")
exit(failures == 0 ? 0 : 1)
