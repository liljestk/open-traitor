import SwiftUI
import Charts

struct LearningView: View {
    @EnvironmentObject var session: SessionStore
    @State private var status: LearningStatus?
    @State private var trends: LearningAccuracyTrends?
    @State private var ensemble: LearningEnsemble?
    @State private var calibration: LearningCalibration?
    @State private var lessons: [JSONValue] = []
    @State private var loading = false
    @State private var error: String?

    var body: some View {
        List {
            if let s = status {
                Section("Status") {
                    LabeledContent("Enabled", value: s.enabled ? "yes" : "no")
                    if let r = s.total_runs { LabeledContent("Runs", value: "\(r)") }
                    if let e = s.total_errors { LabeledContent("Errors", value: "\(e)") }
                    if let subs = s.subsystems?.displayString() {
                        Text(subs).font(.caption2.monospaced()).foregroundStyle(.secondary).lineLimit(8)
                    }
                }
            }
            if let t = trends, !t.data.isEmpty {
                Section("Accuracy trend (\(t.days)d)") {
                    Chart(t.data) { row in
                        LineMark(
                            x: .value("date", row.date),
                            y: .value("acc", row.accuracy ?? 0)
                        )
                    }.frame(height: 180)
                }
            }
            if let e = ensemble, !e.active.isEmpty {
                Section("Ensemble weights") {
                    ForEach(e.active) { w in
                        HStack {
                            Text(w.strategy)
                            Spacer()
                            Text(Fmt.num(w.weight, decimals: 3))
                                .fontDesign(.monospaced)
                        }
                    }
                }
            }
            if let c = calibration, !c.curve.isEmpty {
                Section("Calibration") {
                    Chart(c.curve) { p in
                        PointMark(
                            x: .value("predicted", p.predicted),
                            y: .value("actual", p.actual_accuracy)
                        )
                    }.frame(height: 180)
                    if let models = c.models {
                        ForEach(models) { m in
                            HStack {
                                Text(m.model)
                                Spacer()
                                if let b = m.brier_score {
                                    Text("Brier \(Fmt.num(b, decimals: 4))").font(.caption)
                                }
                            }
                        }
                    }
                }
            }
            if !lessons.isEmpty {
                Section("Lessons") {
                    ForEach(lessons.indices, id: \.self) { i in
                        Text(lessons[i].displayString())
                            .font(.caption.monospaced())
                            .lineLimit(6)
                    }
                }
            }
            ErrorBox(message: error)
        }
        .navigationTitle("Learning")
        .refreshable { await load() }
        .task { await load() }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            async let s = try? await session.api.request("/learning/status", as: LearningStatus.self)
            async let t = try? await session.api.request("/learning/accuracy-trends", as: LearningAccuracyTrends.self)
            async let e = try? await session.api.request("/learning/ensemble-weights", as: LearningEnsemble.self)
            async let c = try? await session.api.request("/learning/calibration", as: LearningCalibration.self)
            async let l = try? await session.api.request("/learning/lessons", as: LearningLessonsResp.self)
            self.status = await s
            self.trends = await t
            self.ensemble = await e
            self.calibration = await c
            self.lessons = (await l)?.lessons ?? []
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }
}
