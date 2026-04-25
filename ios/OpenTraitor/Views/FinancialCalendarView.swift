import SwiftUI

struct FinancialCalendarView: View {
    @EnvironmentObject var session: SessionStore
    @State private var data: FinancialCalendarData?
    @State private var loading = false
    @State private var error: String?

    var body: some View {
        List {
            if let d = data {
                Section { Text("Domain: \(d.domain)").foregroundStyle(.secondary) }
                section(title: "Earnings", events: d.earnings ?? d.events?.filter { $0.type == "earnings" })
                section(title: "Dividends", events: d.dividends ?? d.events?.filter { $0.type == "dividend" })
                section(title: "Macro", events: d.macro ?? d.events?.filter { $0.type == "macro" })
            }
            ErrorBox(message: error)
        }
        .navigationTitle("Calendar")
        .refreshable { await load() }
        .task { await load() }
    }

    @ViewBuilder
    private func section(title: String, events: [FinancialEvent]?) -> some View {
        if let events, !events.isEmpty {
            Section(title) {
                ForEach(events) { e in
                    NavigationLink {
                        CalendarSummaryView(ticker: e.ticker)
                    } label: {
                        HStack {
                            VStack(alignment: .leading) {
                                Text(e.ticker).font(.headline)
                                Text(e.date).font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Text("\(e.days_away)d")
                                .font(.caption2.weight(.bold))
                                .padding(.horizontal, 6).padding(.vertical, 2)
                                .background(importanceColor(e.importance).opacity(0.2))
                                .foregroundStyle(importanceColor(e.importance))
                                .cornerRadius(4)
                        }
                    }
                }
            }
        }
    }

    private func importanceColor(_ s: String) -> Color {
        switch s.lowercased() {
        case "high": return .red
        case "medium": return .orange
        default: return .blue
        }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            self.data = try await session.api.request("/financial-calendar", as: FinancialCalendarData.self)
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }
}

struct CalendarSummaryView: View {
    @EnvironmentObject var session: SessionStore
    let ticker: String
    @State private var data: FinancialSummaryData?
    @State private var error: String?

    var body: some View {
        List {
            if let d = data {
                Section(d.ticker) {
                    if let c = d.company { LabeledContent("Company", value: c) }
                    if let g = d.generated_at { LabeledContent("Generated", value: Fmt.relative(g)) }
                }
                if let s = d.summary, !s.isEmpty {
                    Section("Summary") { Text(s) }
                }
                if let m = d.metrics?.displayString() {
                    Section("Metrics") {
                        Text(m).font(.caption.monospaced())
                    }
                }
            } else if error == nil {
                ProgressView()
            }
            ErrorBox(message: error)
        }
        .navigationTitle(ticker)
        .task { await load() }
    }

    private func load() async {
        do {
            self.data = try await session.api.request(
                "/financial-calendar/summary",
                query: [URLQueryItem(name: "ticker", value: ticker)],
                as: FinancialSummaryData.self
            )
        } catch { self.error = error.localizedDescription }
    }
}
