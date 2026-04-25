import Foundation

// MARK: - Auth

struct LoginRequest: Encodable { let password: String }

struct TotpVerifyRequest: Encodable {
    let code: String
    let pending_token: String?
    let use_backup_code: Bool
}

struct LoginResponse: Decodable {
    let status: String          // "ok" | "requires_2fa"
    let csrf_token: String?
    let pending_token: String?
    let session_ttl: Int?
    let message: String?
}

struct AuthStatus: Decodable {
    let authenticated: Bool
    let auth_configured: Bool
    let csrf_token: String?
    let session_ttl: Int
    let twofa_enabled: Bool?
    let requires_2fa: Bool?
}

struct SystemStatus: Decodable {
    let setup_complete: Bool
    let auth_configured: Bool
    let authenticated: Bool
}

// MARK: - Stats / portfolio

struct StatsSummary: Decodable {
    let total_trades: Int
    let wins: Int
    let losses: Int
    let total_pnl: Double?
    let avg_pnl: Double?
    let best_trade: Double?
    let worst_trade: Double?
    let trades_24h: Int
    let pnl_24h: Double?
    let active_pairs: Int
    let cycles_24h: Int
    let win_rate: Double?
    let portfolio: PortfolioMini?
    let currency: String?

    struct PortfolioMini: Decodable {
        let portfolio_value: Double
        let total_pnl: Double
        let ts: String
    }
}

struct ExposureBreakdown: Decodable, Identifiable {
    var id: String { pair }
    let pair: String
    let quantity: Double
    let entry_price: Double
    let current_price: Double
    let value: Double
    let pct_of_portfolio: Double
    let pnl_pct: Double
}

struct PortfolioExposure: Decodable {
    let portfolio_value: Double
    let cash_balance: Double
    let return_pct: Double
    let total_pnl: Double
    let max_drawdown: Double
    let fear_greed_value: Double?
    let high_stakes_active: Bool?
    let breakdown: [ExposureBreakdown]
    let cash_pct: Double
    let allocated_pct: Double
    let ts: String
}

struct ExposureResponse: Decodable {
    let exposure: PortfolioExposure?
}

struct PortfolioSnapshot: Decodable, Identifiable {
    var id: String { ts }
    let ts: String
    let portfolio_value: Double
    let return_pct: Double
    let total_pnl: Double
}

struct PortfolioHistoryResponse: Decodable {
    let history: [PortfolioSnapshot]
    let count: Int
}

// MARK: - Analytics

struct TradeStatsDetail: Decodable {
    let total_trades: Int
    let winning: Int
    let losing: Int
    let pending: Int
    let total_pnl: Double
    let best_pnl: Double
    let worst_pnl: Double
    let avg_pnl: Double
    let total_volume: Double
    let total_fees: Double
    let avg_confidence: Double
}

struct PortfolioRange: Decodable {
    let low: Double
    let high: Double
    let avg: Double
    let samples: Int
}

struct WinLossStats: Decodable {
    let win_rate: Double
    let avg_win: Double
    let avg_loss: Double
    let sample_size: Int
}

struct DailySummary: Decodable, Identifiable {
    var id: String { date }
    let date: String
    let ts: String
    let opening_value: Double
    let closing_value: Double
    let high_value: Double
    let low_value: Double
    let total_trades: Int
    let winning_trades: Int
    let losing_trades: Int
    let total_pnl: Double
    let best_trade: Double
    let worst_trade: Double
    let events_count: Int
    let summary_text: String?
    let plan_text: String?
}

struct BestWorstTrade: Decodable, Identifiable {
    var id: String { "\(pair)-\(ts)-\(action)" }
    let pair: String
    let action: String
    let pnl: Double
    let price: Double
    let quote_amount: Double
    let ts: String
}

struct AnalyticsPerformance: Decodable {
    let trade_stats: TradeStatsDetail
    let portfolio_range: PortfolioRange
    let event_counts: [String: Int]
    let recent_trades: [Trade]
}

struct AnalyticsBestWorst: Decodable {
    let best: [BestWorstTrade]
    let worst: [BestWorstTrade]
}

