import SwiftUI

struct BacktestingView: View {
    @EnvironmentObject var session: SessionStore
    @State private var runs: [BacktestRunSummary] = []
    @State private var availablePairs: [String] = []
    @State private var loading = false
    @State private var error: String?
    @State private var showTrigger = false

    var body: some View {
        List {
            Section {
                Button {
                    showTrigger = true
                } label: {
                    Label("Run new backtest", systemImage: "play.circle")
                }
            }
            if runs.isEmpty, !loading {
                ContentUnavailableView("No runs yet", systemImage: "tray")
            }
            ForEach(runs) { run in
                NavigationLink {
                    BacktestDetailView(runId: run.id)
                } label: {
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text(run.pair).font(.headline)
                            Spacer()
                            if let r = run.total_return_pct {
                                Text(Fmt.pct(r))
                                    .foregroundStyle(r >= 0 ? .green : .red)
                                    .font(.caption.weight(.bold))
                            }
                        }
                        HStack(spacing: 8) {
                            Text(Fmt.short(run.run_ts)).font(.caption2).foregroundStyle(.secondary)
                            Text("\(run.days)d").font(.caption2).foregroundStyle(.secondary)
                            if let s = run.sharpe_ratio { Text("Sharpe \(Fmt.num(s))").font(.caption2) }
                            if let w = run.win_rate { Text("Win \(Fmt.pct(w))").font(.caption2) }
                            if let dd = run.max_drawdown_pct { Text("DD \(Fmt.pct(dd))").font(.caption2).foregroundStyle(.red) }
                        }
                    }
                }
            }
            ErrorBox(message: error)
        }
        .navigationTitle("Backtesting")
        .refreshable { await load() }
        .task { await load() }
        .sheet(isPresented: $showTrigger) {
            BacktestTriggerView(pairs: availablePairs) {
                Task { await load() }
            }
        }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            async let h = try await session.api.request("/backtesting/history", as: BacktestHistoryResponse.self)
            async let p = try await session.api.request("/backtesting/pairs", as: BacktestPairsResp.self)
            self.runs = try await h.runs
            self.availablePairs = try await p.pairs
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }
}

struct BacktestDetailView: View {
    @EnvironmentObject var session: SessionStore
    let runId: Int
    @State private var run: BacktestRunDetail?
    @State private var error: String?

    var body: some View {
        List {
            if let r = run {
                Section("Run") {
                    LabeledContent("Pair", value: r.pair)
                    LabeledContent("Days", value: "\(r.days)")
                    LabeledContent("Return", value: Fmt.pct(r.total_return_pct))
                    LabeledContent("Sharpe", value: Fmt.num(r.sharpe_ratio))
                    LabeledContent("Win rate", value: Fmt.pct(r.win_rate))
                    LabeledContent("Trades", value: "\(r.total_trades ?? 0)")
                    LabeledContent("Drawdown", value: Fmt.pct(r.max_drawdown_pct))
                }
                if let p = r.params_json?.displayString() {
                    Section("Parameters") {
                        Text(p).font(.caption.monospaced())
                    }
                }
                if let res = r.result_json?.displayString() {
                    Section("Result") {
                        Text(res).font(.caption.monospaced())
                    }
                }
            } else if error == nil {
                ProgressView()
            }
            ErrorBox(message: error)
        }
        .navigationTitle("Run #\(runId)")
        .task { await load() }
    }

    private func load() async {
        do {
            self.run = try await session.api.request("/backtesting/run/\(runId)", as: BacktestRunDetail.self)
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }
}

struct BacktestTriggerView: View {
    @EnvironmentObject var session: SessionStore
    @Environment(\.dismiss) private var dismiss
    let pairs: [String]
    let onSubmit: () -> Void

    @State private var pair = ""
    @State private var days = 30
    @State private var positionPct = 5.0
    @State private var trailing = 2.0
    @State private var entryThreshold = 0.6
    @State private var feePct = 0.5
    @State private var slippagePct = 0.05
    @State private var submitting = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                Picker("Pair", selection: $pair) {
                    Text("Choose…").tag("")
                    ForEach(pairs, id: \.self) { Text($0).tag($0) }
                }
                Stepper("Days: \(days)", value: $days, in: 7...365, step: 7)
                LabeledStepper("Position %", value: $positionPct, range: 1...100, step: 1)
                LabeledStepper("Trailing stop %", value: $trailing, range: 0.5...20, step: 0.5)
                LabeledStepper("Entry threshold", value: $entryThreshold, range: 0.1...1, step: 0.05)
                LabeledStepper("Fee %", value: $feePct, range: 0...2, step: 0.05)
                LabeledStepper("Slippage %", value: $slippagePct, range: 0...1, step: 0.05)
                ErrorBox(message: error)
                Button {
                    Task { await submit() }
                } label: {
                    HStack {
                        Text("Run backtest")
                        Spacer()
                        if submitting { ProgressView() }
                    }
                }
                .disabled(submitting || pair.isEmpty)
            }
            .navigationTitle("New backtest")
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
            let body = BacktestTriggerRequest(
                pair: pair, days: days,
                position_size_pct: positionPct,
                trailing_stop_pct: trailing,
                entry_threshold: entryThreshold,
                fee_pct: feePct,
                slippage_pct: slippagePct
            )
            _ = try await session.api.request(
                "/backtesting/trigger", method: "POST", body: body,
                as: BacktestTriggerResp.self
            )
            onSubmit()
            dismiss()
        } catch { self.error = error.localizedDescription }
    }
}

struct LabeledStepper: View {
    let title: String
    @Binding var value: Double
    let range: ClosedRange<Double>
    let step: Double

    init(_ title: String, value: Binding<Double>, range: ClosedRange<Double>, step: Double) {
        self.title = title
        self._value = value
        self.range = range
        self.step = step
    }

    var body: some View {
        Stepper(value: $value, in: range, step: step) {
            HStack {
                Text(title)
                Spacer()
                Text(Fmt.num(value, decimals: 2))
                    .foregroundStyle(.secondary)
                    .fontDesign(.monospaced)
            }
        }
    }
}
