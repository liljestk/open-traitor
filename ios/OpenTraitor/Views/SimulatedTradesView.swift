import SwiftUI

struct SimulatedTradesView: View {
    @EnvironmentObject var session: SessionStore
    @State private var trades: [SimulatedTrade] = []
    @State private var loading = false
    @State private var error: String?
    @State private var showCreate = false

    var body: some View {
        List {
            Section {
                Button {
                    showCreate = true
                } label: { Label("Add paper trade", systemImage: "plus.circle") }
            }
            if trades.isEmpty, !loading {
                ContentUnavailableView("No simulations", systemImage: "doc.text")
            }
            ForEach(trades) { t in
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text(t.pair).font(.headline)
                        Spacer()
                        Text(t.status.uppercased())
                            .font(.caption2.weight(.bold))
                            .foregroundStyle(t.status == "open" ? .blue : .gray)
                    }
                    HStack {
                        Text("Entry \(Fmt.money(t.entry_price, decimals: 6))")
                        Spacer()
                        Text("Now \(Fmt.money(t.current_price, decimals: 6))")
                    }.font(.caption).foregroundStyle(.secondary)
                    HStack {
                        Text("PnL \(Fmt.money(t.pnl_abs))")
                            .foregroundStyle(t.pnl_abs >= 0 ? .green : .red)
                        Text(Fmt.pct(t.pnl_pct))
                            .foregroundStyle(t.pnl_pct >= 0 ? .green : .red)
                    }.font(.caption.weight(.bold))
                    if let n = t.notes, !n.isEmpty {
                        Text(n).font(.caption2).foregroundStyle(.tertiary)
                    }
                }
                .swipeActions {
                    if t.status == "open" {
                        Button("Close") { Task { await close(t.id) } }.tint(.orange)
                    }
                    Button(role: .destructive) {
                        Task { await delete(t.id) }
                    } label: { Label("Delete", systemImage: "trash") }
                }
            }
            ErrorBox(message: error)
        }
        .navigationTitle("Paper trades")
        .refreshable { await load() }
        .task { await load() }
        .sheet(isPresented: $showCreate) {
            CreateSimulatedTradeView { await load() }
        }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            let r: SimulatedTradesResp = try await session.api.request("/simulated-trades", as: SimulatedTradesResp.self)
            self.trades = r.simulations
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }

    private func close(_ id: Int) async {
        do {
            _ = try await session.api.request(
                "/simulated-trades/\(id)/close",
                method: "POST",
                as: EmptyResponse.self
            )
            await load()
        } catch { self.error = error.localizedDescription }
    }

    private func delete(_ id: Int) async {
        do {
            _ = try await session.api.request(
                "/simulated-trades/\(id)",
                method: "DELETE",
                as: EmptyResponse.self
            )
            await load()
        } catch { self.error = error.localizedDescription }
    }
}

struct CreateSimulatedTradeView: View {
    @EnvironmentObject var session: SessionStore
    @Environment(\.dismiss) private var dismiss
    let onSubmit: () async -> Void

    @State private var pair = ""
    @State private var fromCurrency = "USD"
    @State private var amount: Double = 100
    @State private var notes = ""
    @State private var submitting = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                TextField("Pair (e.g. BTC-USD)", text: $pair)
                    .textInputAutocapitalization(.characters)
                    .autocorrectionDisabled()
                TextField("From currency", text: $fromCurrency)
                    .textInputAutocapitalization(.characters)
                    .autocorrectionDisabled()
                LabeledStepper("Amount", value: $amount, range: 1...100000, step: 10)
                TextField("Notes", text: $notes, axis: .vertical)
                    .lineLimit(3...6)
                ErrorBox(message: error)
                Button {
                    Task { await submit() }
                } label: {
                    HStack {
                        Text("Create")
                        Spacer()
                        if submitting { ProgressView() }
                    }
                }
                .disabled(submitting || pair.isEmpty)
            }
            .navigationTitle("Paper trade")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") { dismiss() }
                }
            }
        }
    }

    private func submit() async {
        submitting = true
        defer { submitting = false }
        do {
            _ = try await session.api.request(
                "/simulated-trades",
                method: "POST",
                body: CreateSimulatedBody(
                    pair: pair,
                    from_currency: fromCurrency,
                    from_amount: amount,
                    notes: notes.isEmpty ? nil : notes
                ),
                as: EmptyResponse.self
            )
            await onSubmit()
            dismiss()
        } catch { self.error = error.localizedDescription }
    }
}
