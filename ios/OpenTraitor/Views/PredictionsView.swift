import SwiftUI
import Charts

struct PredictionsView: View {
    @EnvironmentObject var session: SessionStore
    @State private var data: PredictionAccuracyResponse?
    @State private var pairs: TrackedPairsData?
    @State private var loading = false
    @State private var error: String?

    var body: some View {
        List {
            if let d = data {
                Section("Overall") {
                    LabeledContent("Predictions", value: "\(d.overall.total)")
                    LabeledContent("24h accuracy", value: Fmt.pct(d.overall.accuracy_24h_pct))
                    LabeledContent("1h accuracy", value: Fmt.pct(d.overall.accuracy_1h_pct))
                }

                Section("Daily accuracy") {
                    if d.daily_accuracy.isEmpty {
                        Text("No data yet").foregroundStyle(.secondary)
                    } else {
                        Chart(d.daily_accuracy) { row in
                            BarMark(
                                x: .value("Day", row.date),
                                y: .value("Accuracy", row.accuracy_pct ?? 0)
                            )
                        }
                        .frame(height: 180)
                    }
                }

                Section("Per pair") {
                    ForEach(d.per_pair.sorted(by: { $0.key < $1.key }), id: \.key) { pair, acc in
                        HStack {
                            Text(pair)
                            Spacer()
                            Text(Fmt.pct(acc.accuracy_24h_pct))
                                .foregroundStyle(.secondary)
                        }
                    }
                }

                Section("Confidence calibration") {
                    ForEach(d.confidence_calibration) { bucket in
                        HStack {
                            Text(bucket.confidence_range)
                            Spacer()
                            Text(Fmt.pct(bucket.accuracy_pct))
                                .foregroundStyle(.secondary)
                            Text("(\(bucket.evaluated))")
                                .font(.caption2)
                                .foregroundStyle(.tertiary)
                        }
                    }
                }
            }

            if let p = pairs {
                Section("Tracked pairs (\(p.total_pairs))") {
                    ForEach(p.crypto + p.equity) { tp in
                        NavigationLink {
                            PairPredictionView(pair: tp.pair)
                        } label: {
                            VStack(alignment: .leading) {
                                Text(tp.pair).font(.headline)
                                Text("\(tp.prediction_count) predictions · \(tp.source)")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }

            ErrorBox(message: error)
        }
        .navigationTitle("Predictions")
        .refreshable { await load() }
        .task { await load() }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            async let acc: PredictionAccuracyResponse = session.api.request("/predictions/accuracy", as: PredictionAccuracyResponse.self)
            async let prs: TrackedPairsData = session.api.request("/predictions/pairs", as: TrackedPairsData.self)
            let (a, p) = try await (acc, prs)
            self.data = a
            self.pairs = p
            self.error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}

struct PairPredictionView: View {
    @EnvironmentObject var session: SessionStore
    let pair: String
    @State private var data: PairPredictionHistory?
    @State private var error: String?

    var body: some View {
        List {
            if let d = data {
                Section("Price history") {
                    if d.price_history.isEmpty {
                        Text("No history").foregroundStyle(.secondary)
                    } else {
                        Chart {
                            ForEach(d.price_history.indices, id: \.self) { i in
                                let p = d.price_history[i]
                                LineMark(
                                    x: .value("ts", p.ts),
                                    y: .value("price", p.price)
                                )
                            }
                        }
                        .frame(height: 200)
                    }
                }
                Section("Predictions (\(d.total_predictions))") {
                    ForEach(d.predictions.indices, id: \.self) { i in
                        let m = d.predictions[i]
                        HStack {
                            Image(systemName: m.is_bullish ? "arrow.up.right" : "arrow.down.right")
                                .foregroundStyle(m.is_bullish ? .green : .red)
                            VStack(alignment: .leading) {
                                Text(m.signal_type)
                                Text(Fmt.short(m.ts))
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Text(Fmt.pct(m.confidence * 100))
                                .font(.caption)
                        }
                    }
                }
            } else if error == nil {
                ProgressView()
            }
            ErrorBox(message: error)
        }
        .navigationTitle(pair)
        .task { await load() }
    }

    private func load() async {
        do {
            let d: PairPredictionHistory = try await session.api.request(
                "/predictions/pair/\(pair)",
                as: PairPredictionHistory.self
            )
            self.data = d
            self.error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}