struct AnalyticsData: Decodable {
    let performance: AnalyticsPerformance
    let best_worst: AnalyticsBestWorst
    let daily_summaries: [DailySummary]
    let win_loss: WinLossStats
    let portfolio_range: PortfolioRange
}

// MARK: - Trades

struct Trade: Decodable, Identifiable {
    let id: Int
    let ts: String
    let pair: String
    let action: String
    let price: Double
    let quote_amount: Double
    let pnl: Double?
    let confidence: Double?
    let quantity: Double?
    let fee_quote: Double?
    let signal_type: String?
    let stop_loss: Double?
    let take_profit: Double?
    let reasoning: String?
    let is_rotation: Int?
    let approved_by: String?
    let cycle_id: String?
}

struct TradesResponse: Decodable {
    let trades: [Trade]
    let count: Int
}

struct SyncResponse: Decodable {
    let synced: Int
    let total_exchange: Int?
    let error: String?
}

// MARK: - Cycles & events

struct CycleSummary: Decodable, Identifiable {
    var id: String { cycle_id }
    let cycle_id: String
    let pair: String
    let started_at: String
    let finished_at: String
    let agent_count: Int
    let signal_type: String?
    let confidence: Double?
    let action: String?
    let trade_id: Int?
    let pnl: Double?
    let quote_amount: Double?
    let price: Double?
    let langfuse_trace_id: String?
    let langfuse_url: String?
    let total_prompt_tokens: Int?
    let total_completion_tokens: Int?
    let total_latency_ms: Int?
    let cycle_duration_ms: Int?
}

struct CyclesResponse: Decodable {
    let cycles: [CycleSummary]
    let count: Int
}

struct AgentSpan: Decodable, Identifiable {
    let id: Int
    let ts: String
    let agent_name: String
    let reasoning_json: JSONValue?
    let signal_type: String?
    let confidence: Double?
    let langfuse_trace_id: String?
    let langfuse_span_id: String?
    let prompt_tokens: Int?
    let completion_tokens: Int?
    let latency_ms: Int?
    let raw_prompt: String?
    let pair: String
}

struct CycleFull: Decodable {
    let cycle_id: String
    let pair: String
    let started_at: String
    let finished_at: String
    let total_latency_ms: Int
    let total_tokens: Int
    let langfuse_trace_id: String?
    let langfuse_url: String?
    let spans: [AgentSpan]
    let trade: Trade?
    let decision_outcome: String
    let decision_reason: String
}

struct EventLog: Decodable, Identifiable {
    let id: Int
    let ts: String
    let event_type: String
    let severity: String
    let pair: String?
    let message: String
    let data: JSONValue?
}

struct EventsResponse: Decodable {
    let events: [EventLog]
    let count: Int
}

// MARK: - Predictions

struct PredictionPairAccuracy: Decodable {
    let total: Int
    let correct_24h: Int
    let evaluated_24h: Int
    let correct_1h: Int?
    let evaluated_1h: Int?
    let accuracy_24h_pct: Double?
    let accuracy_1h_pct: Double?
}

struct PredictionAccuracyOverall: Decodable {
    let total: Int
    let correct_24h: Int
    let evaluated_24h: Int
    let correct_1h: Int
    let evaluated_1h: Int
    let accuracy_24h_pct: Double?
    let accuracy_1h_pct: Double?
}

struct ConfidenceBucket: Decodable, Identifiable {
    var id: String { confidence_range }
    let confidence_range: String
    let total: Int
    let correct: Int
    let evaluated: Int
    let accuracy_pct: Double?
}

struct DailyAccuracy: Decodable, Identifiable {
    var id: String { date }
    let date: String
    let total: Int
    let correct: Int
    let evaluated: Int
    let accuracy_pct: Double?
}

struct SignalTypeAccuracy: Decodable {
    let total: Int
    let correct_24h: Int
    let evaluated_24h: Int
    let accuracy_pct: Double?
    let weight: Double?
}

struct PredictionRecord: Decodable, Identifiable {
    var id: String { "\(ts)-\(pair)-\(signal_type)" }
    let ts: String
    let pair: String
    let signal_type: String
    let confidence: Double
    let entry_price: Double
    let suggested_tp: Double?
    let suggested_sl: Double?
}

