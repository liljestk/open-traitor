import SwiftUI

struct OverviewView: View {
    @EnvironmentObject var session: SessionStore
    @State private var stats: StatsSummary?
    @State private var exposure: PortfolioExposure?
    @State private var executive: ExecutiveSummaryResponse?
    @State private var loading = false
    @State private var error: String?

    var body: some View {
        List {
            if let stats {
                Section("Today") {
                    metric("Total trades", value: "\(stats.total_trades)")
                    metric("24h trades", value: "\(stats.trades_24h)")
                    metric("Cycles 24h", value: "\(stats.cycles_24h)")
                    metric("Win rate", value: Fmt.pct(stats.win_rate))
                    metric("Total PnL", value: Fmt.money(stats.total_pnl))
                    metric("PnL 24h", value: Fmt.money(stats.pnl_24h))
                }
            }

            if let p = stats?.portfolio {
                Section("Portfolio") {
                    metric("Value", value: Fmt.money(p.portfolio_value))
                    metric("Total PnL", value: Fmt.money(p.total_pnl))
                    metric("As of", value: Fmt.short(p.ts))
                }
            }

            if let exposure {
                Section("Exposure") {
                    metric("Cash", value: Fmt.pct(exposure.cash_pct))
                    metric("Allocated", value: Fmt.pct(exposure.allocated_pct))
                    metric("Drawdown", value: Fmt.pct(exposure.max_drawdown))
                    if let fg = exposure.fear_greed_value {
                        metric("Fear & Greed", value: Fmt.num(fg, decimals: 0))
                    }
                    if let hs = exposure.high_stakes_active {
                        metric("High stakes", value: hs ? "ACTIVE" : "off")
                    }
                }
                if !exposure.breakdown.isEmpty {
                    Section("Holdings") {
                        ForEach(exposure.breakdown) { row in
                            HStack {
                                VStack(alignment: .leading) {
                                    Text(row.pair).font(.headline)
                                    Text("Qty: \(Fmt.num(row.quantity, decimals: 4))")
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer()
                                VStack(alignment: .trailing) {
                                    Text(Fmt.money(row.value))
                                    Text(Fmt.pct(row.pnl_pct))
                                        .foregroundStyle(row.pnl_pct >= 0 ? .green : .red)
                                        .font(.caption)
                                }
                            }
                        }
                    }
                }
            }

            if let exec = executive {
                Section("Across all profiles") {
                    metric("Combined trades", value: "\(exec.combined.total_trades)")
                    metric("Combined PnL", value: Fmt.money(exec.combined.total_pnl))
                    metric("Active pairs 24h", value: "\(exec.combined.total_active_pairs_24h)")
                    ForEach(exec.profiles) { p in
                        HStack {
                            Text(p.profile.capitalized)
                            Spacer()
                            Text(Fmt.money(p.pnl))
                                .foregroundStyle(p.pnl >= 0 ? .green : .red)
                        }
                    }
                }
            }

            ErrorBox(message: error)
        }
        .navigationTitle("Overview")
        .toolbar {
            ToolbarItem {
                Button {
                    Task { await load() }
                } label: { Image(systemName: "arrow.clockwise") }
                .disabled(loading)
            }
        }
        .refreshable { await load() }
        .task { await load() }
    }

    private func metric(_ label: String, value: String) -> some View {
        HStack {
            Text(label).foregroundStyle(.secondary)
            Spacer()
            Text(value).fontDesign(.monospaced)
        }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            async let s: StatsSummary = session.api.request("/stats/summary", as: StatsSummary.self)
            async let e: ExposureResponse = session.api.request("/portfolio/exposure", as: ExposureResponse.self)
            async let x: ExecutiveSummaryResponse = session.api.request("/executive_summary", injectProfile: false, as: ExecutiveSummaryResponse.self)
            let (stats, exp, exec) = try await (s, e, x)
            self.stats = stats
            self.exposure = exp.exposure
            self.executive = exec
            self.error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}
