import SwiftUI
import Charts

struct AnalyticsView: View {
    @EnvironmentObject var session: SessionStore
    @State private var data: AnalyticsData?
    @State private var history: [PortfolioSnapshot] = []
    @State private var loading = false
    @State private var error: String?

    var body: some View {
        List {
            if let d = data {
                Section("Trades") {
                    LabeledContent("Total", value: "\(d.performance.trade_stats.total_trades)")
                    LabeledContent("Winning", value: "\(d.performance.trade_stats.winning)")
                    LabeledContent("Losing", value: "\(d.performance.trade_stats.losing)")
                    LabeledContent("Win rate", value: Fmt.pct(d.win_loss.win_rate))
                    LabeledContent("Avg win", value: Fmt.money(d.win_loss.avg_win))
                    LabeledContent("Avg loss", value: Fmt.money(d.win_loss.avg_loss))
                    LabeledContent("Total PnL", value: Fmt.money(d.performance.trade_stats.total_pnl))
                    LabeledContent("Total fees", value: Fmt.money(d.performance.trade_stats.total_fees))
                    LabeledContent("Avg confidence", value: Fmt.pct(d.performance.trade_stats.avg_confidence * 100))
                }
                Section("Portfolio range") {
                    LabeledContent("Low", value: Fmt.money(d.portfolio_range.low))
                    LabeledContent("High", value: Fmt.money(d.portfolio_range.high))
                    LabeledContent("Avg", value: Fmt.money(d.portfolio_range.avg))
                }
                if !history.isEmpty {
                    Section("Equity curve") {
                        Chart {
                            ForEach(history) { snap in
                                LineMark(
                                    x: .value("ts", snap.ts),
                                    y: .value("value", snap.portfolio_value)
                                )
                            }
                        }.frame(height: 220)
                    }
                }
                Section("Best trades") {
                    ForEach(d.best_worst.best.prefix(5)) { b in
                        HStack {
                            Text(b.pair)
                            Spacer()
                            Text(Fmt.money(b.pnl)).foregroundStyle(.green)
                        }
                    }
                }
                Section("Worst trades") {
                    ForEach(d.best_worst.worst.prefix(5)) { b in
                        HStack {
                            Text(b.pair)
                            Spacer()
                            Text(Fmt.money(b.pnl)).foregroundStyle(.red)
                        }
                    }
                }
                Section("Daily summaries") {
                    ForEach(d.daily_summaries.prefix(10)) { ds in
                        VStack(alignment: .leading) {
                            HStack {
                                Text(ds.date).font(.headline)
                                Spacer()
                                Text(Fmt.money(ds.total_pnl))
                                    .foregroundStyle(ds.total_pnl >= 0 ? .green : .red)
                            }
                            Text("\(ds.total_trades) trades · open \(Fmt.money(ds.opening_value)) → close \(Fmt.money(ds.closing_value))")
                                .font(.caption).foregroundStyle(.secondary)
                            if let s = ds.summary_text, !s.isEmpty {
                                Text(s).font(.caption2).foregroundStyle(.tertiary)
                            }
                        }
                    }
                }
            }
            ErrorBox(message: error)
        }
        .navigationTitle("Analytics")
        .refreshable { await load() }
        .task { await load() }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            async let a = try await session.api.request("/analytics", as: AnalyticsData.self)
            async let h = try await session.api.request(
                "/portfolio/history",
                query: [URLQueryItem(name: "hours", value: "168")],
                as: PortfolioHistoryResponse.self
            )
            self.data = try await a
            self.history = try await h.history
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }
}