struct PredictionAccuracyResponse: Decodable {
    let predictions: [PredictionRecord]
    let per_pair: [String: PredictionPairAccuracy]
    let overall: PredictionAccuracyOverall
    let by_signal_type: [String: SignalTypeAccuracy]
    let confidence_calibration: [ConfidenceBucket]
    let daily_accuracy: [DailyAccuracy]
}

struct TrackedPair: Decodable, Identifiable {
    var id: String { pair }
    let pair: String
    let prediction_count: Int
    let last_predicted: String
    let signal_types: [String]
    let source: String   // "ai" | "human" | "both"
}

struct TrackedPairsData: Decodable {
    let crypto: [TrackedPair]
    let equity: [TrackedPair]
    let total_pairs: Int
}

struct PricePoint: Decodable {
    let ts: String
    let price: Double
}

struct PredictionMarker: Decodable {
    let ts: String
    let signal_type: String
    let confidence: Double
    let entry_price: Double
    let suggested_tp: Double?
    let suggested_sl: Double?
    let is_bullish: Bool
}

struct PairPredictionHistory: Decodable {
    let pair: String
    let price_history: [PricePoint]
    let predictions: [PredictionMarker]
    let total_predictions: Int
}

// MARK: - Market

struct CoinbaseProduct: Decodable, Identifiable {
    var id: String { id_ }
    let id_: String
    let base: String
    let quote: String

    enum CodingKeys: String, CodingKey {
        case id_ = "id"
        case base, quote
    }
}

struct ProductsResponse: Decodable { let products: [CoinbaseProduct] }

struct ProductSearchResult: Decodable, Identifiable {
    var id: String { id_ }
    let id_: String
    let base: String
    let quote: String
    let display_name: String?
    let volume_24h: Double?
    let price_change_24h: Double?

    enum CodingKeys: String, CodingKey {
        case id_ = "id"
        case base, quote, display_name, volume_24h, price_change_24h
    }
}

struct ProductSearchResponse: Decodable {
    let results: [ProductSearchResult]
    let query: String
}

struct CandleData: Decodable, Identifiable {
    var id: String { start }
    let start: String
    let time: String?
    let low: Double
    let high: Double
    let open: Double
    let close: Double
    let volume: Double
}

struct CandlesResponse: Decodable {
    let candles: [CandleData]
    let pair: String
}

struct MarketPriceResponse: Decodable {
    let pair: String
    let price: Double
    let ts: String
}

// MARK: - Watchlist

struct PairInfo: Decodable, Identifiable {
    var id: String { pair }
    let pair: String
    let followed_by_llm: Bool
    let followed_by_human: Bool
    let price: Double?
}

struct ScanResult: Decodable {
    let ts: String?
    let universe_size: Int?
    let scanned_pairs: Int?
    let summary_text: String?
}

struct RpmBudget: Decodable {
    let provider: String?
    let model: String?
    let tier: String?
    let rpm: Int?
    let interval: Int?
    let available_per_cycle: Int?
}

struct WatchlistData: Decodable {
    let active_pairs: [String]
    let human_followed_pairs: [String]
    let pair_info: [PairInfo]
    let live_prices: [String: Double]
    let scan: ScanResult?
    let pair_count: Int
    let rpm_budget: RpmBudget?
}

struct FollowResponse: Decodable {
    let ok: Bool
    let pair: String
    let followed_by: String?
    let exchange: String?
}

struct UnfollowResponse: Decodable {
    let ok: Bool
    let pair: String
    let unfollowed: Bool?
}

struct FollowRequest: Encodable {
    let pair: String
    let exchange: String?
}

// MARK: - Strategic / Temporal

struct StrategicPlan: Decodable, Identifiable {
    let id: Int
    let horizon: String
    let plan_json: JSONValue?
    let summary_text: String
    let ts: String
    let langfuse_trace_id: String?
    let langfuse_url: String?
    let temporal_workflow_id: String?
    let temporal_run_id: String?
}

struct StrategicResponse: Decodable {
    let plans: [StrategicPlan]
    let count: Int
}

struct TemporalRun: Decodable, Identifiable {
    var id: String { "\(workflow_id)-\(run_id)" }
    let workflow_id: String
    let run_id: String
    let workflow_type: String
    let status: String
    let start_time: String?
    let close_time: String?
}

