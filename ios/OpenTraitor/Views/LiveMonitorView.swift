import SwiftUI

struct LiveMonitorView: View {
    @EnvironmentObject var session: SessionStore
    @StateObject private var socket = LiveSocket()
    @State private var exposure: PortfolioExposure?
    @State private var stops: [String: TrailingStopData] = [:]
    @State private var commands: [TradeCommand] = []
    @State private var pollTask: Task<Void, Never>?

    var body: some View {
        List {
            Section("Live stream") {
                HStack {
                    Circle()
                        .fill(socket.isConnected ? Color.green : Color.gray)
                        .frame(width: 10, height: 10)
                    Text(socket.isConnected ? "Connected" : "Disconnected")
                        .font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    if socket.events.isEmpty {
                        Text("Waiting for events…").font(.caption).foregroundStyle(.tertiary)
                    } else {
                        Text("\(socket.events.count) events").font(.caption).foregroundStyle(.secondary)
                    }
                }
                if let err = socket.lastError {
                    Text(err).font(.caption2).foregroundStyle(.red)
                }
                ForEach(Array(socket.events.prefix(50).enumerated()), id: \.offset) { _, ev in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(ev.agent_name ?? ev.type).font(.caption.weight(.semibold))
                            Spacer()
                            if let lat = ev.latency_ms {
                                Text("\(Int(lat))ms").font(.caption2).foregroundStyle(.secondary)
                            }
                        }
                        HStack {
                            if let pair = ev.pair {
                                Text(pair).font(.caption2).foregroundStyle(.secondary)
                            }
                            if let pt = ev.prompt_tokens, let ct = ev.completion_tokens {
                                Text("\(pt + ct) tok").font(.caption2).foregroundStyle(.tertiary)
                            }
                            if let model = ev.model {
                                Text(model).font(.caption2).foregroundStyle(.tertiary)
                            }
                            Spacer()
                            Text(Fmt.relative(ev.ts)).font(.caption2).foregroundStyle(.tertiary)
                        }
                    }
                }
            }

            if let e = exposure {
                Section("Exposure") {
                    LabeledContent("Cash", value: Fmt.pct(e.cash_pct))
                    LabeledContent("Allocated", value: Fmt.pct(e.allocated_pct))
                    LabeledContent("PnL", value: Fmt.money(e.total_pnl))
                }
            }

            if !stops.isEmpty {
                Section("Trailing stops") {
                    ForEach(stops.keys.sorted(), id: \.self) { pair in
                        if let s = stops[pair] {
                            VStack(alignment: .leading) {
                                Text(pair).font(.headline)
                                HStack {
                                    Text("Stop: \(Fmt.money(s.stop_price, decimals: 6))")
                                    Spacer()
                                    if s.triggered == true {
                                        Text("TRIGGERED").foregroundStyle(.red).font(.caption2.weight(.bold))
                                    }
                                }.font(.caption)
                            }
                        }
                    }
                }
            }

            if !commands.isEmpty {
                Section("Recent commands") {
                    ForEach(commands.prefix(10)) { c in
                        HStack {
                            Text(c.pair).font(.headline)
                            Spacer()
                            Text(c.action.uppercased()).font(.caption.weight(.bold))
                                .foregroundStyle(.orange)
                            Text(Fmt.relative(c.ts)).font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
        .navigationTitle("Live")
        .task {
            socket.attach(api: session.api)
            socket.connect()
            await pollLoop()
        }
        .onDisappear {
            socket.disconnect()
            pollTask?.cancel()
        }
    }

    private func pollLoop() async {
        pollTask?.cancel()
        let task = Task {
            while !Task.isCancelled {
                async let exp = try? await session.api.request("/portfolio/exposure", as: ExposureResponse.self)
                async let st = try? await session.api.request("/trailing-stops", as: TrailingStopsResp.self)
                async let cm = try? await session.api.request("/trade/commands/history", as: CommandsHistoryResp.self)
                let (e, s, c) = await (exp, st, cm)
                await MainActor.run {
                    exposure = e?.exposure
                    stops = s?.stops ?? [:]
                    commands = c?.commands ?? []
                }
                try? await Task.sleep(nanoseconds: 5_000_000_000)
            }
        }
        pollTask = task
        await task.value
    }
}
