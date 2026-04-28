import SwiftUI

struct SmartsView: View {
    @EnvironmentObject var session: SessionStore
    @State private var tab: Tab = .brier
    @State private var error: String?
    @State private var follower: String = ""
    @State private var asset: String = ""
    @State private var metric: String = ""
    @State private var l2Symbol: String = ""

    @State private var brier: [FeatureBrierRow] = []
    @State private var bandit: [BanditRow] = []
    @State private var counterfactual: [CounterfactualRow] = []
    @State private var leadlag: LeadLagResponse?
    @State private var events: [UpcomingSmartEvent] = []
    @State private var drift: [DecisionDriftRow] = []
    @State private var judge: [ReasoningJudgeRow] = []
    @State private var onchain: [OnchainRow] = []
    @State private var shadow: [ShadowRow] = []
    @State private var l2: [L2SnapshotRow] = []

    enum Tab: String, CaseIterable, Identifiable {
        case brier = "Brier"
        case bandit = "Bandit"
        case counterfactual = "CF"
        case leadlag = "Lead-Lag"
        case events = "Events"
        case drift = "Drift"
        case judge = "Judge"
        case onchain = "On-chain"
        case shadow = "Shadow"
        case l2 = "L2"
        var id: String { rawValue }
    }

    var body: some View {
        VStack(spacing: 0) {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ForEach(Tab.allCases) { t in
                        Button(t.rawValue) { tab = t }
                            .buttonStyle(.bordered)
                            .tint(tab == t ? .accentColor : .secondary)
                    }
                }
                .padding(.horizontal)
            }
            .padding(.vertical, 6)