struct TemporalRunsResponse: Decodable {
    let runs: [TemporalRun]
    let count: Int
}

struct TemporalEvent: Decodable, Identifiable {
    var id: Int { event_id }
    let event_id: Int
    let event_type: String
    let event_time: String?
    let attributes: [String: String]?
}

struct TemporalReplay: Decodable {
    let workflow_id: String
    let run_id: String
    let event_count: Int
    let langfuse_trace_id: String?
    let langfuse_url: String?
    let events: [TemporalEvent]
}

// MARK: - Settings

struct FieldSchema: Decodable {
    let type: String
    let min: Double?
    let max: Double?
    let `enum`: [String]?
}

struct NestedSection: Decodable {
    let fields: [String: FieldSchema]
}

struct SectionSchema: Decodable {
    let label: String
    let telegram_tier: String   // "safe" | "semi_safe" | "blocked"
    let fields: [String: FieldSchema]?
    let nested: [String: NestedSection]?
}

struct TelegramTierInfo: Decodable {
    let sections: [String]
    let description: String
}

struct SettingsResponse: Decodable {
    let settings: [String: JSONValue]
    let trading_enabled: Bool
    let sections: [String]
    let section_labels: [String: String]
    let telegram_tiers: [String: TelegramTierInfo]
    let schema: [String: SectionSchema]
    let rpm_budget: RpmBudget?
}

struct SettingsUpdateRequest: Encodable {
    let section: String?
    let updates: [String: JSONValue]?
    let preset: String?
}

struct SettingsUpdateResult: Decodable {
    let ok: Bool
    let preset: String?
    let section: String?
    let changes: [String: JSONValue]?
    let trading_enabled: Bool?
}

struct PresetInfo: Decodable {
    let values: [String: JSONValue]
    let summary: String
}

struct PresetsResponse: Decodable {
    let presets: [String: PresetInfo]
    let current_enabled: JSONValue?
}

struct StyleModifierMeta: Decodable {
    let label: String
    let desc: String
    let exchanges: [String]
    let icon: String
}

struct StyleModifiersResponse: Decodable {
    let modifiers: [String: StyleModifierMeta]
    let active: [String]
    let asset_class: String
}

struct LLMProviderLiveStatus: Codable {
    let name: String
    let model: String
    let is_local: Bool
    let available: Bool
    let tier: String?
    let in_cooldown: Bool?
    let cooldown_remaining_s: Double?
    let daily_tokens: Int?
    let daily_token_limit: Int?
    let daily_requests: Int?
    let daily_request_limit: Int?
    let rpm_limit: Int?
    let rpm_current: Int?
    let credits_remaining: Double?
    let is_free_model: Bool?
}

struct LLMProviderConfig: Codable, Identifiable {
    var id: String { name }
    let name: String
    var enabled: Bool
    var model: String
    var base_url: String?
    var base_url_env: String?
    var api_key_env: String?
    var model_env: String?
    var timeout: Double?
    var rpm_limit: Int?
    var daily_token_limit: Int?
    var daily_request_limit: Int?
    var cooldown_seconds: Double?
    var is_local: Bool?
    var tier: String?
    var api_key_set: Bool?
    var live_status: LLMProviderLiveStatus?
}

struct LLMProvidersResponse: Decodable { let providers: [LLMProviderConfig] }
struct LLMProvidersUpdateBody: Encodable { let providers: [LLMProviderConfig] }
struct LLMProvidersUpdateResp: Decodable { let ok: Bool; let providers: [LLMProviderConfig] }

struct ApiKeysUpdateBody: Encodable { let keys: [String: String] }
struct ApiKeysUpdateResp: Decodable { let ok: Bool; let updated: [String]? }

struct OpenRouterCreditsInfo: Decodable {
    let ok: Bool
    let error: String?
    let credits_remaining: Double?
    let usage: Double?
    let is_free_tier: Bool?
    let label: String?
}

// MARK: - News

struct NewsArticle: Decodable, Identifiable {
    let id: String
    let title: String
    let summary: String
    let source: String
    let url: String
    let published: String
    let sentiment: String      // "bullish" | "bearish" | "neutral"
    let relevance_score: Double?
    let tags: [String]?
}

