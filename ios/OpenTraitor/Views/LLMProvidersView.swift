import SwiftUI

struct LLMProvidersView: View {
    @EnvironmentObject var session: SessionStore
    @State private var providers: [LLMProviderConfig] = []
    @State private var loading = false
    @State private var saving = false
    @State private var error: String?
    @State private var showApiKeys = false
    @State private var apiKey = ""
    @State private var apiKeyEnv = ""
    @State private var credits: OpenRouterCreditsInfo?

    var body: some View {
        List {
            Section {
                Button {
                    showApiKeys = true
                } label: { Label("Set API key", systemImage: "key.fill") }
                if let c = credits, c.ok {
                    HStack {
                        Text("OpenRouter credits")
                        Spacer()
                        Text(Fmt.num(c.credits_remaining))
                    }
                }
            }
            ForEach($providers) { $p in
                Section(p.name) {
                    Toggle("Enabled", isOn: $p.enabled)
                    TextField("Model", text: $p.model)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                    TextField("Base URL", text: Binding(
                        get: { p.base_url ?? "" },
                        set: { newValue in
                            $p.base_url.wrappedValue = newValue.isEmpty ? nil : newValue
                        }
                    ))
                    .textInputAutocapitalization(.never)
                    .keyboardType(.URL)
                    if let s = p.live_status {
                        HStack {
                            Circle()
                                .fill(s.available ? Color.green : Color.red)
                                .frame(width: 8, height: 8)
                            Text(s.available ? "Available" : "Unavailable")
                                .font(.caption).foregroundStyle(.secondary)
                            Spacer()
                            if s.in_cooldown == true, let r = s.cooldown_remaining_s {
                                Text("Cooldown \(Int(r))s").font(.caption2).foregroundStyle(.orange)
                            }
                        }
                        if let cur = s.daily_tokens, let lim = s.daily_token_limit {
                            HStack {
                                Text("Tokens").font(.caption)
                                Spacer()
                                Text("\(cur)/\(lim)").font(.caption2)
                            }
                        }
                        if let cur = s.daily_requests, let lim = s.daily_request_limit {
                            HStack {
                                Text("Requests").font(.caption)
                                Spacer()
                                Text("\(cur)/\(lim)").font(.caption2)
                            }
                        }
                    }
                    if p.api_key_set == true {
                        Text("API key configured").font(.caption2).foregroundStyle(.green)
                    }
                }
            }
            ErrorBox(message: error)
            Section {
                Button {
                    Task { await save() }
                } label: {
                    HStack {
                        Text("Save changes")
                        Spacer()
                        if saving { ProgressView() }
                    }
                }
                .disabled(saving)
            }
        }
        .navigationTitle("LLM Providers")
        .refreshable { await load() }
        .task { await load() }
        .sheet(isPresented: $showApiKeys) {
            NavigationStack {
                Form {
                    TextField("Env name (e.g. OPENROUTER_API_KEY)", text: $apiKeyEnv)
                        .textInputAutocapitalization(.characters)
                        .autocorrectionDisabled()
                    SecureField("API key", text: $apiKey)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    Button("Save") {
                        Task { await saveApiKey() }
                    }
                    .disabled(apiKeyEnv.isEmpty || apiKey.isEmpty)
                }
                .navigationTitle("API key")
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Close") { showApiKeys = false }
                    }
                }
            }
        }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            let r: LLMProvidersResponse = try await session.api.request("/llm-providers", as: LLMProvidersResponse.self)
            self.providers = r.providers
            self.credits = try? await session.api.request("/llm-providers/openrouter-credits", as: OpenRouterCreditsInfo.self)
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }

    private func save() async {
        saving = true
        defer { saving = false }
        do {
            _ = try await session.api.sendWithConfirmation(
                "/llm-providers",
                method: "PUT",
                body: LLMProvidersUpdateBody(providers: providers),
                as: LLMProvidersUpdateResp.self
            )
        } catch { self.error = error.localizedDescription }
    }

    private func saveApiKey() async {
        do {
            _ = try await session.api.sendWithConfirmation(
                "/llm-providers/api-keys",
                method: "PUT",
                body: ApiKeysUpdateBody(keys: [apiKeyEnv: apiKey]),
                as: ApiKeysUpdateResp.self
            )
            apiKey = ""
            showApiKeys = false
            await load()
        } catch { self.error = error.localizedDescription }
    }
}
