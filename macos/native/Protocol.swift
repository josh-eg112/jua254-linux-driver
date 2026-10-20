// JUA254 frame protocol: a Swift port of the production framing in linux/jua254_stream.py
// (LiveFramer with fb_rotation, sized_payload and command ring), checked byte for byte against
// golden vectors from the Python code (macos/native/tests). Platform-neutral: builds on Linux too.

import Foundation

enum JUA254 {
    static let vendorID: UInt16 = 0x0711
    static let productID: UInt16 = 0x5600
    static let interface: Int32 = 0
    static let bulkOutEndpoint: UInt8 = 0x02
    static let projectCode = "JUA254H"
    /// Supported models: USB product ID -> model code (0xB0). Both are MCT Trigger 6 with the same
    /// firmware (bcdDevice 0x1010), endpoints, 58 MB VRAM and mode table (checked 2026-10-16).
    static let models: [UInt16: String] = [0x5600: "JUA254H", 0x5601: "A8640H"]
    /// 0x89 mode-table entry at wIndex 0x0340: 1920x1080@60.
    static let mode1080p60 = Data(hex: "144402003c0098088007d8072c00650438043c040500bb02e8031d0101010000")

    /// Largest JPEG whose sized transfer fits the captured 471008-byte envelope.
    static let capacity = 469_936
    /// 0x89 entries 0..29 are output modes (wIndex n*32); later bytes are other data.
    static let modeTableEntries = 30
    static let chunkBytes = 102_400
    static let sizedPadBytes = 1024
    static let commandRingBase = 0x230_0000
    static let commandRingPrimedFrames = 10
    static let resetFlag: UInt8 = 0x80
    /// The three (Y, UV) VRAM destinations the Windows driver cycles through (1080p).
    static let rotationSlots: [(UInt32, UInt32)] = [(0x30, 0x1fe030), (0xfffef0, 0x11fdef0), (0x1fffdb0, 0x21fddb0)]
}

/// The final type-3 group of the linux-rgb-bars-20260825 handoff (a Windows capture): its video
/// header, logical envelope size, and the gaps between its 10 URBs, which the sender reproduces.
enum Template {
    static let header = Data(hex: "03000000b02f07008e01000006000000800780073000000030e01f00000000000d000000000000000000000000000000")
    static let total = 471_008
    /// Seconds from the template's first record to each of its 10 records.
    static let recordTimes: [Double] = [0.0, 0.001659, 0.002745, 0.002788, 0.003949, 0.004024, 0.005089, 0.005131, 0.006429, 0.006508]
}

/// Windows/vendor logical length: JPEG + 48-byte header + 1024 pad, with the 32-byte descriptor
/// rounded to 2048 (exact for all 15 captured transfers).
func sizedPayloadTotal(_ jpegLength: Int) -> Int {
    let unit = 2048
    return (jpegLength + 48 + JUA254.sizedPadBytes + 32 + unit - 1) / unit * unit - 32
}

/// Triple buffering of the 48-byte video header: each frame advances the FenceID (word 2) and
/// moves the Y/UV destination (words 5/6) to the next slot, so the adapter decodes off-screen.
struct FramebufferRotation {
    let template: Data
    let slots: [(UInt32, UInt32)]
    var index: Int
    var fence: UInt32

    init(templateHeader: Data = Template.header, slots: [(UInt32, UInt32)] = JUA254.rotationSlots) {
        precondition(templateHeader.count == 48, "video header must be 48 bytes")
        template = templateHeader
        self.slots = slots
        let current = (templateHeader.u32(at: 20), templateHeader.u32(at: 24))
        index = slots.firstIndex(where: { $0 == current }) ?? slots.count - 1
        fence = templateHeader.u32(at: 8)
    }

    mutating func nextHeader() -> Data {
        index = (index + 1) % slots.count
        fence = fence &+ 1
        var header = template
        header.setU32(fence, at: 8)
        header.setU32(slots[index].0, at: 20)
        header.setU32(slots[index].1, at: 24)
        return header
    }
}