struct NewsResponse: Decodable {
    let articles: [NewsArticle]
    let count: Int
    let source: String?
}

// MARK: - Financial calendar

struct FinancialEvent: Decodable, Identifiable {
    var id: String { "\(type)-\(ticker)-\(date)" }
    let type: String       // earnings | dividend | macro
    let ticker: String
    let date: String
    let days_away: Int
    let details: JSONValue?
    let importance: String  // high | medium | low
}

struct FinancialCalendarData: Decodable {
    let domain: String      // equity | crypto
    let events: [FinancialEvent]?
    let earnings: [FinancialEvent]?
    let dividends: [FinancialEvent]?
    let macro: [FinancialEvent]?
    let earnings_season: JSONValue?
}

struct FinancialSummaryData: Decodable {
    let ticker: String
    let company: String?
    let summary: String?
    let metrics: JSONValue?
    let generated_at: String?
}

// MARK: - Patterns

struct CatalystEvent: Decodable, Identifiable {
    let id: String
    let exchange: String
    let symbol: String
    let event_type: String
    let event_ts: String
    let source: String?
    let confidence: Double?
    let metadata: JSONValue?
    let inserted_at: String?
}

struct PatternOutcomeSummary: Decodable {
    let n: Int?
    let mean_return: Double?
    let median_return: Double?
    let win_rate: Double?
    let sample_window_days: Int?
    let confidence: Double?
}

struct UpcomingPatternRow: Decodable, Identifiable {
    var id: String { upcoming_event.id }
    let symbol: String
    let exchange: String
    let upcoming_event: CatalystEvent
    let outcome: PatternOutcomeSummary?
}

struct UpcomingPatternsResponse: Decodable {
    let profile: String
    let exchange: String
    let horizon_days: Int
    let count: Int
    let items: [UpcomingPatternRow]
}

struct EventMatchesResponse: Decodable {
    let profile: String
    let exchange: String
    let event: CatalystEvent
    let outcome: PatternOutcomeSummary?
}

struct PatternBackfillRow: Decodable, Identifiable {
    var id: String { "\(symbol)-\(started_at ?? "")" }
    let symbol: String?
    let status: String?
    let processed_events: Int?
    let total_events: Int?
    let started_at: String?
    let updated_at: String?
}

struct PatternStatusResponse: Decodable {
    let profile: String
    let exchange: String
    let ready: Bool
    let counts: [String: Int]?
    let recent_backfills: [PatternBackfillRow]?
}

// MARK: - LLM analytics

struct LLMAnalyticsSummary: Decodable {
    let total_calls: Int
    let total_prompt_tokens: Int
    let total_completion_tokens: Int
    let total_tokens: Int
    let avg_latency_ms: Double?
    let avg_prompt_tokens: Double?
    let avg_completion_tokens: Double?
    let avg_total_tokens: Double?
    let max_latency_ms: Double?
    let min_latency_ms: Double?
    let p50_latency_ms: Double?
    let p90_latency_ms: Double?
    let p99_latency_ms: Double?
    let unique_pairs: Int
    let total_cycles: Int
    let runtime_total_calls: Int?
    let runtime_total_tokens: Int?
}

struct LLMTimeBucket: Decodable, Identifiable {
    var id: String { bucket }
    let bucket: String
    let calls: Int
    let prompt_tokens: Int
    let completion_tokens: Int
    let total_tokens: Int
    let avg_latency_ms: Double?
}

struct LLMAgentStat: Decodable, Identifiable {
    var id: String { agent_name }
    let agent_name: String
    let calls: Int
    let total_tokens: Int
    let avg_latency_ms: Double?
}

struct LLMExchangeStat: Decodable, Identifiable {
    var id: String { exchange }
    let exchange: String
    let calls: Int
    let total_tokens: Int
}

struct LLMPairStat: Decodable, Identifiable {
    var id: String { pair }
    let pair: String
    let calls: Int
    let total_tokens: Int
}

struct LLMProviderRuntimeStat: Decodable, Identifiable {
    var id: String { provider }
    let provider: String
    let calls: Int
    let total_tokens: Int
    let avg_latency_ms: Double?
}

