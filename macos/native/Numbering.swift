// Which number (and so which virtual display, "JUA254" or "JUA254 2", and arrangement) an adapter
// in a given USB port gets. Pure logic, tested by tests/main.swift on Linux and macOS.

/// A port keeps the number it had last time when that number is free; otherwise it gets the lowest
/// free number. The first adapter (no other attached) is always number 1, whichever port it is in.
/// `known` maps port descriptions to remembered numbers and is updated.
func displayNumber(for port: String, known: inout [String: Int], inUse: Set<Int>) -> Int {
    if let remembered = known[port], !inUse.contains(remembered), !inUse.isEmpty || remembered == 1 {
        return remembered
    }
    let number = (1...).first { !inUse.contains($0) }!
    known[port] = number
    return number
}
