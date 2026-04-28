import SwiftUI

struct RecommendationsView: View {
    @EnvironmentObject var session: SessionStore
    @State private var data: RecommendationsResponse?
    @State private var status: String = "pending"
    @State private var loading = false
    @State private var error: String?
    @State private var info: String?
    @State private var pending: (id: Int, action: String)?
    @State private var showConfirm = false

    private let statuses = ["pending", "approved", "rejected", "expired"]

    var body: some View {
        List {
            Section {
                Picker("Status", selection: $status) {
                    ForEach(statuses, id: \.self) { Text($0.capitalized).tag($0) }
                }
                .pickerStyle(.segmented)
                .onChange(of: status) { Task { await load() } }
                if let d = data {
                    HStack {
                        ForEach(d.counts.sorted(by: { $0.key < $1.key }), id: \.key) { k, v in
                            VStack {
                                Text("\(v)").font(.headline)
                                Text(k).font(.caption2).foregroundStyle(.secondary)
                            }
                            .frame(maxWidth: .infinity)
                        }
                    }
                }
                ErrorBox(message: error)
                if let info { Text(info).font(.caption).foregroundStyle(.green) }
            }
            if let rows = data?.rows {
                Section("Recommendations (\(rows.count))") {
                    if rows.isEmpty {
                        Text("No recommendations").foregroundStyle(.secondary)
                    }
                    ForEach(rows) { r in
                        VStack(alignment: .leading, spacing: 4) {
                            HStack {
                                Text(r.symbol).font(.headline)
                                Spacer()
                                Text(r.kind).font(.caption2.weight(.bold))
                                    .padding(.horizontal, 6).padding(.vertical, 2)
                                    .background(Color.purple.opacity(0.2))
                                    .foregroundStyle(.purple)
                                    .cornerRadius(4)
                                Text(r.status.uppercased())
                                    .font(.caption2.weight(.bold))
                                    .foregroundStyle(statusColor(r.status))
                            }
                            Text(r.summary).font(.subheadline)
                            if !r.rationale.isEmpty {
                                Text(r.rationale).font(.caption).foregroundStyle(.secondary)
                            }
                            HStack {
                                if let n = r.metric_name, let v = r.metric_value {
                                    Text("\(n): \(Fmt.num(v))").font(.caption2).foregroundStyle(.secondary)
                                }
                                Spacer()
                                Text(Fmt.relative(r.created_at)).font(.caption2).foregroundStyle(.tertiary)
                            }
                            if r.status == "pending" {
                                HStack {
                                    Button {
                                        pending = (r.id, "approved"); showConfirm = true
                                    } label: { Label("Approve", systemImage: "checkmark.circle").foregroundStyle(.green) }
                                    .buttonStyle(.bordered)
                                    Button {
                                        pending = (r.id, "rejected"); showConfirm = true
                                    } label: { Label("Reject", systemImage: "xmark.circle").foregroundStyle(.red) }
                                    .buttonStyle(.bordered)
                                }
                                .padding(.top, 4)
                            }
                        }
                        .padding(.vertical, 4)
                    }
                }
            }
        }
        .navigationTitle("Recommendations")
        .refreshable { await load() }
        .task { await load() }
        .confirmationDialog("Send decision?", isPresented: $showConfirm, titleVisibility: .visible) {
            Button("Confirm", role: pending?.action == "rejected" ? .destructive : nil) {
                if let p = pending { Task { await decide(id: p.id, action: p.action) } }
            }
            Button("Cancel", role: .cancel) {}
        }
    }

    private func statusColor(_ s: String) -> Color {
        switch s {
        case "approved": return .green
        case "rejected": return .red
        case "expired": return .gray
        default: return .orange
        }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            let resp: RecommendationsResponse = try await session.api.request(
                "/recommendations",
                query: [URLQueryItem(name: "status", value: status)],
                as: RecommendationsResponse.self
            )
            self.data = resp
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }

    private func decide(id: Int, action: String) async {
        info = nil
        do {
            let _: RecommendationDecisionResp = try await session.api.sendWithConfirmation(
                "/recommendations/\(id)/decision",
                method: "POST",
                body: RecommendationDecisionBody(status: action),
                as: RecommendationDecisionResp.self
            )
            self.info = "Recommendation \(id) → \(action)"
            await load()
        } catch { self.error = error.localizedDescription }
    }
}
