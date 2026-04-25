import SwiftUI

struct CyclesView: View {
    @EnvironmentObject var session: SessionStore
    @State private var cycles: [CycleSummary] = []
    @State private var hours: Int = 24
    @State private var loading = false
    @State private var error: String?

    var body: some View {
        List {
            Section {
                Picker("Range", selection: $hours) {
                    Text("6h").tag(6)
                    Text("24h").tag(24)
                    Text("7d").tag(168)
                }
                .pickerStyle(.segmented)
                .onChange(of: hours) { _, _ in Task { await load() } }
            }

            if cycles.isEmpty, !loading {
                ContentUnavailableView("No cycles", systemImage: "brain")
            }

            ForEach(cycles) { c in
                NavigationLink {
                    CycleDetailView(cycleId: c.cycle_id)
                } label: {
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text(c.pair).font(.headline)
                            Spacer()
                            if let action = c.action {
                                Text(action.uppercased())
                                    .font(.caption2.weight(.bold))
                                    .padding(.horizontal, 6).padding(.vertical, 2)
                                    .background(Fmt.badgeColor(forAction: action).opacity(0.2))
                                    .foregroundStyle(Fmt.badgeColor(forAction: action))
                                    .cornerRadius(4)
                            }
                        }
                        HStack {
                            Text(Fmt.short(c.started_at))
                                .font(.caption).foregroundStyle(.secondary)
                            Spacer()
                            if let conf = c.confidence {
                                Text("conf \(Fmt.pct(conf * 100, decimals: 1))")
                                    .font(.caption2).foregroundStyle(.secondary)
                            }
                            if let dur = c.cycle_duration_ms {
                                Text("\(dur)ms")
                                    .font(.caption2).foregroundStyle(.secondary)
                            }
                        }
                        HStack {
                            Text("\(c.agent_count) agents")
                                .font(.caption2).foregroundStyle(.secondary)
                            if let pt = c.total_prompt_tokens, let ct = c.total_completion_tokens {
                                Text("· \(pt + ct) tok")
                                    .font(.caption2).foregroundStyle(.secondary)
                            }
                            if let pnl = c.pnl {
                                Spacer()
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
        .navigationTitle("Cycles")
        .refreshable { await load() }
        .task { await load() }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            let resp: CyclesResponse = try await session.api.request(
                "/cycles",
                query: [URLQueryItem(name: "hours", value: "\(hours)")],
                as: CyclesResponse.self
            )
            self.cycles = resp.cycles
            self.error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}

struct CycleDetailView: View {
    @EnvironmentObject var session: SessionStore
    let cycleId: String
    @State private var cycle: CycleFull?
    @State private var error: String?

    var body: some View {
        List {
            if let c = cycle {
                Section("Decision") {
                    LabeledContent("Pair", value: c.pair)
                    LabeledContent("Outcome", value: c.decision_outcome)
                    if !c.decision_reason.isEmpty {
                        Text(c.decision_reason).font(.callout)
                    }
                }
                Section("Stats") {
                    LabeledContent("Latency", value: "\(c.total_latency_ms) ms")
                    LabeledContent("Tokens", value: "\(c.total_tokens)")
                    LabeledContent("Started", value: Fmt.short(c.started_at))
                    LabeledContent("Finished", value: Fmt.short(c.finished_at))
                    if let url = c.langfuse_url, let u = URL(string: url) {
                        Link("View in Langfuse", destination: u)
                    }
                }
                Section("Agents (\(c.spans.count))") {
                    ForEach(c.spans) { span in
                        VStack(alignment: .leading, spacing: 4) {
                            HStack {
                                Text(span.agent_name).font(.headline)
                                Spacer()
                                if let lat = span.latency_ms {
                                    Text("\(lat) ms").font(.caption).foregroundStyle(.secondary)
                                }
                            }
                            if let signal = span.signal_type {
                                Text("Signal: \(signal)").font(.caption).foregroundStyle(.secondary)
                            }
                            if let r = span.reasoning_json?.displayString() {
                                Text(r)
                                    .font(.caption2)
                                    .lineLimit(8)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                }
                if let trade = c.trade {
                    Section("Resulting Trade") {
                        LabeledContent("Action", value: trade.action.uppercased())
                        LabeledContent("Price", value: Fmt.money(trade.price, decimals: 6))
                        LabeledContent("Amount", value: Fmt.money(trade.quote_amount))
                        if let pnl = trade.pnl { LabeledContent("PnL", value: Fmt.money(pnl)) }
                    }
                }
            } else if error == nil {
                ProgressView()
            }
            ErrorBox(message: error)
        }
        .navigationTitle("Cycle")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
    }

    private func load() async {
        do {
            let c: CycleFull = try await session.api.request("/cycles/\(cycleId)", as: CycleFull.self)
            self.cycle = c
            self.error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}
