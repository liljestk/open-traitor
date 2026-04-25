import SwiftUI

struct PatternsView: View {
    @EnvironmentObject var session: SessionStore
    @State private var status: PatternStatusResponse?
    @State private var upcoming: UpcomingPatternsResponse?
    @State private var loading = false
    @State private var error: String?

    var body: some View {
        List {
            if let s = status {
                Section("Status") {
                    LabeledContent("Profile", value: s.profile)
                    LabeledContent("Exchange", value: s.exchange)
                    LabeledContent("Ready", value: s.ready ? "yes" : "no")
                    if let counts = s.counts {
                        ForEach(counts.sorted(by: { $0.key < $1.key }), id: \.key) { k, v in
                            LabeledContent(k, value: "\(v)")
                        }
                    }
                }
                if let backfills = s.recent_backfills, !backfills.isEmpty {
                    Section("Recent backfills") {
                        ForEach(backfills) { b in
                            VStack(alignment: .leading) {
                                Text(b.symbol ?? "?").font(.headline)
                                HStack {
                                    Text(b.status ?? "?").font(.caption)
                                    Spacer()
                                    if let p = b.processed_events, let t = b.total_events {
                                        Text("\(p)/\(t)").font(.caption2).foregroundStyle(.secondary)
                                    }
                                }
                                if let ts = b.updated_at {
                                    Text(Fmt.relative(ts)).font(.caption2).foregroundStyle(.tertiary)
                                }
                            }
                        }
                    }
                }
            }

            if let u = upcoming {
                Section("Upcoming (\(u.count))") {
                    if u.items.isEmpty {
                        Text("No upcoming events").foregroundStyle(.secondary)
                    }
                    ForEach(u.items) { item in
                        NavigationLink {
                            PatternDetailView(eventId: item.upcoming_event.id)
                        } label: {
                            VStack(alignment: .leading, spacing: 4) {
                                HStack {
                                    Text(item.symbol).font(.headline)
                                    Spacer()
                                    Text(item.upcoming_event.event_type)
                                        .font(.caption2.weight(.bold))
                                        .padding(.horizontal, 6).padding(.vertical, 2)
                                        .background(Color.blue.opacity(0.2))
                                        .foregroundStyle(.blue)
                                        .cornerRadius(4)
                                }
                                Text(Fmt.short(item.upcoming_event.event_ts))
                                    .font(.caption).foregroundStyle(.secondary)
                                if let o = item.outcome {
                                    HStack {
                                        if let m = o.mean_return {
                                            Text("avg \(Fmt.pct(m * 100))").font(.caption2)
                                        }
                                        if let w = o.win_rate {
                                            Text("win \(Fmt.pct(w * 100))").font(.caption2)
                                        }
                                        if let n = o.n {
                                            Text("n=\(n)").font(.caption2).foregroundStyle(.secondary)
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }

            ErrorBox(message: error)
        }
        .navigationTitle("Patterns")
        .refreshable { await load() }
        .task { await load() }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            async let s = try await session.api.request("/patterns/status", as: PatternStatusResponse.self)
            async let u = try await session.api.request("/patterns/upcoming", as: UpcomingPatternsResponse.self)
            self.status = try await s
            self.upcoming = try await u
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }
}

struct PatternDetailView: View {
    @EnvironmentObject var session: SessionStore
    let eventId: String
    @State private var data: EventMatchesResponse?
    @State private var error: String?

    var body: some View {
        List {
            if let d = data {
                Section("Event") {
                    LabeledContent("Type", value: d.event.event_type)
                    LabeledContent("Symbol", value: d.event.symbol)
                    LabeledContent("Time", value: Fmt.short(d.event.event_ts))
                    if let src = d.event.source { LabeledContent("Source", value: src) }
                    if let conf = d.event.confidence {
                        LabeledContent("Confidence", value: Fmt.pct(conf * 100))
                    }
                }
                if let o = d.outcome {
                    Section("Historical outcome") {
                        if let n = o.n { LabeledContent("Sample size", value: "\(n)") }
                        if let m = o.mean_return { LabeledContent("Mean return", value: Fmt.pct(m * 100)) }
                        if let med = o.median_return { LabeledContent("Median return", value: Fmt.pct(med * 100)) }
                        if let w = o.win_rate { LabeledContent("Win rate", value: Fmt.pct(w * 100)) }
                        if let win = o.sample_window_days { LabeledContent("Window", value: "\(win) days") }
                        if let c = o.confidence { LabeledContent("Confidence", value: Fmt.pct(c * 100)) }
                    }
                }
                if let metaText = d.event.metadata?.displayString() {
                    Section("Metadata") {
                        Text(metaText).font(.caption.monospaced())
                    }
                }
            } else if error == nil {
                ProgressView()
            }
            ErrorBox(message: error)
        }
        .navigationTitle("Pattern")
        .task { await load() }
    }

    private func load() async {
        do {
            self.data = try await session.api.request("/patterns/\(eventId)/matches", as: EventMatchesResponse.self)
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }
}
