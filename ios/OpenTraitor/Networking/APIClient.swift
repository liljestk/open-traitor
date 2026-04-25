import Foundation

enum APIError: LocalizedError {
    case badURL
    case http(Int, String)
    case decode(Error)
    case transport(Error)
    case unauthorized
    case notConfigured
    case requires2FA(pendingToken: String)
    case confirmationLoop

    var errorDescription: String? {
        switch self {
        case .badURL: return "Invalid server URL."
        case .http(let s, let m):
            let trimmed = m.count > 200 ? String(m.prefix(200)) + "…" : m
            return "HTTP \(s): \(trimmed)"
        case .decode(let e): return "Decode error: \(e.localizedDescription)"
        case .transport(let e): return e.localizedDescription
        case .unauthorized: return "Not authenticated."
        case .notConfigured: return "Open Settings and set the server URL."
        case .requires2FA: return "Two-factor authentication required."
        case .confirmationLoop: return "Update required confirmation but loop did not converge."
        }
    }
}

struct EmptyBody: Encodable {}

struct EmptyResponse: Decodable {
    init() {}
    init(from decoder: Decoder) throws {}
}

/// Generic envelope for endpoints that may either return a success payload or
/// `{ ok: false, confirmation_required: true, confirmation_token: "..." }`.
struct ConfirmationEnvelope<T: Decodable>: Decodable {
    let confirmationRequired: Bool
    let confirmationToken: String?
    let payload: T?

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(JSONValue.self)
        if case .object(let dict) = raw,
           case .some(.bool(true)) = dict["confirmation_required"] {
            self.confirmationRequired = true
            if case .some(.string(let token)) = dict["confirmation_token"] {
                self.confirmationToken = token
            } else {
                self.confirmationToken = nil
            }
            self.payload = nil
            return
        }
        self.confirmationRequired = false
        self.confirmationToken = nil
        let data = try JSONEncoder().encode(raw)
        self.payload = try JSONDecoder().decode(T.self, from: data)
    }
}

@MainActor
final class APIClient: ObservableObject {
    @Published var baseURL: URL?
    @Published var profile: String = "coinbase"
    @Published private(set) var csrfToken: String = ""

    private let session: URLSession

    init() {
        let cfg = URLSessionConfiguration.default
        cfg.httpCookieAcceptPolicy = .always
        cfg.httpShouldSetCookies = true
        cfg.httpCookieStorage = HTTPCookieStorage.shared
        cfg.requestCachePolicy = .reloadIgnoringLocalCacheData
        cfg.timeoutIntervalForRequest = 30
        cfg.timeoutIntervalForResource = 60
        self.session = URLSession(configuration: cfg)
    }

    // MARK: URL building

    /// `path` MUST start with `/` (relative to `/api`). For example,
    /// `"/auth/status"` becomes `<base>/api/auth/status`.
    func buildURL(_ path: String, query: [URLQueryItem] = [], injectProfile: Bool = true) throws -> URL {
        guard let base = baseURL else { throw APIError.notConfigured }
        let suffix = path.hasPrefix("/") ? String(path.dropFirst()) : path
        let full = base
            .appendingPathComponent("api")
            .appendingPathComponent(suffix)
        var comps = URLComponents(url: full, resolvingAgainstBaseURL: false)!
        var items = query
        if injectProfile, !profile.isEmpty {
            items.append(URLQueryItem(name: "profile", value: profile))
        }
        if !items.isEmpty { comps.queryItems = items }
        guard let u = comps.url else { throw APIError.badURL }
        return u
    }

    // MARK: Request execution

    private func makeRequest(url: URL, method: String, body: Encodable?) throws -> URLRequest {
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body, !(body is EmptyBody) {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONEncoder().encode(AnyEncodable(body))
        }
        if !csrfToken.isEmpty,
           ["POST", "PUT", "PATCH", "DELETE"].contains(method.uppercased()) {
            req.setValue(csrfToken, forHTTPHeaderField: "X-CSRF-Token")
        }
        return req
    }

