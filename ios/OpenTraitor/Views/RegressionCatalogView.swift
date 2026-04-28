import SwiftUI

/// Catalog view of all per-symbol/per-event regression models (`/regression/models`).
/// Use SymbolInsightsView for the per-symbol drill-down.
struct RegressionCatalogView: View {
    @EnvironmentObject var session: SessionStore
    @State private var data: RegressionModelsResponse?
    @State private var symbolFilter: String = ""
    @State private var eventFilter: String = ""
    @State private var minSamples: Int = 0
    @State private var error: String?

    var body: some View {
        List {
            Section("Filters") {
                TextField("Symbol (optional)", text: $symbolFilter)
                    .textInputAutocapitalization(.characters).autocorrectionDisabled()
                TextField("Event type (optional)", text: $eventFilter)
                    .autocorrectionDisabled()
                Stepper("Min samples: \(minSamples)", value: $minSamples, in: 0...500, step: 10)
                Button("Apply") { Task { await load() } }
            }
            if let d = data {
                Section("Models (\(d.count) of \(d.total))") {
                    if d.rows.isEmpty {
                        Text("No models").foregroundStyle(.secondary)
                    }
                    ForEach(d.rows) { r in
                        VStack(alignment: .leading, spacing: 2) {
                            HStack {
                                Text("\(r.symbol) — \(r.event_type)").font(.headline)
                                Spacer()
                                if let r2 = r.r_squared {
                                    Text("R² \(Fmt.num(r2, decimals: 3))")
                                        .font(.caption.weight(.bold))
                                        .foregroundStyle(qualityColor(r2))
                                }
                            }
                            HStack {
                                Text("h=\(r.horizon_days)d").font(.caption2)
                                Text("n=\(r.sample_count)").font(.caption2)
                                if let h = r.hit_rate { Text("hit \(Fmt.pct(h * 100))").font(.caption2) }
                                if let m = r.mean_forward_return { Text("μ \(Fmt.pct(m * 100))").font(.caption2) }
                            }
                            .foregroundStyle(.secondary)
                            if let coefs = r.coefficients_json, !coefs.isEmpty {
                                Text(coefs.sorted(by: { $0.key < $1.key }).map { "\($0.key): \(Fmt.num($0.value, decimals: 3))" }.joined(separator: " • "))
                                    .font(.caption2).foregroundStyle(.tertiary)
                            }
                            if let ts = r.computed_at {
                                Text(Fmt.relative(ts)).font(.caption2).foregroundStyle(.tertiary)
                            }
                        }
                    }
                }
            }
            ErrorBox(message: error)
        }
        .navigationTitle("Regression Models")
        .refreshable { await load() }
        .task { await load() }
    }

    private func qualityColor(_ r2: Double) -> Color {
        if r2 >= 0.5 { return .green }
        if r2 >= 0.2 { return .orange }
        return .secondary
    }

    private func load() async {
        var q: [URLQueryItem] = []
        if !symbolFilter.isEmpty { q.append(URLQueryItem(name: "symbol", value: symbolFilter)) }
        if !eventFilter.isEmpty { q.append(URLQueryItem(name: "event_type", value: eventFilter)) }
        if minSamples > 0 { q.append(URLQueryItem(name: "min_samples", value: "\(minSamples)")) }
        do {
            self.data = try await session.api.request("/regression/models", query: q, as: RegressionModelsResponse.self)
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }
}
