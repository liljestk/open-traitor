import SwiftUI

struct CommandsView: View {
    @EnvironmentObject var session: SessionStore
    @State private var pair = ""
    @State private var history: [TradeCommand] = []
    @State private var stops: [String: TrailingStopData] = [:]
    @State private var loading = false
    @State private var sending = false
    @State private var error: String?
    @State private var info: String?
    @State private var pendingAction: String?
    @State private var showConfirm = false

    var body: some View {
        List {
            Section("Issue command") {
                if !stops.isEmpty {
                    Picker("Pair", selection: $pair) {
                        Text("Choose…").tag("")
                        ForEach(stops.keys.sorted(), id: \.self) { Text($0).tag($0) }
                    }
                } else {
                    TextField("Pair (e.g. BTC-USD)", text: $pair)
                        .textInputAutocapitalization(.characters)
                        .autocorrectionDisabled()
                }
                Button {
                    pendingAction = "liquidate"; showConfirm = true
                } label: { Label("Liquidate", systemImage: "xmark.octagon").foregroundStyle(.red) }
                .disabled(pair.isEmpty || sending)
                Button {
                    pendingAction = "tighten_stop"; showConfirm = true
                } label: { Label("Tighten stop", systemImage: "arrow.down.right.square").foregroundStyle(.orange) }
                .disabled(pair.isEmpty || sending)
                Button {
                    pendingAction = "pause"; showConfirm = true
                } label: { Label("Pause trading", systemImage: "pause.circle") }
                .disabled(pair.isEmpty || sending)
                if let info { Text(info).font(.caption).foregroundStyle(.green) }
                ErrorBox(message: error)
            }
            Section("Recent commands") {
                if history.isEmpty { Text("No history").foregroundStyle(.secondary) }
                ForEach(history) { c in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(c.pair).font(.headline)
                            Text(c.action.uppercased()).font(.caption.weight(.bold)).foregroundStyle(.orange)
                        }
                        Spacer()
                        Text(Fmt.relative(c.ts)).font(.caption2).foregroundStyle(.secondary)
                    }
                }
            }
        }
        .navigationTitle("Commands")
        .refreshable { await load() }
        .task { await load() }
        .confirmationDialog("Send \(pendingAction ?? "") on \(pair)?",
                             isPresented: $showConfirm,
                             titleVisibility: .visible) {
            Button("Confirm", role: .destructive) {
                if let a = pendingAction { Task { await send(a) } }
            }
            Button("Cancel", role: .cancel) {}
        }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            async let h = try await session.api.request("/trade/commands/history", as: CommandsHistoryResp.self)
            async let s = try await session.api.request("/trailing-stops", as: TrailingStopsResp.self)
            self.history = try await h.commands
            self.stops = try await s.stops
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }

    private func send(_ action: String) async {
        sending = true
        defer { sending = false }
        info = nil
        do {
            struct Body: Encodable { let action: String }
            let resp: CommandActionResp = try await session.api.sendWithConfirmation(
                "/trade/\(pair)/command",
                method: "POST",
                body: Body(action: action),
                as: CommandActionResp.self
            )
            self.info = "Command \(action) → \(resp.status ?? "ok")"
            await load()
        } catch { self.error = error.localizedDescription }
    }
}