    private static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .iso8601
        return d
    }()

    @discardableResult
    func request<T: Decodable>(
        _ path: String,
        method: String = "GET",
        query: [URLQueryItem] = [],
        body: Encodable? = nil,
        injectProfile: Bool = true,
        as type: T.Type = T.self
    ) async throws -> T {
        let url = try buildURL(path, query: query, injectProfile: injectProfile)
        let req = try makeRequest(url: url, method: method, body: body)

        do {
            let (data, resp) = try await session.data(for: req)
            guard let http = resp as? HTTPURLResponse else {
                throw APIError.http(-1, "Invalid response")
            }
            // CSRF refresh + retry on 403 for mutating verbs.
            if http.statusCode == 403,
               ["POST", "PUT", "PATCH", "DELETE"].contains(method.uppercased()) {
                _ = try? await refreshCSRF()
                let retry = try makeRequest(url: url, method: method, body: body)
                let (rdata, rresp) = try await session.data(for: retry)
                guard let rhttp = rresp as? HTTPURLResponse else {
                    throw APIError.http(-1, "Invalid retry response")
                }
                return try parseResponse(rdata, rhttp, as: T.self)
            }
            return try parseResponse(data, http, as: T.self)
        } catch let err as APIError {
            throw err
        } catch {
            throw APIError.transport(error)
        }
    }

    private func parseResponse<T: Decodable>(_ data: Data, _ http: HTTPURLResponse, as type: T.Type) throws -> T {
        if http.statusCode == 401 { throw APIError.unauthorized }
        guard (200..<300).contains(http.statusCode) else {
            let msg = String(data: data, encoding: .utf8) ?? ""
            throw APIError.http(http.statusCode, msg)
        }
        if T.self == EmptyResponse.self {
            return EmptyResponse() as! T
        }
        if data.isEmpty, let empty = EmptyResponse() as? T { return empty }
        do {
            return try Self.decoder.decode(T.self, from: data)
        } catch {
            throw APIError.decode(error)
        }
    }

    // MARK: Confirmation token loop

    func sendWithConfirmation<TBody: Encodable, TResp: Decodable>(
        _ path: String,
        method: String = "PUT",
        body: TBody,
        injectProfile: Bool = true,
        as type: TResp.Type = TResp.self
    ) async throws -> TResp {
        let env: ConfirmationEnvelope<TResp> = try await request(
            path, method: method, body: body, injectProfile: injectProfile,
            as: ConfirmationEnvelope<TResp>.self
        )
        if env.confirmationRequired, let token = env.confirmationToken {
            let merged = MergedBody(body: body, confirmationToken: token)
            let env2: ConfirmationEnvelope<TResp> = try await request(
                path, method: method, body: merged, injectProfile: injectProfile,
                as: ConfirmationEnvelope<TResp>.self
            )
            if let p = env2.payload { return p }
            throw APIError.confirmationLoop
        }
        if let p = env.payload { return p }
        throw APIError.confirmationLoop
    }

    private struct MergedBody<T: Encodable>: Encodable {
        let body: T
        let confirmationToken: String

        func encode(to encoder: Encoder) throws {
            try body.encode(to: encoder)
            var c = encoder.container(keyedBy: DynamicKey.self)
            try c.encode(confirmationToken, forKey: DynamicKey(stringValue: "confirmation_token")!)
        }

        struct DynamicKey: CodingKey {
            var stringValue: String
            var intValue: Int? { nil }
            init?(stringValue: String) { self.stringValue = stringValue }
            init?(intValue: Int) { return nil }
        }
    }

    // MARK: Auth

    @discardableResult
    func refreshCSRF() async throws -> AuthStatus {
        let s: AuthStatus = try await request(
            "/auth/status",
            injectProfile: false,
            as: AuthStatus.self
        )
        if let t = s.csrf_token { csrfToken = t }
        return s
    }

    func authStatus() async throws -> AuthStatus {
        try await refreshCSRF()
    }

    func login(password: String) async throws -> LoginResponse {
        let resp: LoginResponse = try await request(
            "/auth/login",
            method: "POST",
            body: LoginRequest(password: password),
            injectProfile: false,
            as: LoginResponse.self
        )
        if let t = resp.csrf_token { csrfToken = t }
        return resp
    }

    func verify2FA(code: String, pendingToken: String, useBackupCode: Bool = false) async throws -> LoginResponse {
        let body = TotpVerifyRequest(code: code, pending_token: pendingToken, use_backup_code: useBackupCode)
        let resp: LoginResponse = try await request(
            "/auth/2fa/verify",
            method: "POST",
            body: body,
            injectProfile: false,
            as: LoginResponse.self
        )
        if let t = resp.csrf_token { csrfToken = t }
        return resp
    }

    func logout() async {
        _ = try? await request("/auth/logout", method: "POST", injectProfile: false, as: EmptyResponse.self)
        csrfToken = ""
        if let url = baseURL,
           let cookies = HTTPCookieStorage.shared.cookies(for: url) {
            cookies.forEach { HTTPCookieStorage.shared.deleteCookie($0) }
        }
    }

    // MARK: WebSocket

    func makeLiveSocket() -> URLSessionWebSocketTask? {
        guard let base = baseURL else { return nil }
        let isHTTPS = base.scheme == "https"
        var comps = URLComponents()
        comps.scheme = isHTTPS ? "wss" : "ws"
        comps.host = base.host
        comps.port = base.port
        comps.path = "/ws/live"
        guard let url = comps.url else { return nil }
        let req = URLRequest(url: url)
        return session.webSocketTask(with: req)
    }

    // MARK: External URL builders (CSV export, etc.)

    func externalURL(_ path: String, query: [URLQueryItem] = [], injectProfile: Bool = true) -> URL? {
        try? buildURL(path, query: query, injectProfile: injectProfile)
    }
}

private struct AnyEncodable: Encodable {
    let value: Encodable
    init(_ value: Encodable) { self.value = value }
    func encode(to encoder: Encoder) throws { try value.encode(to: encoder) }
}
