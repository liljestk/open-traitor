import SwiftUI

struct CrossAssetView: View {
    @EnvironmentObject var session: SessionStore
    @State private var tab: Tab = .correlations
    @State private var symbol: String = ""

    @State private var taxonomy: CrossAssetTaxonomyResponse?
    @State private var corrs: CrossAssetCorrelationsResponse?
    @State private var clusters: CrossAssetClustersResponse?
    @State private var regs: CrossAssetRegressionsResponse?
    @State private var cascade: CrossAssetCascadeResponse?
    @State private var driverEvent: String = ""
    @State private var error: String?

    enum Tab: String, CaseIterable, Identifiable {
        case correlations, clusters, taxonomy, regressions, cascade
        var id: String { rawValue }
    }

    var body: some View {
        VStack(spacing: 0) {
            Picker("Tab", selection: $tab) {
                ForEach(Tab.allCases) { Text($0.rawValue.capitalized).tag($0) }
            }
            .pickerStyle(.segmented)
            .padding(.horizontal)
            .padding(.top, 8)

            HStack {
                TextField("Symbol filter (optional)", text: $symbol)
                    .textInputAutocapitalization(.characters)
                    .autocorrectionDisabled()
                    .textFieldStyle(.roundedBorder)
                Button("Apply") { Task { await load() } }
                    .buttonStyle(.bordered)
            }
            .padding(.horizontal)
            .padding(.vertical, 6)

            List {
                ErrorBox(message: error)
                switch tab {
                case .correlations: correlationsSection
                case .clusters: clustersSection
                case .taxonomy: taxonomySection
                case .regressions: regressionsSection
                case .cascade: cascadeSection
                }
            }
        }
        .navigationTitle("Cross-asset")
        .refreshable { await load() }
        .task { await load() }
        .onChange(of: tab) { Task { await load() } }
    }

    @ViewBuilder private var correlationsSection: some View {
        if let rows = corrs?.rows, !rows.isEmpty {
            Section("Correlations (\(rows.count))") {
                ForEach(rows) { r in
                    HStack {
                        VStack(alignment: .leading) {
                            Text("\(r.base_symbol) ↔ \(r.peer_symbol)").font(.headline)
                            Text("\(r.window_days)d • n=\(r.sample_count) • lag=\(r.lead_lag_days)d")
                                .font(.caption2).foregroundStyle(.secondary)
                        }
                        Spacer()
                        VStack(alignment: .trailing) {
                            if let p = r.pearson { Text("ρ \(Fmt.num(p, decimals: 3))").font(.caption) }
                            if let s = r.spearman { Text("rs \(Fmt.num(s, decimals: 3))").font(.caption2).foregroundStyle(.secondary) }
                        }
                    }
                }
            }
        } else {
            Text("No correlations").foregroundStyle(.secondary)
        }
    }

    @ViewBuilder private var clustersSection: some View {
        if let cs = clusters?.clusters, !cs.isEmpty {
            ForEach(cs) { c in
                Section(c.label ?? "Cluster \(c.cluster_id)") {
                    if let coh = c.cohesion {
                        LabeledContent("Cohesion", value: Fmt.num(coh, decimals: 3))
                    }
                    Text(c.members.joined(separator: ", ")).font(.caption)
                }
            }
        } else {
            Text("No clusters").foregroundStyle(.secondary)
        }
    }

    @ViewBuilder private var taxonomySection: some View {
        if let rows = taxonomy?.rows, !rows.isEmpty {
            Section("Taxonomy (\(rows.count))") {
                ForEach(rows) { r in
                    VStack(alignment: .leading) {
                        Text(r.symbol).font(.headline)
                        Text("\(r.asset_class) • \(r.ecosystem ?? "—") • \(r.sector ?? "—")")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                }
            }
        } else {
            Text("No taxonomy data").foregroundStyle(.secondary)
        }
    }

    @ViewBuilder private var regressionsSection: some View {
        if let rows = regs?.rows, !rows.isEmpty {
            Section("Cross-event regressions (\(rows.count))") {
                ForEach(rows) { r in
                    VStack(alignment: .leading, spacing: 2) {
                        Text("\(r.driver_symbol):\(r.driver_event_type) → \(r.target_symbol)").font(.subheadline)
                        HStack {
                            if let b = r.beta { Text("β \(Fmt.num(b, decimals: 3))").font(.caption2) }
                            if let r2 = r.r_squared { Text("R² \(Fmt.num(r2, decimals: 3))").font(.caption2) }
                            if let h = r.hit_rate { Text("hit \(Fmt.pct(h * 100))").font(.caption2) }
                            Text("n=\(r.sample_count) h=\(r.horizon_days)d").font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }
            }
        } else {
            Text("No regressions").foregroundStyle(.secondary)
        }
    }

    @ViewBuilder private var cascadeSection: some View {
        Section("Cascade query") {
            TextField("Driver symbol", text: $symbol)
                .textInputAutocapitalization(.characters).autocorrectionDisabled()
            TextField("Driver event type", text: $driverEvent)
                .autocorrectionDisabled()
            Button("Run cascade") { Task { await loadCascade() } }
                .disabled(symbol.isEmpty || driverEvent.isEmpty)
        }
        if let c = cascade {
            Section("\(c.driver_symbol):\(c.driver_event_type) → predictions") {
                if c.predictions.isEmpty {
                    Text("No predictions").foregroundStyle(.secondary)
                }
                ForEach(c.predictions) { p in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(p.target_symbol).font(.headline)
                            Spacer()
                            if let d = p.expected_drift {
                                Text("drift \(Fmt.num(d, decimals: 4))")
                                    .font(.caption.weight(.bold))
                                    .foregroundStyle(d >= 0 ? .green : .red)
                            }
                        }
                        HStack {
                            if let b = p.beta { Text("β \(Fmt.num(b, decimals: 3))").font(.caption2) }
                            if let r2 = p.r_squared { Text("R² \(Fmt.num(r2, decimals: 3))").font(.caption2) }
                            if let h = p.hit_rate { Text("hit \(Fmt.pct(h * 100))").font(.caption2) }
                            Text("n=\(p.sample_count)").font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
    }

    private func load() async {
        let q = symbol.isEmpty ? [] : [URLQueryItem(name: "symbol", value: symbol)]
        do {
            switch tab {
            case .correlations:
                self.corrs = try await session.api.request("/cross-asset/correlations", query: q, as: CrossAssetCorrelationsResponse.self)
            case .clusters:
                self.clusters = try await session.api.request("/cross-asset/clusters", query: q, as: CrossAssetClustersResponse.self)
            case .taxonomy:
                self.taxonomy = try await session.api.request("/cross-asset/taxonomy", query: q, as: CrossAssetTaxonomyResponse.self)
            case .regressions:
                self.regs = try await session.api.request("/cross-asset/regressions", as: CrossAssetRegressionsResponse.self)
            case .cascade:
                break
            }
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }

    private func loadCascade() async {
        do {
            self.cascade = try await session.api.request(
                "/cross-asset/cascade",
                query: [
                    URLQueryItem(name: "driver_symbol", value: symbol),
                    URLQueryItem(name: "driver_event_type", value: driverEvent),
                ],
                as: CrossAssetCascadeResponse.self
            )
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }
}
