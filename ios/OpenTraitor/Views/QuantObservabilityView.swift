import SwiftUI

struct QuantObservabilityView: View {
    @EnvironmentObject var session: SessionStore
    @State private var allocator: QuantAllocator?
    @State private var edges: QuantEdges?
    @State private var healing: QuantHealing?
    @State private var promotions: QuantPromotions?
    @State private var macro: MacroRegimeResponse?
    @State private var loading = false
    @State private var error: String?

    var body: some View {
        List {
            if let m = macro {
                Section("Macro regime") {
                    if let cons = m.consensus {
                        LabeledContent("Consensus", value: cons.regime)
                        Text(cons.rationale).font(.caption).foregroundStyle(.secondary)
                    }
                    if let profs = m.profiles {
                        ForEach(profs.sorted(by: { $0.key < $1.key }), id: \.key) { name, p in
                            HStack {
                                Text(name)
                                Spacer()
                                Text(p.regime ?? "?").font(.caption)
                                if let c = p.confidence {
                                    Text(Fmt.pct(c * 100)).font(.caption2).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                }
            }
            if let a = allocator {
                Section("Allocator") {
                    LabeledContent("Available", value: a.available ? "yes" : "no")
                    if let p = a.profile { LabeledContent("Profile", value: p) }
                    if let weights = a.weights {
                        ForEach(weights.sorted(by: { $0.key < $1.key }), id: \.key) { k, v in
                            HStack {
                                Text(k)
                                Spacer()
                                Text(Fmt.num(v, decimals: 3)).fontDesign(.monospaced)
                            }
                        }
                    }
                }
            }
            if let e = edges, let list = e.edges, !list.isEmpty {
                Section("Edges") {
                    ForEach(list) { ed in
                        VStack(alignment: .leading) {
                            HStack {
                                Text(ed.strategy).font(.headline)
                                if let r = ed.regime { Text("[\(r)]").font(.caption2).foregroundStyle(.secondary) }
                                Spacer()
                                if let s = ed.sharpe { Text("Sharpe \(Fmt.num(s))").font(.caption) }
                            }
                            HStack {
                                if let n = ed.count { Text("n=\(n)").font(.caption2) }
                                if let m = ed.mean_return { Text("mean \(Fmt.pct(m * 100))").font(.caption2) }
                                if let w = ed.win_rate { Text("win \(Fmt.pct(w * 100))").font(.caption2) }
                            }.foregroundStyle(.secondary)
                        }
                    }
                }
            }
            if let h = healing, let strats = h.strategies, !strats.isEmpty {
                Section("Strategy health") {
                    ForEach(strats) { s in
                        HStack {
                            Circle()
                                .fill(s.enabled == true ? Color.green : Color.red)
                                .frame(width: 8, height: 8)
                            VStack(alignment: .leading) {
                                Text(s.name).font(.headline)
                                if let cd = s.cooldown_remaining_s, cd > 0 {
                                    Text("Cooldown: \(Int(cd))s").font(.caption2).foregroundStyle(.orange)
                                }
                                if let tier = s.tier {
                                    Text("Tier: \(tier)").font(.caption2).foregroundStyle(.secondary)
                                }
                            }
                            Spacer()
                            if let pnl = s.recent_pnl {
                                Text(Fmt.money(pnl))
                                    .foregroundStyle(pnl >= 0 ? .green : .red)
                                    .font(.caption)
                            }
                        }
                    }
                }
            }
            if let p = promotions, let list = p.promotions, !list.isEmpty {
                Section("WFO promotions") {
                    ForEach(list) { pr in
                        VStack(alignment: .leading) {
                            HStack {
                                Text(pr.strategy).font(.headline)
                                Spacer()
                                if pr.promoted == true {
                                    Text("PROMOTED").font(.caption2.weight(.bold)).foregroundStyle(.green)
                                }
                            }
                            HStack {
                                Text(Fmt.relative(pr.ts)).font(.caption2).foregroundStyle(.secondary)
                                if let w = pr.wfe { Text("WFE \(Fmt.num(w, decimals: 3))").font(.caption2) }
                            }
                        }
                    }
                }
            }
            ErrorBox(message: error)
        }
        .navigationTitle("Quant")
        .refreshable { await load() }
        .task { await load() }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            async let a = try? await session.api.request("/quant/allocator", as: QuantAllocator.self)
            async let e = try? await session.api.request("/quant/edges", as: QuantEdges.self)
            async let h = try? await session.api.request("/quant/healing", as: QuantHealing.self)
            async let p = try? await session.api.request("/quant/promotions", as: QuantPromotions.self)
            async let m = try? await session.api.request("/quant/macro_regime", injectProfile: false, as: MacroRegimeResponse.self)
            self.allocator = await a
            self.edges = await e
            self.healing = await h
            self.promotions = await p
            self.macro = await m
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }
}
