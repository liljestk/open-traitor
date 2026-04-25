import Foundation

/// Decodable container for arbitrary JSON values from the dashboard
/// (`reasoning_json`, `plan_json`, `data`, etc.).
indirect enum JSONValue: Decodable, Encodable, Equatable {
    case null
    case bool(Bool)
    case int(Int)
    case double(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null; return }
        if let b = try? c.decode(Bool.self) { self = .bool(b); return }
        if let i = try? c.decode(Int.self) { self = .int(i); return }
        if let d = try? c.decode(Double.self) { self = .double(d); return }
        if let s = try? c.decode(String.self) { self = .string(s); return }
        if let a = try? c.decode([JSONValue].self) { self = .array(a); return }
        if let o = try? c.decode([String: JSONValue].self) { self = .object(o); return }
        throw DecodingError.dataCorruptedError(in: c, debugDescription: "Unknown JSON value")
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .null: try c.encodeNil()
        case .bool(let v): try c.encode(v)
        case .int(let v): try c.encode(v)
        case .double(let v): try c.encode(v)
        case .string(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .object(let v): try c.encode(v)
        }
    }

    /// Pretty multi-line string (for displaying raw JSON in detail screens).
    func prettyString() -> String {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let data = try? encoder.encode(self),
              let s = String(data: data, encoding: .utf8) else { return "—" }
        return s
    }

    /// Best-effort scalar string for table rows.
    func displayString() -> String {
        switch self {
        case .null: return "—"
        case .bool(let v): return v ? "true" : "false"
        case .int(let v): return "\(v)"
        case .double(let v): return String(format: "%g", v)
        case .string(let v): return v
        case .array(let v): return "[\(v.count) items]"
        case .object(let v): return "{\(v.count) keys}"
        }
    }

    var asDouble: Double? {
        switch self {
        case .double(let d): return d
        case .int(let i): return Double(i)
        case .bool(let b): return b ? 1 : 0
        default: return nil
        }
    }

    var asString: String? {
        if case .string(let s) = self { return s }
        return nil
    }
}
