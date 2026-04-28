import SwiftUI

/// Hub mirroring the dashboard's Intelligence page — collects all model-intelligence views in one place.
struct IntelligenceView: View {
    var body: some View {
        List {
            Section("Per-symbol") {
                NavigationLink { SymbolInsightsView() } label: {
                    Label("Symbol Insights", systemImage: "magnifyingglass.circle")
                }
                NavigationLink { RegressionCatalogView() } label: {
                    Label("Regression Models", systemImage: "function")
                }
            }
            Section("Cross-asset") {
                NavigationLink { CrossAssetView() } label: {
                    Label("Cross-asset", systemImage: "point.3.connected.trianglepath.dotted")
                }
            }
            Section("Smarts") {
                NavigationLink { SmartsView() } label: {
                    Label("Smarts (Phases 1-8)", systemImage: "sparkles")
                }
                NavigationLink { RecommendationsView() } label: {
                    Label("Recommendations", systemImage: "checkmark.seal")
                }
            }
        }
        .navigationTitle("Intelligence")
    }
}
