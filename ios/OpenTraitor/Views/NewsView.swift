import SwiftUI

struct NewsView: View {
    @EnvironmentObject var session: SessionStore
    @State private var articles: [NewsArticle] = []
    @State private var loading = false
    @State private var error: String?

    var body: some View {
        List {
            if articles.isEmpty, !loading {
                ContentUnavailableView("No news", systemImage: "newspaper")
            }
            ForEach(articles) { a in
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text(a.sentiment.uppercased())
                            .font(.caption2.weight(.bold))
                            .padding(.horizontal, 6).padding(.vertical, 2)
                            .background(Fmt.sentimentColor(a.sentiment).opacity(0.2))
                            .foregroundStyle(Fmt.sentimentColor(a.sentiment))
                            .cornerRadius(4)
                        Spacer()
                        Text(a.source).font(.caption2).foregroundStyle(.secondary)
                        Text(Fmt.relative(a.published)).font(.caption2).foregroundStyle(.tertiary)
                    }
                    if let url = URL(string: a.url) {
                        Link(a.title, destination: url).font(.headline)
                    } else {
                        Text(a.title).font(.headline)
                    }
                    Text(a.summary).font(.caption).foregroundStyle(.secondary)
                    if let tags = a.tags, !tags.isEmpty {
                        Text(tags.joined(separator: " · "))
                            .font(.caption2).foregroundStyle(.tertiary)
                    }
                }
            }
            ErrorBox(message: error)
        }
        .navigationTitle("News")
        .refreshable { await load() }
        .task { await load() }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            let r: NewsResponse = try await session.api.request("/news", as: NewsResponse.self)
            self.articles = r.articles
            self.error = nil
        } catch { self.error = error.localizedDescription }
    }
}
