import SwiftUI

/// Lightweight reusable formatting helpers used across views.
enum Fmt {
    static func money(_ v: Double?, decimals: Int = 2) -> String {
        guard let v else { return "—" }
        return String(format: "$%.\(decimals)f", v)
    }

    static func pct(_ v: Double?, decimals: Int = 2) -> String {
        guard let v else { return "—" }
        return String(format: "%.\(decimals)f%%", v)
    }

    static func num(_ v: Double?, decimals: Int = 2) -> String {
        guard let v else { return "—" }
        return String(format: "%.\(decimals)f", v)
    }

    static func int(_ v: Int?) -> String {
        guard let v else { return "—" }
        return "\(v)"
    }

    /// Parse ISO8601 timestamps used by the dashboard. Returns nil on failure.
    static func parseDate(_ s: String?) -> Date? {
        guard let s else { return nil }
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: s) { return d }
        f.formatOptions = [.withInternetDateTime]
        return f.date(from: s)
    }

    static func relative(_ s: String?) -> String {
        guard let date = parseDate(s) else { return s ?? "—" }
        let rf = RelativeDateTimeFormatter()
        rf.unitsStyle = .short
        return rf.localizedString(for: date, relativeTo: Date())
    }

    static func short(_ s: String?) -> String {
        guard let date = parseDate(s) else { return s ?? "—" }
        let f = DateFormatter()
        f.dateStyle = .short
        f.timeStyle = .short
        return f.string(from: date)
    }

    static func badgeColor(forAction action: String?) -> Color {
        switch (action ?? "").lowercased() {
        case "buy": return .green
        case "sell": return .red
        case "hold": return .gray
        default: return .blue
        }
    }

    static func sentimentColor(_ s: String?) -> Color {
        switch (s ?? "").lowercased() {
        case "bullish": return .green
        case "bearish": return .red
        default: return .gray
        }
    }

    static func severityColor(_ s: String?) -> Color {
        switch (s ?? "").lowercased() {
        case "error", "critical", "fatal": return .red
        case "warn", "warning": return .orange
        case "info": return .blue
        default: return .secondary
        }
    }
}

/// Helper view that shows an error inline.
struct ErrorBox: View {
    let message: String?
    var body: some View {
        if let m = message, !m.isEmpty {
            Text(m)
                .font(.footnote)
                .foregroundStyle(.red)
                .padding(.vertical, 4)
        }
    }
}
