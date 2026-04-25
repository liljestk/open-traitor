import SwiftUI

struct RiskExposureView: View {
    @EnvironmentObject var session: SessionStore
    @State private var exposure: PortfolioExposure?
    @State private var stops: [String: TrailingStopData] = [:]
    @State private var loading = false
    @State private var error: String?

    var body: some View {
        List {
            if let e = exposure {
                Section("Risk metrics") {
                    LabeledContent("Portfolio", value: Fmt.money(e.portfolio_value))
                    LabeledContent("Cash", value: Fmt.money(e.cash_balance))
                    LabeledContent("Cash %", value: Fmt.pct(e.cash_pct))
                    LabeledContent("Allocated %", value: Fmt.pct(e.allocated_pct))
                    LabeledContent("Total PnL", value: Fmt.money(e.total_pnl))
                    LabeledContent("Drawdown", value: Fmt.pct(e.max_drawdown))
                    LabeledContent("Return %", value: Fmt.pct(e.return_pct))
                    if let fg = e.fear_greed_value {
                        LabeledContent("Fear & Greed", value: Fmt.num(fg, decimals: 0))
                    }
                }
                Section("Holdings") {
                    if e.breakdown.isEmpty {
                        Text("No open positions").foregroundStyle(.secondary)
                    }
                    ForEach(e.breakdown) { b in
                        VStack(alignment: .leading) {
                            HStack {
                                Text(b.pair).font(.headline)
                                Spacer()
                                Text(Fmt.money(b.value))
                            }
                            HStack {
                                Text("Entry \(Fmt.money(b.entry_price, decimals: 6))")
                                Spacer()
                                Text("Now \(Fmt.money(b.current_price, decimals: 6))")
                            }.font(.caption).foregroundStyle(.secondary)
                            HStack {
                                Text("Allocation: \(Fmt.pct(b.pct_of_portfolio))")
                                    .font(.caption).foregroundStyle(.secondary)
                                Spacer()
                                Text(Fmt.pct(b.pnl_pct))
                                    .foregroundStyle(b.pnl_pct >= 0 ? .green : .red)
                                    .font(.caption.weight(.bold))
                            }
                        }
                    }
                }
            }
            if !stops.isEmpty {
                Section("Trailing stops") {
                    ForEach(stops.keys.sorted(), id: \.self) { pair in
                        if let s = stops[pair] {
                            VStack(alignment: .leading, spacing: 4) {
                                HStack {
                                    Text(pair).font(.headline)
                                    Spacer()
                                    if s.triggered == true {
                                        Text("TRIGGERED")
                                            .font(.caption2.weight(.bold))
                                            .foregroundStyle(.red)
                                    }
                                }
                                if let entry = s.entry_price, let stop = s.stop_price {
                                    Text("Entry \(Fmt.money(entry, decimals: 6)) → Stop \(Fmt.money(stop, decimals: 6))")
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                                if let trail = s.trail_pct {
                                    Text("Trail: \(Fmt.pct(trail))").font(.caption2).foregroundStyle(.secondary)
                                }
                                if let tiers = s.tiers, !tiers.isEmpty {
                                    ForEach(tiers.indices, id: \.self) { i in
                                        let t = tiers[i]
                                        HStack {
                                            Text("Tier \(i+1):")
                                            if let trig = t.trigger_pct { Text(Fmt.pct(trig * 100)) }
                                            if let frac = t.exit_fraction {
                                                Text("→ exit \(Fmt.pct(frac * 100))").foregroundStyle(.secondary)
                                            }
                                            Spacer()
                                            if t.triggered == true {
                                                Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                                            }
                                        }.font(.caption2)
                                    }
                                }
                            }
                        }
                    }
                }
            }
            ErrorBox(message: error)
        }
        .navigationTitle("Risk")
        .refreshable { await load() }
        .task { await load() }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            async let exp = try await session.api.request("/portfolio/exposure", as: ExposureResponse.self)
            async let st = try await session.api.request("/trailing-stops", as: TrailingStopsResp.self)
            self.exposure = try await exp.exposure
            self.stops = try await st.stops
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }
}