struct LLMAnalyticsData: Decodable {
    let summary: LLMAnalyticsSummary
    let time_series: [LLMTimeBucket]
    let by_agent: [LLMAgentStat]
    let by_exchange: [LLMExchangeStat]
    let top_pairs: [LLMPairStat]
    let providers: [LLMProviderRuntimeStat]
    let hours: Int
    let bucket: String       // hourly | daily | weekly
}

struct OptimizerData: Decodable {
    let settings: JSONValue
    let defaults: JSONValue?
    let param_meta: JSONValue?
    let history: JSONValue?
    let context: JSONValue?
    let hours: Int?
}

struct OptimizerApplyBody: Encodable { let settings: [String: JSONValue] }
struct OptimizerApplyResp: Decodable {
    let ok: Bool
    let applied: JSONValue?
    let changes: JSONValue?
}

// MARK: - Backtesting

struct BacktestRunSummary: Decodable, Identifiable {
    let id: Int
    let run_ts: String
    let pair: String
    let exchange: String?
    let days: Int
    let total_return_pct: Double?
    let sharpe_ratio: Double?
    let win_rate: Double?
    let total_trades: Int?
    let max_drawdown_pct: Double?
    let alpha: Double?
    let is_wfo: Bool?
    let wfo_wfe: Double?
}

struct BacktestHistoryResponse: Decodable {
    let runs: [BacktestRunSummary]
}

struct BacktestRunDetail: Decodable {
    let id: Int
    let run_ts: String
    let pair: String
    let exchange: String?
    let days: Int
    let params_json: JSONValue?
    let result_json: JSONValue?
    let total_return_pct: Double?
    let sharpe_ratio: Double?
    let win_rate: Double?
    let total_trades: Int?
    let max_drawdown_pct: Double?
}

struct BacktestTriggerRequest: Encodable {
    let pair: String
    let days: Int
    let position_size_pct: Double?
    let trailing_stop_pct: Double?
    let entry_threshold: Double?
    let fee_pct: Double?
    let slippage_pct: Double?
}

struct BacktestTriggerResp: Decodable {
    let run_id: Int?
    let status: String?
    let error: String?
}

struct BacktestPairsResp: Decodable {
    let pairs: [String]
}

// MARK: - Quant observability

struct QuantAllocator: Decodable {
    let available: Bool
    let profile: String?
    let weights: [String: Double]?
    let state: JSONValue?
}

struct EdgeStats: Decodable, Identifiable {
    var id: String { "\(strategy)-\(regime ?? "")" }
    let strategy: String
    let regime: String?
    let count: Int?
    let mean_return: Double?
    let win_rate: Double?
    let sharpe: Double?
}

struct QuantEdges: Decodable {
    let available: Bool
    let profile: String?
    let regime: String?
    let edges: [EdgeStats]?
}

struct StrategyStatus: Decodable, Identifiable {
    var id: String { name }
    let name: String
    let tier: String?
    let enabled: Bool?
    let cooldown_remaining_s: Double?
    let recent_pnl: Double?
}

struct QuantHealing: Decodable {
    let available: Bool
    let profile: String?
    let tiers: JSONValue?
    let strategies: [StrategyStatus]?
}

struct WFOPromotion: Decodable, Identifiable {
    var id: String { "\(strategy)-\(ts)" }
    let ts: String
    let strategy: String
    let from_params: JSONValue?
    let to_params: JSONValue?
    let wfe: Double?
    let promoted: Bool?
}

struct QuantPromotions: Decodable {
    let available: Bool
    let profile: String?
    let promotions: [WFOPromotion]?
    let count: Int?
}

struct MacroRegimeProfile: Decodable {
    let regime: String?
    let confidence: Double?
    let atr_pct: Double?
    let slope: Double?
    let ts: String?
}

struct MacroRegimeResponse: Decodable {
    let available: Bool
    let ts: String?
    let profiles: [String: MacroRegimeProfile]?
    let consensus: MacroConsensus?
    let error: String?

    struct MacroConsensus: Decodable {
        let regime: String
        let rationale: String
    }
}

// MARK: - Commands

struct TradeCommand: Decodable, Identifiable {
    var id: String { "\(ts)-\(pair)-\(action)" }
    let ts: String
    let pair: String
    let action: String     // liquidate | tighten_stop | pause
    let status: String?
}