/// JPEG upload ring in adapter VRAM, advanced as Windows does: transfers are placed back to back,
/// wrap to the base when the next one would pass the end, and the first ten frames after start or
/// a wrap carry the vendor reset flag.
struct CommandRing {
    let base: Int
    let end: Int
    var next: Int
    var placed = 0

    init(vramMB: Int, base: Int = JUA254.commandRingBase) throws {
        self.base = base
        end = vramMB * 1024 * 1024
        next = base
        if end - base < 2 * (JUA254.capacity + 48) { throw AdapterError.protocolFailure("VRAM \(vramMB) MB leaves no command ring") }
    }

    mutating func place(_ total: Int) -> (address: Int, reset: Bool) {
        var wrapped = false
        if next + total > end { next = base; wrapped = true }
        let address = next
        next += total
        placed += 1
        return (address, wrapped || placed <= JUA254.commandRingPrimedFrames)
    }
}

/// One USB bulk write and the delay to wait before it (from the template's capture timing).
struct Transfer {
    let delay: Double
    let data: Data
}

/// One logical type-3 transfer: 32-byte descriptors (total, address, size, offset, more) and
/// payload chunks of at most 102400 bytes, as in framed_group().
func framedGroup(jpeg: Data, header: Data, total: Int, address: Int, reset: Bool) throws -> [Transfer] {
    guard total <= Template.total else { throw AdapterError.protocolFailure("frame exceeds the captured envelope") }
    guard total >= 48 + jpeg.count else { throw AdapterError.protocolFailure("JPEG does not fit the logical transfer") }
    var head = header
    head.setU32(UInt32(total - 48), at: 4)
    if reset { head[head.startIndex + 47] |= JUA254.resetFlag }
    // Built once at its final size; the payload chunks below share its bytes rather than copying them.
    var body = Data(capacity: total)
    body.append(head)
    body.append(jpeg)
    body.count = total  // zero padding
    var out: [Transfer] = []
    var offset = 0
    while offset < total {
        let size = min(JUA254.chunkBytes, total - offset)
        var descriptor = Data(count: 32)
        for (i, word) in [0, total, address, size, offset, offset + size < total ? 1 : 0, 0, 0].enumerated() {
            descriptor.setU32(UInt32(truncatingIfNeeded: word), at: i * 4)
        }
        for data in [descriptor, body[offset..<offset + size]] {
            let n = out.count
            guard n < Template.recordTimes.count else { throw AdapterError.protocolFailure("more URBs than the template") }
            out.append(Transfer(delay: n == 0 ? 0 : Template.recordTimes[n] - Template.recordTimes[n - 1], data: data))
        }
        offset += size
    }
    return out
}

/// Builds each frame's transfers the way the Linux production streamer does (rotation, sized
/// payloads, command ring), for the default 1920x1080@60 mode.
/// An adapter output mode and the VRAM layout derived from it, as VideoMode in jua254_stream.py:
/// the three buffer bases stay where Windows put them, each UV plane follows its Y plane after
/// pitch * align16(height) bytes, and the command ring starts at the first 1 MiB boundary above the
/// third UV plane. 1920x1080@60 gives exactly the captured slots and ring base.
struct VideoMode: Hashable, CustomStringConvertible {
    let width: Int, height: Int, refresh: Int
    static let standard = VideoMode(width: 1920, height: 1080, refresh: 60)
    static let frameSlotStride = 0xfffec0

