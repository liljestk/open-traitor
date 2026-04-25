import SwiftUI

@main
struct OpenTraitorApp: App {
    @StateObject private var session = SessionStore()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(session)
                .environmentObject(session.api)
                .preferredColorScheme(.dark)
        }
    }
}