struct CommandsHistoryResp: Decodable {
    let commands: [TradeCommand]
}

struct TrailingStopTier: Decodable {
    let trigger_pct: Double?
    let exit_fraction: Double?
    let triggered: Bool?
    let trigger_price: Double?
}

struct TrailingStopData: Decodable {
    let pair: String?
    let entry_price: Double?
    let trail_pct: Double?
    let stop_price: Double?
    let triggered: Bool?
    let highest_price: Double?
    let total_quantity: Double?
    let remaining_quantity: Double?
    let tiers: [TrailingStopTier]?
}

struct TrailingStopsResp: Decodable {
    let stops: [String: TrailingStopData]
    let source: String?
}

struct CommandActionResp: Decodable {
    let status: String?
    let action: String?
    let pair: String?
}

// MARK: - Simulated trades

struct SimulatedTrade: Decodable, Identifiable {
    let id: Int
    let ts: String
    let pair: String
    let from_currency: String
    let to_currency: String
    let from_amount: Double
    let entry_price: Double
    let current_price: Double
    let quantity: Double
    let pnl_abs: Double
    let pnl_pct: Double
    let status: String       // open | closed
    let closed_at: String?
    let close_price: Double?
    let close_pnl_abs: Double?
    let close_pnl_pct: Double?
    let notes: String?
}

struct SimulatedTradesResp: Decodable {
    let simulations: [SimulatedTrade]
    let count: Int
}

struct CreateSimulatedBody: Encodable {
    let pair: String
    let from_currency: String
    let from_amount: Double
    let notes: String?
}

// MARK: - Learning

struct LearningStatus: Decodable {
    let enabled: Bool
    let subsystems: [String: JSONValue]?
    let total_runs: Int?
    let total_errors: Int?
}

struct LearningAccuracyTrend: Decodable, Identifiable {
    var id: String { date }
    let date: String
    let total: Int
    let correct: Int
    let accuracy: Double?
}

struct LearningAccuracyTrends: Decodable {
    let days: Int
    let data: [LearningAccuracyTrend]
}

struct EnsembleWeight: Decodable, Identifiable {
    var id: String { strategy }
    let strategy: String
    let weight: Double
    let confidence: Double?
}

struct LearningEnsemble: Decodable {
    let active: [EnsembleWeight]
    let history: JSONValue?
}

struct CalibrationPoint: Decodable, Identifiable {
    var id: Double { predicted }
    let predicted: Double
    let actual_accuracy: Double
}

struct CalibrationModel: Decodable, Identifiable {
    var id: String { model }
    let model: String
    let brier_score: Double?
    let log_loss: Double?
}

struct LearningCalibration: Decodable {
    let curve: [CalibrationPoint]
    let models: [CalibrationModel]?
}

struct LearningLessonsResp: Decodable {
    let lessons: [JSONValue]?
}

// MARK: - WS live event

struct LiveEvent: Decodable {
    let type: String
    let cycle_id: String?
    let pair: String?
    let exchange: String?
    let agent_name: String?
    let model: String?
    let latency_ms: Double?
    let prompt_tokens: Int?
    let completion_tokens: Int?
    let langfuse_trace_id: String?
    let ts: String?
}

// MARK: - Setup wizard

struct SetupConfig: Decodable {
    let setup_complete: Bool?
    let profiles: JSONValue?
    let raw: JSONValue?

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(JSONValue.self)
        self.raw = raw
        if case .object(let dict) = raw {
            if case .some(.bool(let b)) = dict["setup_complete"] {
                self.setup_complete = b
            } else { self.setup_complete = nil }
            self.profiles = dict["profiles"]
        } else {
            self.setup_complete = nil
            self.profiles = nil
        }
    }
}

// MARK: - Executive summary

struct ExecutiveProfileRow: Decodable, Identifiable {
    var id: String { profile }
    let profile: String
    let trades: Int
    let pnl: Double
    let active_pairs_24h: Int
}

struct ExecutiveCombined: Decodable {
    let total_trades: Int
    let total_pnl: Double
    let total_active_pairs_24h: Int
}

struct ExecutiveSummaryResponse: Decodable {
    let profiles: [ExecutiveProfileRow]
    let combined: ExecutiveCombined
}