            List {
                ErrorBox(message: error)
                content
            }
        }
        .navigationTitle("Smarts")
        .refreshable { await load() }
        .task { await load() }
        .onChange(of: tab) { Task { await load() } }
    }

    @ViewBuilder private var content: some View {
        switch tab {
        case .brier:
            Section("Feature Brier scores (\(brier.count))") {
                if brier.isEmpty { Text("No data").foregroundStyle(.secondary) }
                ForEach(brier) { r in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(r.feature_name).font(.headline)
                            Text("n=\(r.samples)").font(.caption2).foregroundStyle(.secondary)
                        }
                        Spacer()
                        VStack(alignment: .trailing) {
                            if let b = r.brier_score { Text("Brier \(Fmt.num(b, decimals: 4))").font(.caption) }
                            if let c = r.avg_confidence { Text("conf \(Fmt.pct(c * 100))").font(.caption2) }
                        }
                    }
                }
            }
        case .bandit:
            Section("Contextual bandit (\(bandit.count))") {
                if bandit.isEmpty { Text("No data").foregroundStyle(.secondary) }
                ForEach(bandit) { r in
                    HStack {
                        VStack(alignment: .leading) {
                            Text("\(r.regime) → \(r.strategy)").font(.headline)
                            Text("α=\(Fmt.num(r.alpha)) β=\(Fmt.num(r.beta))").font(.caption2)
                        }
                        Spacer()
                        Text("\(r.n_pulls)").font(.caption.weight(.bold))
                    }
                }
            }
        case .counterfactual:
            Section("Counterfactual replay (\(counterfactual.count))") {
                if counterfactual.isEmpty { Text("No data").foregroundStyle(.secondary) }
                ForEach(counterfactual) { r in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(r.pair).font(.headline)
                            Spacer()
                            Text(r.agreed == true ? "AGREE" : "DIVERGE")
                                .font(.caption2.weight(.bold))
                                .foregroundStyle(r.agreed == true ? .green : .orange)
                        }
                        Text("actual: \(r.actual_action) → replay: \(r.replay_action)").font(.caption)
                        HStack {
                            if let a = r.actual_pnl_pct { Text("actual \(Fmt.pct(a * 100))").font(.caption2) }
                            if let p = r.replay_pnl_pct { Text("replay \(Fmt.pct(p * 100))").font(.caption2) }
                            Spacer()
                            Text(Fmt.relative(r.ts)).font(.caption2).foregroundStyle(.tertiary)
                        }
                    }
                }
            }
        case .leadlag:
            Section("Lead-lag (specify follower)") {
                HStack {
                    TextField("Follower symbol", text: $follower)
                        .textInputAutocapitalization(.characters).autocorrectionDisabled()
                    Button("Load") { Task { await loadLeadLag() } }.disabled(follower.isEmpty)
                }
            }
            if let ll = leadlag {
                Section("\(ll.follower) leaders (\(ll.rows.count))") {
                    if ll.rows.isEmpty { Text("No leaders").foregroundStyle(.secondary) }
                    ForEach(ll.rows) { r in
                        HStack {
                            VStack(alignment: .leading) {
                                Text("\(r.leader) → \(r.follower)").font(.headline)
                                Text("lag \(r.lag_minutes)m • n=\(r.sample_count)").font(.caption2).foregroundStyle(.secondary)
                            }
                            Spacer()
                            if let b = r.beta { Text("β \(Fmt.num(b, decimals: 3))").font(.caption) }
                            if let r2 = r.r_squared { Text("R² \(Fmt.num(r2, decimals: 3))").font(.caption2) }
                        }
                    }
                }
            }
        case .events:
            Section("Upcoming smart events (\(events.count))") {
                if events.isEmpty { Text("No events").foregroundStyle(.secondary) }
                ForEach(events) { e in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(e.symbol).font(.headline)
                            Spacer()
                            Text(e.event_type).font(.caption2.weight(.bold))
                                .padding(.horizontal, 6).padding(.vertical, 2)
                                .background(Color.blue.opacity(0.2))
                                .cornerRadius(4)
                        }
                        if let t = e.title { Text(t).font(.caption) }
                        HStack {
                            Text(Fmt.short(e.event_ts)).font(.caption2).foregroundStyle(.secondary)
                            Spacer()
                            if let imp = e.importance { Text("imp \(Fmt.num(imp, decimals: 2))").font(.caption2) }
                        }
                    }
                }
            }
        case .drift:
            Section("Decision drift (\(drift.count))") {
                if drift.isEmpty { Text("No data").foregroundStyle(.secondary) }
                ForEach(drift) { r in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(r.agent).font(.headline)
                            Spacer()
                            if let a = r.alert {
                                Text(a.uppercased()).font(.caption2.weight(.bold)).foregroundStyle(.orange)
                            }
                        }
                        Text("\(r.snapshot_date) • n=\(r.n_decisions)").font(.caption2).foregroundStyle(.secondary)
                        HStack {
                            if let m = r.mean_conf { Text("μ \(Fmt.pct(m * 100))").font(.caption2) }
                            if let z = r.z_score { Text("z \(Fmt.num(z, decimals: 2))").font(.caption2) }
                        }
                    }
                }
            }
        case .judge:
            Section("Reasoning judge (\(judge.count))") {
                if judge.isEmpty { Text("No data").foregroundStyle(.secondary) }
                ForEach(judge) { r in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text("\(r.agent) • \(r.pair)").font(.headline)
                            Spacer()
                            Text(r.verdict.uppercased()).font(.caption2.weight(.bold))
                                .foregroundStyle(verdictColor(r.verdict))
                        }
                        if let s = r.score { Text("score \(Fmt.num(s, decimals: 2))").font(.caption2) }
                        if let rationale = r.rationale, !rationale.isEmpty {
                            Text(rationale).font(.caption).foregroundStyle(.secondary).lineLimit(3)
                        }
                        Text(Fmt.relative(r.judged_at)).font(.caption2).foregroundStyle(.tertiary)
                    }
                }
            }
        case .onchain:
            Section("On-chain query") {
                HStack {
                    TextField("Asset", text: $asset).textInputAutocapitalization(.characters).autocorrectionDisabled()
                    TextField("Metric", text: $metric).autocorrectionDisabled()
                    Button("Load") { Task { await loadOnchain() } }.disabled(asset.isEmpty || metric.isEmpty)
                }
            }
            Section("On-chain (\(onchain.count))") {
                if onchain.isEmpty { Text("No data").foregroundStyle(.secondary) }
                ForEach(onchain) { r in
                    HStack {
                        VStack(alignment: .leading) {
                            Text("\(r.asset)/\(r.metric)").font(.headline)
                            Text(Fmt.relative(r.ts)).font(.caption2).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if let v = r.value { Text(Fmt.num(v, decimals: 4)).font(.caption.weight(.bold)) }
                    }
                }
            }
        case .shadow:
            Section("Shadow strategist (\(shadow.count))") {
                if shadow.isEmpty { Text("No data").foregroundStyle(.secondary) }
                ForEach(shadow) { r in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text("\(r.variant) • \(r.pair)").font(.headline)
                            Spacer()
                            Text(r.diff_action == true ? "DIFF" : "SAME")
                                .font(.caption2.weight(.bold))
                                .foregroundStyle(r.diff_action == true ? .orange : .secondary)
                        }
                        Text("shadow: \(r.action) vs live: \(r.live_action)").font(.caption)
                        if let r2 = r.reasoning, !r2.isEmpty {
                            Text(r2).font(.caption2).foregroundStyle(.secondary).lineLimit(2)
                        }
                        Text(Fmt.relative(r.ts)).font(.caption2).foregroundStyle(.tertiary)
                    }
                }
            }
        case .l2:
            Section("L2 snapshots query") {
                HStack {
                    TextField("Symbol", text: $l2Symbol)
                        .textInputAutocapitalization(.characters).autocorrectionDisabled()
                    Button("Load") { Task { await loadL2() } }.disabled(l2Symbol.isEmpty)
                }
            }
            Section("Snapshots (\(l2.count))") {
                if l2.isEmpty { Text("No data").foregroundStyle(.secondary) }
                ForEach(l2) { r in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(r.symbol).font(.headline)
                            Spacer()
                            if let m = r.mid { Text(Fmt.money(m)).font(.caption) }
                        }
                        HStack {
                            if let s = r.spread_bps { Text("spread \(Fmt.num(s, decimals: 1))bps").font(.caption2) }
                            if let o = r.obi { Text("OBI \(Fmt.num(o, decimals: 3))").font(.caption2) }
                            Spacer()
                            Text(Fmt.relative(r.ts)).font(.caption2).foregroundStyle(.tertiary)
                        }
                    }
                }
            }
        }
    }

    private func verdictColor(_ v: String) -> Color {
        switch v.lowercased() {
        case "good", "pass", "ok": return .green
        case "bad", "fail": return .red
        default: return .orange
        }
    }

    private func load() async {
        do {
            switch tab {
            case .brier:
                let r: SmartsResponse<FeatureBrierRow> = try await session.api.request("/smarts/feature-brier", as: SmartsResponse<FeatureBrierRow>.self)
                self.brier = r.rows
            case .bandit:
                let r: SmartsResponse<BanditRow> = try await session.api.request("/smarts/bandit", as: SmartsResponse<BanditRow>.self)
                self.bandit = r.rows
            case .counterfactual:
                let r: SmartsResponse<CounterfactualRow> = try await session.api.request("/smarts/counterfactual", as: SmartsResponse<CounterfactualRow>.self)
                self.counterfactual = r.rows
            case .events:
                let r: SmartsResponse<UpcomingSmartEvent> = try await session.api.request("/smarts/upcoming-events", as: SmartsResponse<UpcomingSmartEvent>.self)
                self.events = r.rows
            case .drift:
                let r: SmartsResponse<DecisionDriftRow> = try await session.api.request("/smarts/decision-drift", as: SmartsResponse<DecisionDriftRow>.self)
                self.drift = r.rows
            case .judge:
                let r: SmartsResponse<ReasoningJudgeRow> = try await session.api.request("/smarts/reasoning-judge", as: SmartsResponse<ReasoningJudgeRow>.self)
                self.judge = r.rows
            case .shadow:
                let r: SmartsResponse<ShadowRow> = try await session.api.request("/smarts/shadow", as: SmartsResponse<ShadowRow>.self)
                self.shadow = r.rows
            case .leadlag, .onchain, .l2:
                break // require user input
            }
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }

    private func loadLeadLag() async {
        do {
            self.leadlag = try await session.api.request("/smarts/lead-lag/\(follower)", as: LeadLagResponse.self)
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }

    private func loadOnchain() async {
        do {
            let r: SmartsResponse<OnchainRow> = try await session.api.request("/smarts/onchain/\(asset)/\(metric)", as: SmartsResponse<OnchainRow>.self)
            self.onchain = r.rows
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }

    private func loadL2() async {
        do {
            let r: SmartsResponse<L2SnapshotRow> = try await session.api.request("/smarts/l2-snapshots/\(l2Symbol)", as: SmartsResponse<L2SnapshotRow>.self)
            self.l2 = r.rows
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }
}
