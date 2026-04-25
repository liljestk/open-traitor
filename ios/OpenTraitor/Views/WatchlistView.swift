import SwiftUI
import Charts

struct WatchlistView: View {
    @EnvironmentObject var session: SessionStore
    @State private var data: WatchlistData?
    @State private var loading = false
    @State private var error: String?
    @State private var searchText = ""
    @State private var searchResults: [ProductSearchResult] = []
    @State private var searching = false

    var body: some View {
        List {
            Section {
                TextField("Search markets…", text: $searchText)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .onSubmit { Task { await search() } }
                if searching { ProgressView() }
                ForEach(searchResults) { r in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(r.id_).font(.headline)
                            if let d = r.display_name { Text(d).font(.caption).foregroundStyle(.secondary) }
                        }
                        Spacer()
                        Button("Follow") { Task { await follow(r.id_) } }
                            .buttonStyle(.bordered)
                    }
                }
            }

            if let d = data {
                Section("Active pairs (\(d.active_pairs.count))") {
                    ForEach(d.pair_info) { info in
                        NavigationLink {
                            PairChartView(pair: info.pair)
                        } label: {
                            HStack {
                                VStack(alignment: .leading) {
                                    Text(info.pair).font(.headline)
                                    HStack(spacing: 6) {
                                        if info.followed_by_human {
                                            Label("human", systemImage: "person.fill")
                                                .font(.caption2).foregroundStyle(.blue)
                                        }
                                        if info.followed_by_llm {
                                            Label("LLM", systemImage: "cpu.fill")
                                                .font(.caption2).foregroundStyle(.purple)
                                        }
                                    }
                                }
                                Spacer()
                                if let p = info.price ?? d.live_prices[info.pair] {
                                    Text(Fmt.money(p, decimals: 4))
                                        .font(.caption)
                                        .fontDesign(.monospaced)
                                }
                            }
                        }
                        .swipeActions {
                            Button(role: .destructive) {
                                Task { await unfollow(info.pair) }
                            } label: { Label("Unfollow", systemImage: "minus.circle") }
                        }
                    }
                }

                if let s = d.scan {
                    Section("Last scan") {
                        if let ts = s.ts { LabeledContent("When", value: Fmt.relative(ts)) }
                        if let n = s.scanned_pairs { LabeledContent("Scanned", value: "\(n)") }
                        if let summary = s.summary_text {
                            Text(summary).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }

                if let r = d.rpm_budget {
                    Section("RPM budget") {
                        if let p = r.provider { LabeledContent("Provider", value: p) }
                        if let m = r.model { LabeledContent("Model", value: m) }
                        if let rpm = r.rpm { LabeledContent("RPM", value: "\(rpm)") }
                        if let avail = r.available_per_cycle { LabeledContent("Per cycle", value: "\(avail)") }
                    }
                }
            }

            ErrorBox(message: error)
        }
        .navigationTitle("Watchlist")
        .refreshable { await load() }
        .task { await load() }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            self.data = try await session.api.request("/watchlist", as: WatchlistData.self)
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }

    private func search() async {
        guard !searchText.isEmpty else { searchResults = []; return }
        searching = true
        defer { searching = false }
        do {
            let q = [URLQueryItem(name: "q", value: searchText)]
            let r: ProductSearchResponse = try await session.api.request("/products/search", query: q, as: ProductSearchResponse.self)
            self.searchResults = r.results
        } catch { self.error = error.localizedDescription }
    }

    private func follow(_ pair: String) async {
        do {
            _ = try await session.api.request(
                "/watchlist/follow", method: "POST",
                body: FollowRequest(pair: pair, exchange: session.profile),
                as: FollowResponse.self
            )
            await load()
        } catch { self.error = error.localizedDescription }
    }

    private func unfollow(_ pair: String) async {
        do {
            _ = try await session.api.request(
                "/watchlist/unfollow", method: "POST",
                body: FollowRequest(pair: pair, exchange: session.profile),
                as: UnfollowResponse.self
            )
            await load()
        } catch { self.error = error.localizedDescription }
    }
}

struct PairChartView: View {
    @EnvironmentObject var session: SessionStore
    let pair: String
    @State private var candles: [CandleData] = []
    @State private var error: String?

    var body: some View {
        VStack {
            if !candles.isEmpty {
                Chart {
                    ForEach(candles.indices, id: \.self) { i in
                        let c = candles[i]
                        RuleMark(
                            x: .value("ts", i),
                            yStart: .value("low", c.low),
                            yEnd: .value("high", c.high)
                        )
                        .foregroundStyle(.gray)
                        BarMark(
                            x: .value("ts", i),
                            yStart: .value("open", c.open),
                            yEnd: .value("close", c.close),
                            width: 4
                        )
                        .foregroundStyle(c.close >= c.open ? .green : .red)
                    }
                }
                .frame(height: 280)
                .padding()
            } else if error == nil {
                ProgressView()
            }
            ErrorBox(message: error)
            Spacer()
        }
        .navigationTitle(pair)
        .task { await load() }
    }

    private func load() async {
        do {
            let resp: CandlesResponse = try await session.api.request(
                "/candles/\(pair)",
                query: [URLQueryItem(name: "granularity", value: "ONE_HOUR"),
                        URLQueryItem(name: "limit", value: "100")],
                as: CandlesResponse.self
            )
            self.candles = resp.candles
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }
}
