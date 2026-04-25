import SwiftUI
import Charts

struct LLMAnalyticsView: View {
    @EnvironmentObject var session: SessionStore
    @State private var data: LLMAnalyticsData?
    @State private var optimizer: OptimizerData?
    @State private var hours: Int = 24
    @State private var loading = false
    @State private var error: String?
    @State private var applying = false

    var body: some View {
        List {
            Picker("Range", selection: $hours) {
                Text("24h").tag(24)
                Text("7d").tag(168)
                Text("30d").tag(720)
            }
            .pickerStyle(.segmented)
            .onChange(of: hours) { _, _ in Task { await load() } }

            if let s = data?.summary {
                Section("Summary") {
                    LabeledContent("Calls", value: "\(s.total_calls)")
                    LabeledContent("Tokens", value: "\(s.total_tokens)")
                    LabeledContent("Cycles", value: "\(s.total_cycles)")
                    LabeledContent("Pairs", value: "\(s.unique_pairs)")
                    if let avg = s.avg_latency_ms {
                        LabeledContent("Avg latency", value: "\(Int(avg)) ms")
                    }
                    if let p90 = s.p90_latency_ms {
                        LabeledContent("p90 latency", value: "\(Int(p90)) ms")
                    }
                    if let p99 = s.p99_latency_ms {
                        LabeledContent("p99 latency", value: "\(Int(p99)) ms")
                    }
                }
            }

            if let series = data?.time_series, !series.isEmpty {
                Section("Tokens over time") {
                    Chart(series) { b in
                        BarMark(x: .value("bucket", b.bucket),
                                y: .value("tokens", b.total_tokens))
                    }.frame(height: 200)
                }
            }

            if let agents = data?.by_agent, !agents.isEmpty {
                Section("By agent") {
                    ForEach(agents) { a in
                        HStack {
                            Text(a.agent_name).font(.headline)
                            Spacer()
                            Text("\(a.calls) calls").font(.caption).foregroundStyle(.secondary)
                            Text("\(a.total_tokens) tok").font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
            }

            if let pairs = data?.top_pairs, !pairs.isEmpty {
                Section("Top pairs") {
                    ForEach(pairs) { p in
                        HStack {
                            Text(p.pair)
                            Spacer()
                            Text("\(p.calls)").font(.caption).foregroundStyle(.secondary)
                            Text("\(p.total_tokens) tok").font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
            }

            if let providers = data?.providers, !providers.isEmpty {
                Section("Providers") {
                    ForEach(providers) { p in
                        HStack {
                            Text(p.provider)
                            Spacer()
                            if let lat = p.avg_latency_ms {
                                Text("\(Int(lat))ms").font(.caption).foregroundStyle(.secondary)
                            }
                            Text("\(p.calls) calls").font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
            }

            if let opt = optimizer {
                Section("Optimizer") {
                    Text(opt.settings.displayString())
                        .font(.caption.monospaced())
                    Button {
                        Task { await applyOptimizer() }
                    } label: {
                        HStack {
                            Text("Apply current settings")
                            Spacer()
                            if applying { ProgressView() }
                        }
                    }
                    .disabled(applying)
                }
            }

            ErrorBox(message: error)
        }
        .navigationTitle("LLM analytics")
        .refreshable { await load() }
        .task { await load() }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            async let a = try await session.api.request(
                "/llm-analytics",
                query: [URLQueryItem(name: "hours", value: "\(hours)")],
                as: LLMAnalyticsData.self
            )
            async let o = try? await session.api.request("/llm-analytics/optimizer", as: OptimizerData.self)
            self.data = try await a
            self.optimizer = await o
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }

    private func applyOptimizer() async {
        guard case .object(let dict)? = optimizer?.settings else { return }
        applying = true
        defer { applying = false }
        do {
            _ = try await session.api.sendWithConfirmation(
                "/llm-analytics/optimizer/apply",
                method: "POST",
                body: OptimizerApplyBody(settings: dict),
                as: OptimizerApplyResp.self
            )
        } catch { self.error = error.localizedDescription }
    }
}
