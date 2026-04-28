import SwiftUI

/// Per-symbol drill-down combining picker (`/symbols/list`) and summary (`/symbols/{symbol}/summary`).
/// Mirrors the dashboard's RegressionAI / Symbol Insights page.
struct SymbolInsightsView: View {
    @EnvironmentObject var session: SessionStore
    @State private var search: String = ""
    @State private var list: [SymbolListItem] = []
    @State private var selected: String?
    @State private var summary: SymbolSummary?
    @State private var loadingList = false
    @State private var loadingSummary = false
    @State private var error: String?

    var filtered: [SymbolListItem] {
        guard !search.isEmpty else { return list }
        let q = search.uppercased()
        return list.filter { $0.symbol.contains(q) || $0.base.contains(q) }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                TextField("Search symbol", text: $search)
                    .textInputAutocapitalization(.characters)
                    .autocorrectionDisabled()
                    .textFieldStyle(.roundedBorder)
                if loadingList { ProgressView().controlSize(.small) }
            }
            .padding(.horizontal).padding(.vertical, 6)

            if selected == nil {
                List(filtered) { item in
                    Button { Task { await pick(item.symbol) } } label: {
                        HStack {
                            VStack(alignment: .leading) {
                                Text(item.symbol).font(.headline)
                                Text("\(item.base)/\(item.quote) • \(item.domain)")
                                    .font(.caption2).foregroundStyle(.secondary)
                            }
                            Spacer()
                            qualityBadges(item)
                        }
                    }
                }
                .navigationTitle("Symbol Insights")
                .task { await loadList() }
            } else if let s = summary {
                summaryView(s)
            } else if loadingSummary {
                ProgressView("Loading…").frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                Text("No data").foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder private func qualityBadges(_ item: SymbolListItem) -> some View {
        HStack(spacing: 4) {
            badgeDot(active: item.has_regression, label: "R", color: .blue)
            badgeDot(active: item.has_patterns, label: "P", color: .purple)
            badgeDot(active: item.has_trades, label: "T", color: .green)
        }
    }
    private func badgeDot(active: Bool, label: String, color: Color) -> some View {
        Text(label)
            .font(.caption2.weight(.bold))
            .frame(width: 18, height: 18)
            .background((active ? color : Color.gray).opacity(0.25))
            .foregroundStyle(active ? color : .secondary)
            .clipShape(Circle())
    }

    @ViewBuilder private func summaryView(_ s: SymbolSummary) -> some View {
        List {
            Section {
                Button { selected = nil; summary = nil } label: {
                    Label("Back to symbol list", systemImage: "chevron.left")
                }
                Text(s.plain_summary).font(.subheadline)
            } header: {
                Text("\(s.symbol) (\(s.domain))")
            }

            if !s.regression.rows.isEmpty {
                Section("Regression models (\(s.regression.count))") {
                    ForEach(s.regression.rows) { r in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(r.event_type).font(.headline)
                            HStack {
                                if let r2 = r.r_squared { Text("R² \(Fmt.num(r2, decimals: 3))").font(.caption2) }
                                if let h = r.hit_rate { Text("hit \(Fmt.pct(h * 100))").font(.caption2) }
                                if let m = r.mean_forward_return { Text("μ \(Fmt.pct(m * 100))").font(.caption2) }
                                Text("n=\(r.sample_count) h=\(r.horizon_days)d").font(.caption2).foregroundStyle(.secondary)
                            }
                            if let coefs = r.coefficients_json, !coefs.isEmpty {
                                Text(coefs.sorted(by: { $0.key < $1.key }).map { "\($0.key): \(Fmt.num($0.value, decimals: 3))" }.joined(separator: " • "))
                                    .font(.caption2).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }

            if !s.patterns.upcoming.isEmpty {
                Section("Upcoming patterns (\(s.patterns.count))") {
                    ForEach(s.patterns.upcoming) { e in
                        VStack(alignment: .leading, spacing: 2) {
                            HStack {
                                Text(e.event_type).font(.headline)
                                Spacer()
                                if let dir = e.outcome?.direction {
                                    Text(dir.uppercased()).font(.caption2.weight(.bold))
                                        .foregroundStyle(dir == "bullish" ? .green : (dir == "bearish" ? .red : .secondary))
                                }
                            }
                            Text(Fmt.short(e.event_ts)).font(.caption2).foregroundStyle(.secondary)
                            if let n = e.outcome?.n_matches { Text("matches: \(n)").font(.caption2) }
                            if let c = e.outcome?.confidence { Text("confidence: \(Fmt.pct(c * 100))").font(.caption2) }
                        }
                    }
                }
            }

            if !s.trades.rows.isEmpty {
                Section("Recent trades (\(s.trades.count))") {
                    ForEach(s.trades.rows) { t in
                        HStack {
                            VStack(alignment: .leading) {
                                Text("\(t.action.uppercased()) \(t.pair)").font(.subheadline)
                                Text(Fmt.relative(t.ts)).font(.caption2).foregroundStyle(.secondary)
                            }
                            Spacer()
                            VStack(alignment: .trailing) {
                                Text(Fmt.money(t.price)).font(.caption)
                                if let p = t.pnl { Text(Fmt.money(p)).font(.caption2).foregroundStyle(p >= 0 ? .green : .red) }
                            }
                        }
                    }
                }
            }

            if !s.reasoning.cycles.isEmpty {
                Section("Reasoning cycles (\(s.reasoning.count))") {
                    ForEach(s.reasoning.cycles) { c in
                        VStack(alignment: .leading, spacing: 2) {
                            HStack {
                                Text(c.action ?? c.signal_type ?? "?").font(.headline)
                                Spacer()
                                if let conf = c.confidence { Text(Fmt.pct(conf * 100)).font(.caption2) }
                            }
                            HStack {
                                Text("\(c.agent_count) agents").font(.caption2).foregroundStyle(.secondary)
                                Spacer()
                                if let p = c.pnl {
                                    Text(Fmt.money(p)).font(.caption2)
                                        .foregroundStyle(p >= 0 ? .green : .red)
                                }
                                Text(Fmt.relative(c.started_at)).font(.caption2).foregroundStyle(.tertiary)
                            }
                        }
                    }
                }
            }

            if let li = s.live_impact {
                Section("Live impact") {
                    impactRow("Patterns", li.patterns)
                    impactRow("Regressions", li.regressions)
                    impactRow("Trades", li.trades)
                    impactRow("Reasoning journal", li.reasoning_journal)
                }
            }
            ErrorBox(message: error)
        }
        .navigationTitle(s.symbol)
        .refreshable { if let sym = selected { await loadSummary(sym) } }
    }

    @ViewBuilder private func impactRow(_ label: String, _ b: SymbolImpactBlock?) -> some View {
        if let b {
            HStack {
                Image(systemName: b.in_decision_loop ? "checkmark.circle.fill" : "circle")
                    .foregroundStyle(b.in_decision_loop ? .green : .secondary)
                VStack(alignment: .leading) {
                    Text(label).font(.subheadline)
                    if let w = b.`where`, !w.isEmpty {
                        Text(w).font(.caption2).foregroundStyle(.secondary)
                    }
                    if let f = b.feature_flag {
                        Text("flag: \(f)").font(.caption2).foregroundStyle(.tertiary)
                    }
                }
            }
        }
    }

    private func pick(_ symbol: String) async {
        selected = symbol
        await loadSummary(symbol)
    }

    private func loadList() async {
        loadingList = true; defer { loadingList = false }
        do {
            let r: SymbolListResponse = try await session.api.request("/symbols/list", as: SymbolListResponse.self)
            self.list = r.items
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }

    private func loadSummary(_ symbol: String) async {
        loadingSummary = true; defer { loadingSummary = false }
        do {
            self.summary = try await session.api.request("/symbols/\(symbol)/summary", as: SymbolSummary.self)
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }
}
