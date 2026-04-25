import Foundation
import SwiftUI

@MainActor
final class SessionStore: ObservableObject {
    @Published var isAuthenticated = false
    @Published var isCheckingAuth = true
    @Published var systemStatus: SystemStatus?
    @Published var lastError: String?

    /// Set when the backend returns `requires_2fa`. UI navigates to TwoFactorView.
    @Published var pending2FAToken: String?

    @AppStorage("server_url") var serverURLString: String = ""
    @AppStorage("profile") var profile: String = "coinbase"
    @AppStorage("remember_password") var rememberPassword: Bool = false

    let api = APIClient()

    init() {
        applyConfig()
        Task { await refresh() }
    }

    func applyConfig() {
        api.profile = profile
        let trimmed = serverURLString.trimmingCharacters(in: .whitespaces)
        if let url = URL(string: trimmed), url.scheme != nil, url.host != nil {
            api.baseURL = url
        } else {
            api.baseURL = nil
        }
    }

    func refresh() async {
        isCheckingAuth = true
        defer { isCheckingAuth = false }
        guard api.baseURL != nil else { return }
        do {
            // Capture system status once (no auth needed for /system/status).
            systemStatus = try? await api.request(
                "/system/status",
                injectProfile: false,
                as: SystemStatus.self
            )
            let s = try await api.authStatus()
            isAuthenticated = s.authenticated
            if !isAuthenticated, rememberPassword,
               let pw = Keychain.get("dashboard_password"),
               !pw.isEmpty {
                try? await login(password: pw, persist: true)
            }
        } catch {
            lastError = error.localizedDescription
            isAuthenticated = false
        }
    }

    func login(password: String, persist: Bool) async throws {
        let resp = try await api.login(password: password)
        switch resp.status {
        case "ok":
            isAuthenticated = true
            pending2FAToken = nil
            if persist {
                rememberPassword = true
                Keychain.set(password, for: "dashboard_password")
            } else {
                rememberPassword = false
                Keychain.remove("dashboard_password")
            }
        case "requires_2fa":
            pending2FAToken = resp.pending_token
            isAuthenticated = false
        default:
            throw APIError.http(401, resp.message ?? "Login failed")
        }
    }

    func verify2FA(code: String, useBackupCode: Bool) async throws {
        guard let token = pending2FAToken else {
            throw APIError.http(400, "No pending 2FA session")
        }
        let resp = try await api.verify2FA(code: code, pendingToken: token, useBackupCode: useBackupCode)
        if resp.status == "ok" {
            pending2FAToken = nil
            isAuthenticated = true
        } else {
            throw APIError.http(401, resp.message ?? "2FA failed")
        }
    }

    func logout() async {
        await api.logout()
        Keychain.remove("dashboard_password")
        rememberPassword = false
        isAuthenticated = false
        pending2FAToken = nil
    }
}