    var description: String { "\(width)x\(height)@\(refresh)" }
    var pitch: Int { width }
    private var plane: Int { pitch * ((height + 15) / 16 * 16) }
    /// Width a multiple of 16 and the NV12 frame fits a buffer slot.
    var isSupported: Bool { width > 0 && height > 0 && width % 16 == 0 && plane * 3 / 2 <= Self.frameSlotStride }
    var slots: [(UInt32, UInt32)] { JUA254.rotationSlots.map { ($0.0, $0.0 + UInt32(plane)) } }
    var ringBase: Int {
        let end = Int(slots[slots.count - 1].1) + plane / 2
        return (end + (1 << 20) - 1) & ~((1 << 20) - 1)
    }
    /// The template type-3 header with this mode's pitch (word 4) and first buffer (words 5/6).
    func header(_ template: Data = Template.header) -> Data {
        var header = template
        header.setU32(UInt32(pitch | (pitch << 16)), at: 16)
        header.setU32(slots[0].0, at: 20)
        header.setU32(slots[0].1, at: 24)
        return header
    }

    init(width: Int, height: Int, refresh: Int) { self.width = width; self.height = height; self.refresh = refresh }

    /// "1680x1050@60" -> mode, or nil.
    init?(_ text: String) {
        let parts = text.split(whereSeparator: { $0 == "x" || $0 == "@" }).compactMap { Int($0) }
        guard parts.count == 3 else { return nil }
        self.init(width: parts[0], height: parts[1], refresh: parts[2])
    }

    /// (hactive, vactive, refresh) of one 32-byte 0x89 mode-table entry, as decode_mode_entry().
    init(modeEntry blob: Data) {
        let refresh = Int(blob.u16(at: 4)), width = Int(blob.u16(at: 8)), height = Int(blob.u16(at: 16))
        self.init(width: width, height: height, refresh: refresh)
    }
}

/// Builds each frame's transfers the way the Linux production streamer does (rotation, sized
/// payloads, command ring) for one output mode.
struct Framer {
    var rotation: FramebufferRotation
    var ring: CommandRing

    init(vramMB: Int, mode: VideoMode = .standard) throws {
        rotation = FramebufferRotation(templateHeader: mode.header(), slots: mode.slots)
        ring = try CommandRing(vramMB: vramMB, base: mode.ringBase)
    }

    mutating func group(jpeg: Data) throws -> [Transfer] {
        let header = rotation.nextHeader()
        let total = sizedPayloadTotal(jpeg.count)
        let (address, reset) = ring.place(total)
        return try framedGroup(jpeg: jpeg, header: header, total: total, address: address, reset: reset)
    }
}

enum AdapterError: Error, CustomStringConvertible {
    case notFound
    case disconnected
    case noHDMI
    case timeout(String)
    case usb(String)
    case protocolFailure(String)
    case unsuitableJPEG(String)

    var description: String {
        switch self {
        case .notFound: return "JUA254 not found"
        case .disconnected: return "Adapter disconnected"
        case .noHDMI: return "No HDMI monitor detected on the adapter"
        case .timeout(let what): return "The adapter stopped responding (\(what))"
        case .usb(let what): return "USB error: \(what)"
        case .protocolFailure(let what): return what
        case .unsuitableJPEG(let what): return "JPEG unsuitable for the adapter: \(what)"
        }
    }
}

extension Data {
    init(hex: String) {
        var bytes = [UInt8]()
        var index = hex.startIndex
        while index < hex.endIndex {
            let next = hex.index(index, offsetBy: 2)
            bytes.append(UInt8(hex[index..<next], radix: 16)!)
            index = next
        }
        self.init(bytes)
    }

    func u16(at offset: Int) -> UInt16 {
        let i = startIndex + offset
        return UInt16(self[i]) | UInt16(self[i + 1]) << 8
    }

    func u32(at offset: Int) -> UInt32 {
        let i = startIndex + offset
        return UInt32(self[i]) | UInt32(self[i + 1]) << 8 | UInt32(self[i + 2]) << 16 | UInt32(self[i + 3]) << 24
    }

    mutating func setU32(_ value: UInt32, at offset: Int) {
        let i = startIndex + offset
        for b in 0..<4 { self[i + b] = UInt8(truncatingIfNeeded: value >> (8 * UInt32(b))) }
    }
}
