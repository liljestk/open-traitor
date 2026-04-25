import Foundation

/// Wraps a `URLSessionWebSocketTask` and pushes decoded `LiveEvent`s into an
/// `AsyncStream` so SwiftUI views can consume them with `for await`.
@MainActor
final class LiveSocket: ObservableObject {
    @Published private(set) var isConnected = false
    @Published private(set) var lastError: String?
    @Published private(set) var events: [LiveEvent] = []

    private var task: URLSessionWebSocketTask?
    private var receiveLoopTask: Task<Void, Never>?
    private weak var api: APIClient?
    private let maxBuffered = 200

    init(api: APIClient? = nil) {
        self.api = api
    }

    /// Replace the API client. Required for views that can only access the
    /// `EnvironmentObject` after the view appears (since `@StateObject`
    /// cannot reach environment values during `init`).
    func attach(api: APIClient) {
        self.api = api
    }

    func connect() {
        guard task == nil, let api else { return }
        guard let socket = api.makeLiveSocket() else {
            lastError = "Server URL not configured."
            return
        }
        self.task = socket
        socket.resume()
        isConnected = true
        lastError = nil
        receiveLoopTask = Task { [weak self] in
            await self?.receiveLoop()
        }
    }

    func disconnect() {
        receiveLoopTask?.cancel()
        receiveLoopTask = nil
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        isConnected = false
    }

    private func receiveLoop() async {
        guard let task else { return }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        while !Task.isCancelled, let task = self.task {
            do {
                let message = try await task.receive()
                let data: Data
                switch message {
                case .data(let d): data = d
                case .string(let s): data = Data(s.utf8)
                @unknown default: continue
                }
                if let event = try? decoder.decode(LiveEvent.self, from: data) {
                    events.insert(event, at: 0)
                    if events.count > maxBuffered {
                        events.removeLast(events.count - maxBuffered)
                    }
                }
            } catch {
                lastError = error.localizedDescription
                isConnected = false
                self.task = nil
                return
            }
        }
    }

    func clear() { events.removeAll() }
}
