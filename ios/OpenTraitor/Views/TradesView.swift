import SwiftUI

struct TradesView: View {
    @EnvironmentObject var session: SessionStore
    @State private var trades: [Trade] = []
    @State private var hours: Int = 24
    @State private var loading = false
    @State private var syncing = false
    @State private var error: String?

    var body: some View {
        List {
            Section {
                Picker("Range", selection: $hours) {
                    Text("6h").tag(6)
                    Text("24h").tag(24)
                    Text("7d").tag(168)
                    Text("30d").tag(720)
                }
                .pickerStyle(.segmented)
                .onChange(of: hours) { _, _ in Task { await load() } }

                if let url = session.api.externalURL("/trades/export",
                                                    query: [URLQueryItem(name: "hours", value: "\(hours)")]) {
                    Link("Export CSV", destination: url)
                }
            }

            if trades.isEmpty, !loading {
                ContentUnavailableView("No trades", systemImage: "tray")
            }

            ForEach(trades) { t in
                NavigationLink {
                    TradeDetailView(trade: t)
                } label: {
                    HStack {
                        VStack(alignment: .leading) {
                            HStack {
                                Text(t.action.uppercased())
                                    .font(.caption2.weight(.bold))
                                    .padding(.horizontal, 6).padding(.vertical, 2)
                                    .background(Fmt.badgeColor(forAction: t.action).opacity(0.2))
                                    .foregroundStyle(Fmt.badgeColor(forAction: t.action))
                                    .cornerRadius(4)
                                Text(t.pair).font(.headline)
                            }
                            Text(Fmt.short(t.ts))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        VStack(alignment: .trailing) {
                            Text(Fmt.money(t.quote_amount))
                            if let pnl = t.pnl {
                                Text(Fmt.money(pnl))
                                    .font(.caption)
                                    .foregroundStyle(pnl >= 0 ? .green : .red)
                            }
                        }
                    }
                }
            }

            ErrorBox(message: error)
        }
        .navigationTitle("Trades")
        .toolbar {
            ToolbarItem {
                Button {
                    Task { await sync() }
                } label: {
                    if syncing { ProgressView() } else { Image(systemName: "arrow.triangle.2.circlepath") }
                }.disabled(syncing)
            }
        }
        .refreshable { await load() }
        .task { await load() }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            let resp: TradesResponse = try await session.api.request(
                "/trades",
                query: [URLQueryItem(name: "hours", value: "\(hours)")],
                as: TradesResponse.self
            )
            self.trades = resp.trades
            self.error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func sync() async {
        syncing = true
        defer { syncing = false }
        do {
            _ = try await session.api.request(
                "/trades/sync",
                method: "POST",
                as: SyncResponse.self
            )
            await load()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

struct TradeDetailView: View {
    let trade: Trade

    var body: some View {
        Form {
            Section("Trade") {
                LabeledContent("Pair", value: trade.pair)
                LabeledContent("Action", value: trade.action.uppercased())
                LabeledContent("Time", value: Fmt.short(trade.ts))
                LabeledContent("Price", value: Fmt.money(trade.price, decimals: 6))
                LabeledContent("Quantity", value: Fmt.num(trade.quantity, decimals: 6))
                LabeledContent("Quote amount", value: Fmt.money(trade.quote_amount))
                if let pnl = trade.pnl {
                    LabeledContent("PnL", value: Fmt.money(pnl))
                }
                if let conf = trade.confidence {
                    LabeledContent("Confidence", value: Fmt.pct(conf * 100))
                }
                if let signal = trade.signal_type {
                    LabeledContent("Signal", value: signal)
                }
                if let sl = trade.stop_loss {
                    LabeledContent("Stop loss", value: Fmt.money(sl, decimals: 6))
                }
                if let tp = trade.take_profit {
                    LabeledContent("Take profit", value: Fmt.money(tp, decimals: 6))
                }
                if let cycle = trade.cycle_id {
                    LabeledContent("Cycle", value: cycle)
                }
                if let approver = trade.approved_by {
                    LabeledContent("Approved by", value: approver)
                }
            }
            if let reasoning = trade.reasoning, !reasoning.isEmpty {
                Section("Reasoning") {
                    Text(reasoning).font(.callout)
                }
            }
        }
        .navigationTitle(trade.pair)
    }
}
