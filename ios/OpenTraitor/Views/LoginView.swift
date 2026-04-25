import SwiftUI

struct LoginView: View {
    @EnvironmentObject var session: SessionStore
    @State private var password = ""
    @State private var remember = true
    @State private var loggingIn = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("Connecting to") {
                    Text(session.serverURLString.isEmpty ? "Server not set" : session.serverURLString)
                        .font(.footnote).foregroundStyle(.secondary)
                    Text("Profile: \(session.profile)")
                        .font(.footnote).foregroundStyle(.secondary)
                }

                Section("Password") {
                    SecureField("Dashboard password", text: $password)
                        .textInputAutocapitalization(.never)
                        .submitLabel(.go)
                        .onSubmit { Task { await doLogin() } }
                    Toggle("Remember on this device", isOn: $remember)
                }

                Section {
                    Button {
                        Task { await doLogin() }
                    } label: {
                        HStack {
                            Text("Log in")
                            Spacer()
                            if loggingIn { ProgressView() }
                        }
                    }
                    .disabled(loggingIn || password.isEmpty)
                }

                ErrorBox(message: error)

                Section {
                    NavigationLink("Server settings") {
                        SettingsView(forceSetup: false)
                    }
                }
            }
            .navigationTitle("OpenTraitor")
        }
    }

    private func doLogin() async {
        loggingIn = true
        defer { loggingIn = false }
        do {
            try await session.login(password: password, persist: remember)
            password = ""
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}

struct TwoFactorView: View {
    @EnvironmentObject var session: SessionStore
    @State private var code = ""
    @State private var useBackup = false
    @State private var verifying = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("Two-factor authentication") {
                    Text("Enter the 6-digit code from your authenticator app.")
                        .font(.footnote).foregroundStyle(.secondary)
                    TextField(useBackup ? "Backup code" : "6-digit code", text: $code)
                        .keyboardType(useBackup ? .default : .numberPad)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .submitLabel(.go)
                        .onSubmit { Task { await verify() } }
                    Toggle("Use backup code", isOn: $useBackup)
                }

                Section {
                    Button {
                        Task { await verify() }
                    } label: {
                        HStack {
                            Text("Verify")
                            Spacer()
                            if verifying { ProgressView() }
                        }
                    }
                    .disabled(verifying || code.isEmpty)

                    Button("Cancel", role: .destructive) {
                        session.pending2FAToken = nil
                    }
                }

                ErrorBox(message: error)
            }
            .navigationTitle("Verify 2FA")
        }
    }

    private func verify() async {
        verifying = true
        defer { verifying = false }
        do {
            try await session.verify2FA(code: code, useBackupCode: useBackup)
            code = ""
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}
