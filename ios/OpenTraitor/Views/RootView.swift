import SwiftUI

struct RootView: View {
    @EnvironmentObject var session: SessionStore

    var body: some View {
        Group {
            if session.isCheckingAuth {
                ProgressView("Connecting…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if session.api.baseURL == nil {
                NavigationStack { SettingsView(forceSetup: true) }
            } else if session.pending2FAToken != nil {
                TwoFactorView()
            } else if !session.isAuthenticated {
                LoginView()
            } else {
                MainTabs()
            }
        }
        .animation(.default, value: session.isAuthenticated)
        .animation(.default, value: session.isCheckingAuth)
        .animation(.default, value: session.pending2FAToken)
    }
}

struct MainTabs: View {
    var body: some View {
        TabView {
            NavigationStack { OverviewView() }
                .tabItem { Label("Overview", systemImage: "chart.line.uptrend.xyaxis") }

            NavigationStack { LiveMonitorView() }
                .tabItem { Label("Live", systemImage: "dot.radiowaves.left.and.right") }

            NavigationStack { TradesView() }
                .tabItem { Label("Trades", systemImage: "arrow.left.arrow.right") }

            NavigationStack { WatchlistView() }
                .tabItem { Label("Watchlist", systemImage: "binoculars") }

            NavigationStack { MoreView() }
                .tabItem { Label("More", systemImage: "ellipsis.circle") }
        }
    }
}

struct MoreView: View {
    var body: some View {
        List {
            Section("Reasoning") {
                NavigationLink { CyclesView() } label: { Label("Cycles", systemImage: "brain") }
                NavigationLink { LLMAnalyticsView() } label: { Label("LLM Analytics", systemImage: "cpu") }
                NavigationLink { LearningView() } label: { Label("Learning (ALE)", systemImage: "graduationcap") }
                NavigationLink { PlanningView() } label: { Label("Planning Audit", systemImage: "calendar.badge.clock") }
            }

            Section("Markets") {
                NavigationLink { PredictionsView() } label: { Label("Predictions", systemImage: "scope") }
                NavigationLink { PatternsView() } label: { Label("Patterns", systemImage: "waveform.path.ecg") }
                NavigationLink { NewsView() } label: { Label("News", systemImage: "newspaper") }
                NavigationLink { FinancialCalendarView() } label: { Label("Calendar", systemImage: "calendar") }
            }

            Section("Risk & quant") {
                NavigationLink { RiskExposureView() } label: { Label("Risk Exposure", systemImage: "shield") }
                NavigationLink { CommandsView() } label: { Label("HITL Commands", systemImage: "exclamationmark.triangle") }
                NavigationLink { QuantObservabilityView() } label: { Label("Quant", systemImage: "function") }
                NavigationLink { BacktestingView() } label: { Label("Backtesting", systemImage: "tray.full") }
                NavigationLink { SimulatedTradesView() } label: { Label("Paper Trades", systemImage: "doc.text.magnifyingglass") }
            }

            Section("Analytics") {
                NavigationLink { AnalyticsView() } label: { Label("Analytics", systemImage: "chart.bar") }
                NavigationLink { SystemLogsView() } label: { Label("System Logs", systemImage: "doc.text") }
            }

            Section("Account") {
                NavigationLink { SettingsView(forceSetup: false) } label: { Label("Settings", systemImage: "gearshape") }
                NavigationLink { LLMProvidersView() } label: { Label("LLM Providers", systemImage: "antenna.radiowaves.left.and.right") }
                NavigationLink { PresetsView() } label: { Label("Presets", systemImage: "slider.horizontal.3") }
            }
        }
        .navigationTitle("More")
    }
}
