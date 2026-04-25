import SwiftUI

struct SystemLogsView: View {
    @EnvironmentObject var session: SessionStore
    @State private var events: [EventLog] = []
    @State private var hours: Int = 24
    @State private var severityFilter = "all"
    @State private var loading = false
    @State private var error: String?

    var body: some View {
        List {
            Section {
                Picker("Range", selection: $hours) {
                    Text("1h").tag(1)
                    Text("24h").tag(24)
                    Text("7d").tag(168)
                }
                .pickerStyle(.segmented)
                .onChange(of: hours) { _, _ in Task { await load() } }
                Picker("Severity", selection: $severityFilter) {
                    Text("All").tag("all")
                    Text("Info").tag("info")
                    Text("Warning").tag("warning")
                    Text("Error").tag("error")
                }
            }
            ForEach(filteredEvents) { e in
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text(e.severity.uppercased())
                            .font(.caption2.weight(.bold))
                            .padding(.horizontal, 6).padding(.vertical, 2)
                            .background(Fmt.severityColor(e.severity).opacity(0.2))
                            .foregroundStyle(Fmt.severityColor(e.severity))
                            .cornerRadius(4)
                        Text(e.event_type).font(.caption.weight(.semibold))
                        Spacer()
                        Text(Fmt.relative(e.ts)).font(.caption2).foregroundStyle(.secondary)
                    }
                    if let pair = e.pair {
                        Text(pair).font(.caption2).foregroundStyle(.tertiary)
                    }
                    Text(e.message).font(.caption)
                    if let txt = e.data?.displayString() {
                        Text(txt).font(.caption2.monospaced()).foregroundStyle(.secondary).lineLimit(8)
                    }
                }
            }
            ErrorBox(message: error)
        }
        .navigationTitle("System logs")
        .refreshable { await load() }
        .task { await load() }
    }

    private var filteredEvents: [EventLog] {
        if severityFilter == "all" { return events }
        return events.filter { $0.severity.lowercased().hasPrefix(severityFilter) }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            let r: EventsResponse = try await session.api.request(
                "/events",
                query: [URLQueryItem(name: "hours", value: "\(hours)")],
                as: EventsResponse.self
            )
            self.events = r.events
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }
}
