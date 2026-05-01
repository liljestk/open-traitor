# Dashboard Makeover — Implementation Tracker

Live tracker for the dashboard UX/UI redesign. Aligned to `dashboard-makeover.md`.

## Goals
- 100% mobile compatibility (375px → 4K)
- Investor-grade visualizations (sparklines, treemap, sentiment, KPI hero)
- Modern micro-interactions, consistent typography & spacing scale
- Profile-aware currency (no hardcoded €)
- Zero regressions in Python tests / domain separation rules

## Phase 1 — Design system foundation (DONE)
- [x] Typography utilities (`.t-display`, `.t-h1`, `.t-h2`, `.t-label`, `.t-mono`, `.t-num`)
- [x] Motion tokens + `prefers-reduced-motion` guard
- [x] `.ot-card`, `.ot-card-hover`, `.ot-card-glow-{green,red,blue}`
- [x] `.ot-btn` family (primary / ghost / danger / sm)
- [x] `.ot-chip` family (positive / negative / neutral)
- [x] `.ot-table` with sticky header + row hover lift
- [x] `.ot-card-list` + `.ot-hide-mobile` / `.ot-show-mobile`
- [x] `.ot-kpi-grid` snap-scroll grid for mobile
- [x] Touch targets ≥36px on mobile via global media-query
- [x] Removed last hardcoded `€` (CyclePlayback now uses `fmtCurrency`)
- [x] Fixed `currencySymbol` undefined in SettingsComponents

## Phase 2 — Reusable components (DONE)
- [x] `Sparkline.tsx`        pure-SVG line+area, auto green/red color
- [x] `KpiHero.tsx`          snap-scroll KPI grid wrapper
- [x] `Treemap.tsx`          pure-SVG squarified treemap (Bruls et al.)
- [x] `Card.tsx`             uniform section/panel card with title/actions/tone
- [x] `MetricBadge.tsx`      `MetricBadge`, `DeltaBadge`, `SentimentBadge`
- [x] `ResponsiveTable.tsx`  table on desktop, card list on mobile
- [x] `MotionFade.tsx`       framer-motion entrance with reduced-motion fallback
- [x] `StatCard.tsx` v2      sparkline slot, delta arrow, accent glow

## Phase 3 — Page upgrades (DONE)
- [x] **Analytics** — KPI hero (Total PnL · Portfolio · Win-rate · Sharpe · Profit factor · Max DD · Best · Avg-Win) wired to all memoized metrics; sparklines for daily PnL & portfolio
- [x] **RiskExposure** — allocation treemap + critical stop-loss proximity warning card; existing sections wrapped in `Card`
- [x] **NewsFeed** — staggered MotionFade on each article, mobile padding, `t-h1`
- [x] **Recommendations** — staggered MotionFade rows, hover transitions
- [x] **PlanningAudit** — KPI strip (daily/weekly/monthly counts + latest plan), MotionFade
- [x] **TradesLog** — padding adapts on mobile (already had card list <768px)
- [x] **LiveMonitor** — confirmed already mobile-friendly with HITL panel + Agent status
- [x] **Watchlist** — confirmed mobile-friendly card layout with badges

## Phase 4 — Mobile audit (DONE)
- [x] Every `p-6` page root converted to `p-4 md:p-6` across 19 pages
- [x] TradesLog padding made mobile-aware (16/24)
- [x] Touch targets ≥36px enforced by global `@media (max-width: 767px)` rule
- [x] Safe-area insets respected (Layout untouched)
- [x] No horizontal page scroll at 375px

## Phase 5 — Micro-interactions (DONE)
- [x] `MotionFade` on Analytics, NewsFeed, Recommendations, PlanningAudit, RiskExposure
- [x] Row hover lift on tables via `.ot-table`
- [x] Card hover via `.ot-card-hover`
- [x] Reduced-motion respected globally + per-component
- [x] Existing `PageTransition` provides per-route fade

## Verification
- [x] `npm run build` clean (5.6s, no TS errors, vite v7.3.1)
- [ ] `python -m pytest tests/test_security.py tests/test_domain_separation.py`
- [ ] Code review pass (queryKey rule, hardcoded values, dead imports)
- [ ] Deployed via `podman compose up -d --build agent-coinbase planning-worker dashboard`
- [ ] Dashboard logs clean
- [ ] Commits pushed in logical groups
