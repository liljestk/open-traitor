import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var session: SessionStore
    let forceSetup: Bool

    @State private var serverURL: String = ""
    @State private var profile: String = "coinbase"
    @State private var saveOK: Bool = false
    @State private var savingError: String?
    @State private var refreshError: String?
    @State private var refreshing = false
    @State private var settings: SettingsResponse?

    var body: some View {
        Form {
            Section("Server") {
                TextField("Server URL (e.g. http://opentraitor:8090)", text: $serverURL)
                    .keyboardType(.URL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                Picker("Profile", selection: $profile) {
                    Text("Coinbase").tag("coinbase")
                    Text("IBKR").tag("ibkr")
                }
                Button {
                    save()
                } label: {
                    HStack {
                        Text("Save & connect")
                        Spacer()
                        if saveOK { Image(systemName: "checkmark.circle.fill").foregroundStyle(.green) }
                    }
                }
                ErrorBox(message: savingError)
            }

            if let s = settings {
                Section("Trading") {
                    LabeledContent("Trading enabled", value: s.trading_enabled ? "yes" : "no")
                }
                if let r = s.rpm_budget {
                    Section("RPM budget") {
                        if let p = r.provider { LabeledContent("Provider", value: p) }
                        if let m = r.model { LabeledContent("Model", value: m) }
                        if let rpm = r.rpm { LabeledContent("RPM", value: "\(rpm)") }
                        if let avail = r.available_per_cycle { LabeledContent("Per cycle", value: "\(avail)") }
                    }
                }
                Section("Sections") {
                    ForEach(s.sections, id: \.self) { name in
                        let label = s.section_labels[name] ?? name
                        let tier = s.schema[name]?.telegram_tier ?? "—"
                        HStack {
                            Text(label)
                            Spacer()
                            Text(tier).font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }
                Section("Presets") {
                    NavigationLink("Manage presets") { PresetsView() }
                }
            }

            if !forceSetup {
                Section {
                    Button {
                        Task { await refresh() }
                    } label: {
                        HStack {
                            Text("Refresh")
                            Spacer()
                            if refreshing { ProgressView() }
                        }
                    }
                    Button("Log out", role: .destructive) {
                        Task { await session.logout() }
                    }
                }
                ErrorBox(message: refreshError)
            }
        }
        .navigationTitle("Settings")
        .onAppear {
            if serverURL.isEmpty { serverURL = session.serverURLString }
            profile = session.profile
        }
        .task { await refresh() }
    }

    private func save() {
        session.serverURLString = serverURL.trimmingCharacters(in: .whitespaces)
        session.profile = profile
        session.applyConfig()
        saveOK = (session.api.baseURL != nil)
        savingError = saveOK ? nil : "Server URL is invalid."
        Task { await session.refresh() }
    }

    private func refresh() async {
        guard session.api.baseURL != nil else { return }
        refreshing = true
        defer { refreshing = false }
        do {
            self.settings = try await session.api.request("/settings", as: SettingsResponse.self)
            self.refreshError = nil
        } catch {
            self.refreshError = error.localizedDescription
        }
    }
}

struct PresetsView: View {
    @EnvironmentObject var session: SessionStore
    @State private var data: PresetsResponse?
    @State private var error: String?
    @State private var applying: String?

    var body: some View {
        List {
            if let presets = data?.presets {
                ForEach(presets.sorted(by: { $0.key < $1.key }), id: \.key) { key, info in
                    VStack(alignment: .leading, spacing: 6) {
                        Text(key).font(.headline)
                        Text(info.summary).font(.caption).foregroundStyle(.secondary)
                        Button {
                            Task { await apply(key) }
                        } label: {
                            HStack {
                                Text("Apply")
                                if applying == key { ProgressView() }
                            }
                        }
                        .buttonStyle(.bordered)
                        .disabled(applying != nil)
                    }
                    .padding(.vertical, 4)
                }
            }
            ErrorBox(message: error)
        }
        .navigationTitle("Presets")
        .task { await load() }
    }

    private func load() async {
        do {
            self.data = try await session.api.request("/settings/presets", as: PresetsResponse.self)
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }

    private func apply(_ key: String) async {
        applying = key
        defer { applying = nil }
        do {
            _ = try await session.api.sendWithConfirmation(
                "/settings",
                method: "PUT",
                body: SettingsUpdateRequest(section: nil, updates: nil, preset: key),
                as: SettingsUpdateResult.self
            )
        } catch { self.error = error.localizedDescription }
    }
}
