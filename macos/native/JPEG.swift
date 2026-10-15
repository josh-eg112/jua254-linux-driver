// Make an ImageIO JPEG match what the JUA254 decodes: a Swift port of adapter_jpeg() in
// macos/mirror.py (with normalize_component_ids and the jpeg_metadata checks it relies on).

import Foundation

enum JPEGCheck: Error, Equatable {
    /// The adapter cannot decode it (the reason says why); use another encoder.
    case unsuitable(String)
    /// Valid, but larger than the adapter's upload slot; skip this frame.
    case tooLarge(Int)
}

/// Sampling factors the adapter decodes (Y 2x2, Cb/Cr 1x1).
let adapterSampling: [UInt8] = [0x22, 0x11, 0x11]

/// Drop APP1-15/COM segments (EXIF, ICC), require baseline 4:2:0 (restart markers only if
/// `allowRestart`), renumber components 1/2/3 to the captured encoder's 0/1/2, and check the size.
func adapterJPEG(_ jpeg: Data, allowRestart: Bool = true, width: Int = 1920, height: Int = 1080) throws -> Data {
    let bytes = [UInt8](jpeg)
    guard bytes.count >= 4, bytes[0] == 0xFF, bytes[1] == 0xD8 else { throw JPEGCheck.unsuitable("not a JPEG") }
    var out: [UInt8] = [0xFF, 0xD8]
    var problems: [String] = []
    var offset = 2
    var foundScan = false
    while offset + 4 <= bytes.count {
        guard bytes[offset] == 0xFF else { throw JPEGCheck.unsuitable("bad marker at \(offset)") }
        let marker = bytes[offset + 1]
        if marker == 0xDA {
            out.append(contentsOf: bytes[offset...])
            foundScan = true
            break
        }
        let length = Int(bytes[offset + 2]) << 8 | Int(bytes[offset + 3])
        let end = offset + 2 + length
        guard length >= 2, end <= bytes.count else { throw JPEGCheck.unsuitable("invalid JPEG segment length") }
        if marker == 0xDD && !allowRestart { problems.append("uses restart markers (DRI)") }
        if !((0xE1...0xEF).contains(marker) || marker == 0xFE) { out.append(contentsOf: bytes[offset..<end]) }
        offset = end
    }
    guard foundScan else { throw JPEGCheck.unsuitable("no image data (SOS)") }
    try renumberComponents(&out)
    let frame = try baselineFrame(out)
    if frame.sampling != adapterSampling {
        let shown = frame.sampling.map { "'0x" + String($0, radix: 16) + "'" }.joined(separator: ", ")
        problems.append("chroma sampling [\(shown)], adapter needs 4:2:0")  // worded as in mirror.py
    }
    if !problems.isEmpty { throw JPEGCheck.unsuitable(problems.joined(separator: "; ")) }
    guard frame.width == width, frame.height == height else { throw JPEGCheck.unsuitable("wrong size") }
    guard out.count <= JUA254.capacity else { throw JPEGCheck.tooLarge(out.count) }
    return Data(out)
}

/// Conventional JPEG component IDs 1/2/3 become the captured encoder's 0/1/2 (SOF0 and SOS).
func renumberComponents(_ data: inout [UInt8]) throws {
    func mapped(_ id: UInt8) throws -> UInt8 {
        guard (1...3).contains(id) else { throw JPEGCheck.unsuitable("unexpected component id \(id)") }
        return id - 1
    }
    var offset = 2
    while offset < data.count {
        while offset < data.count && data[offset] == 0xFF { offset += 1 }
        guard offset + 2 < data.count else { break }
        let marker = data[offset]
        offset += 1
        if marker == 0xD9 { break }
        let length = Int(data[offset]) << 8 | Int(data[offset + 1])
        if marker == 0xC0 {
            for i in 0..<Int(data[offset + 7]) { data[offset + 8 + 3 * i] = try mapped(data[offset + 8 + 3 * i]) }
        } else if marker == 0xDA {
            for i in 0..<Int(data[offset + 2]) { data[offset + 3 + 2 * i] = try mapped(data[offset + 3 + 2 * i]) }
            break
        }
        offset += length
    }
}

/// Width, height and sampling factors of a baseline (SOF0) JPEG that also has a scan and ends in EOI.
func baselineFrame(_ data: [UInt8]) throws -> (width: Int, height: Int, sampling: [UInt8]) {
    guard data.count >= 4, data[0] == 0xFF, data[1] == 0xD8, data[data.count - 2] == 0xFF, data[data.count - 1] == 0xD9 else {
        throw JPEGCheck.unsuitable("JPEG must have exact SOI/EOI markers and no trailing bytes")
    }
    var offset = 2
    var frame: (Int, Int, [UInt8])?
    var scan = false
    while offset < data.count {
        guard data[offset] == 0xFF else { throw JPEGCheck.unsuitable("invalid JPEG marker at offset \(offset)") }
        while offset < data.count && data[offset] == 0xFF { offset += 1 }
        guard offset < data.count else { break }
        let marker = data[offset]
        offset += 1
        if marker == 0xD9 { break }
        if (0xD0...0xD7).contains(marker) || [0x00, 0x01, 0xD8].contains(marker) { continue }
        guard offset + 2 <= data.count else { throw JPEGCheck.unsuitable("invalid JPEG segment length") }
        let length = Int(data[offset]) << 8 | Int(data[offset + 1])
        guard length >= 2, offset + length <= data.count else { throw JPEGCheck.unsuitable("invalid JPEG segment length") }
        let segment = Array(data[(offset + 2)..<(offset + length)])
        if marker == 0xC0 {
            let height = Int(segment[1]) << 8 | Int(segment[2]), width = Int(segment[3]) << 8 | Int(segment[4])
            frame = (width, height, (0..<Int(segment[5])).map { segment[7 + 3 * $0] })
        }
        if marker == 0xDA { scan = true; break }
        offset += length
    }
    guard let (width, height, sampling) = frame, scan else { throw JPEGCheck.unsuitable("JPEG must contain baseline SOF0 and SOS markers") }
    return (width, height, sampling)
}
