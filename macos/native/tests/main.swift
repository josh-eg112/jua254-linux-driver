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
    print("numbering: checked")
}

print(failures == 0 ? "ALL PASSED" : "\(failures) FAILURES")
exit(failures == 0 ? 0 : 1)
