import SwiftUI

struct PlanningView: View {
    @EnvironmentObject var session: SessionStore
    @State private var plans: [StrategicPlan] = []
    @State private var runs: [TemporalRun] = []
    @State private var loading = false
    @State private var error: String?

    var body: some View {
        List {
            Section("Strategic plans") {
                if plans.isEmpty, !loading { Text("No plans").foregroundStyle(.secondary) }
                ForEach(plans) { p in
                    NavigationLink {
                        StrategicPlanDetailView(plan: p)
                    } label: {
                        VStack(alignment: .leading) {
                            HStack {
                                Text(p.horizon).font(.headline)
                                Spacer()
                                Text(Fmt.short(p.ts)).font(.caption2).foregroundStyle(.secondary)
                            }
                            Text(p.summary_text)
                                .lineLimit(3)
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
            }
            Section("Temporal runs") {
                if runs.isEmpty, !loading { Text("No runs").foregroundStyle(.secondary) }
                ForEach(runs) { r in
                    NavigationLink {
                        TemporalReplayView(workflowId: r.workflow_id, runId: r.run_id)
                    } label: {
                        VStack(alignment: .leading) {
                            HStack {
                                Text(r.workflow_type).font(.headline)
                                Spacer()
                                Text(r.status.uppercased())
                                    .font(.caption2.weight(.bold))
                                    .foregroundStyle(r.status.lowercased() == "completed" ? .green : .orange)
                            }
                            Text(r.workflow_id).font(.caption2).foregroundStyle(.secondary)
                            if let st = r.start_time {
                                Text(Fmt.relative(st)).font(.caption2).foregroundStyle(.tertiary)
                            }
                        }
                    }
                }
            }
            ErrorBox(message: error)
        }
        .navigationTitle("Planning")
        .refreshable { await load() }
        .task { await load() }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            async let s = try await session.api.request("/strategic", as: StrategicResponse.self)
            async let t = try? await session.api.request("/temporal/runs", as: TemporalRunsResponse.self)
            self.plans = try await s.plans
            self.runs = (await t)?.runs ?? []
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }
}

struct StrategicPlanDetailView: View {
    let plan: StrategicPlan
    var body: some View {
        Form {
            Section("Plan") {
                LabeledContent("Horizon", value: plan.horizon)
                LabeledContent("Time", value: Fmt.short(plan.ts))
                if let url = plan.langfuse_url, let u = URL(string: url) {
                    Link("View in Langfuse", destination: u)
                }
            }
            Section("Summary") { Text(plan.summary_text) }
            if let json = plan.plan_json?.displayString() {
                Section("JSON") {
                    Text(json).font(.caption.monospaced())
                }
            }
        }
        .navigationTitle("Plan #\(plan.id)")
    }
}

struct TemporalReplayView: View {
    @EnvironmentObject var session: SessionStore
    let workflowId: String
    let runId: String
    @State private var data: TemporalReplay?
    @State private var error: String?

    var body: some View {
        List {
            if let d = data {
                Section("Workflow") {
                    LabeledContent("ID", value: d.workflow_id)
                    LabeledContent("Run", value: d.run_id)
                    LabeledContent("Events", value: "\(d.event_count)")
                    if let url = d.langfuse_url, let u = URL(string: url) {
                        Link("View in Langfuse", destination: u)
                    }
                }
                Section("Events") {
                    ForEach(d.events) { e in
                        VStack(alignment: .leading) {
                            HStack {
                                Text(e.event_type).font(.caption.weight(.semibold))
                                Spacer()
                                if let t = e.event_time {
                                    Text(Fmt.relative(t)).font(.caption2).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                }
            } else if error == nil {
                ProgressView()
            }
            ErrorBox(message: error)
        }
        .navigationTitle("Replay")
        .task { await load() }
    }

    private func load() async {
        do {
            self.data = try await session.api.request(
                "/temporal/replay/\(workflowId)/\(runId)",
                as: TemporalReplay.self
            )
        } catch { self.error = error.localizedDescription }
    }
}
