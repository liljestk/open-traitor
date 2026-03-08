import { useState, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import dayjs from 'dayjs'
import { Plus, X, Activity, History, ArrowRight } from 'lucide-react'
import {
    fetchSimulatedTrades,
    createSimulatedTrade,
    closeSimulatedTrade,
    fetchMarketPrice,
    fetchProducts,
} from '../api'
import type { CoinbaseProduct } from '../api'
import PageTransition from '../components/PageTransition'
import { useLiveStore } from '../store'

// Profile-aware fallback products
const CRYPTO_FALLBACK_PRODUCTS: CoinbaseProduct[] = [
    { id: 'BTC-EUR', base: 'BTC', quote: 'EUR' },
    { id: 'ETH-EUR', base: 'ETH', quote: 'EUR' },
    { id: 'SOL-EUR', base: 'SOL', quote: 'EUR' },
    { id: 'BTC-USD', base: 'BTC', quote: 'USD' },
    { id: 'ETH-USD', base: 'ETH', quote: 'USD' },
    { id: 'SOL-USD', base: 'SOL', quote: 'USD' },
    { id: 'ETH-BTC', base: 'ETH', quote: 'BTC' },
    { id: 'SOL-BTC', base: 'SOL', quote: 'BTC' },
]

const EQUITY_FALLBACK_PRODUCTS: CoinbaseProduct[] = [
    { id: 'AAPL-USD', base: 'AAPL', quote: 'USD' },
    { id: 'MSFT-USD', base: 'MSFT', quote: 'USD' },
    { id: 'GOOGL-USD', base: 'GOOGL', quote: 'USD' },
    { id: 'AMZN-USD', base: 'AMZN', quote: 'USD' },
    { id: 'NVDA-USD', base: 'NVDA', quote: 'USD' },
    { id: 'TSLA-USD', base: 'TSLA', quote: 'USD' },
]

const EQUITY_PROFILES = new Set(['ibkr'])

function pnlColor(pnl: number): string {
    if (pnl > 0) return 'text-green-400'
    if (pnl < 0) return 'text-red-400'
    return 'text-gray-400'
}

function pnlBg(pnl: number): string {
    if (pnl > 0) return 'bg-green-400/10 border-green-400/30'
    if (pnl < 0) return 'bg-red-400/10 border-red-400/30'
    return 'bg-gray-800 border-gray-700'
}

export default function SimulatedTrades() {
    const qc = useQueryClient()
    const profile = useLiveStore((s) => s.profile)
    const isEquity = EQUITY_PROFILES.has(profile.toLowerCase())
    const [buyAsset, setBuyAsset] = useState(isEquity ? 'AAPL' : 'BTC')
    const [quoteCurrency, setQuoteCurrency] = useState(isEquity ? 'USD' : 'EUR')
    const [amount, setAmount] = useState('1000')
    const [notes, setNotes] = useState('')
    const [showClosed, setShowClosed] = useState(false)

    // Fetch tradable products (profile-aware via apiFetch)
    const { data: productsData } = useQuery({
        queryKey: ['products', profile],
        queryFn: fetchProducts,
        staleTime: 5 * 60_000, // cache for 5 min
    })

    const products = productsData?.products?.length
        ? productsData.products
        : isEquity ? EQUITY_FALLBACK_PRODUCTS : CRYPTO_FALLBACK_PRODUCTS

    // Derive available assets and quote currencies from product list
    const { baseAssets, quotesForBase, pairId } = useMemo(() => {
        // All unique base assets, sorted alphabetically
        const allBases = [...new Set(products.map(p => p.base))].sort()

        // Quote currencies available for the selected buy asset
        const quotesForSelected = products
            .filter(p => p.base === buyAsset)
            .map(p => p.quote)
        const uniqueQuotes = [...new Set(quotesForSelected)].sort((a, b) => {
            // Prioritise common fiats first
            const priority = ['EUR', 'USD', 'USDT', 'USDC', 'GBP', 'BTC', 'ETH']
            const ai = priority.indexOf(a), bi = priority.indexOf(b)
            if (ai !== -1 && bi !== -1) return ai - bi
            if (ai !== -1) return -1
            if (bi !== -1) return 1
            return a.localeCompare(b)
        })

        // The resolved pair id
        const resolvedPair = `${buyAsset}-${quoteCurrency}`
        const validPair = products.some(p => p.id === resolvedPair) ? resolvedPair : ''

        return { baseAssets: allBases, quotesForBase: uniqueQuotes, pairId: validPair }
    }, [products, buyAsset, quoteCurrency])

    // Auto-correct quote currency when switching buy asset
    const handleBuyAssetChange = (newBase: string) => {
        setBuyAsset(newBase)
        const quotesForNew = products.filter(p => p.base === newBase).map(p => p.quote)
        if (!quotesForNew.includes(quoteCurrency)) {
            // Pick best available quote
            const preferred = ['EUR', 'USD', 'USDT', 'USDC', 'GBP', 'BTC']
            const best = preferred.find(q => quotesForNew.includes(q)) ?? quotesForNew[0] ?? ''
            setQuoteCurrency(best)
        }
    }

    const pair = pairId
    const fromCurrency = quoteCurrency

    // Auto-refreshing query for all simulations
    const { data, isLoading } = useQuery({
        queryKey: ['simulations', showClosed, profile],
        queryFn: () => fetchSimulatedTrades(showClosed),
        refetchInterval: 30_000, // refresh live PnL every 30s
    })

    // Live price preview for the form
    const { data: priceData } = useQuery({
        queryKey: ['market-price', pair, profile],
        queryFn: () => fetchMarketPrice(pair),
        staleTime: 10_000,
    })

    const createMut = useMutation({
        mutationFn: createSimulatedTrade,
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ['simulations', showClosed, profile] })
            setNotes('')
        },
    })

    const closeMut = useMutation({
        mutationFn: closeSimulatedTrade,
        onSuccess: () => qc.invalidateQueries({ queryKey: ['simulations', showClosed, profile] }),
    })

    const sims = data?.simulations ?? []
    const openSims = sims.filter(s => s.status === 'open')
    const closedSims = sims.filter(s => s.status === 'closed')

    const handleCreate = (e: React.FormEvent) => {
        e.preventDefault()
        if (!amount || isNaN(Number(amount))) return
        createMut.mutate({
            pair,
            from_currency: fromCurrency,
            from_amount: Number(amount),
            notes,
        })
    }

    return (
        <PageTransition>
        <div className="p-6 space-y-8 max-w-6xl mx-auto">
            <div className="flex items-center justify-between">
                <h2 className="text-2xl font-bold text-gray-100 flex items-center gap-2">
                    <Activity className="text-brand-400" />
                    Simulated Trades
                </h2>
                <p className="text-sm text-gray-400">Paper trade against live {profile === 'ibkr' ? 'IBKR' : 'Coinbase'} prices</p>
            </div>

            {/* New Simulation Form */}
            <div className="bg-gray-900 border border-gray-800 rounded-xl p-5 shadow-sm">
                <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wider mb-4 border-b border-gray-800 pb-2">
                    Open New Simulation
                </h3>
                <form onSubmit={handleCreate} className="grid grid-cols-1 md:grid-cols-6 gap-4 items-end">
                    {/* Buy Asset */}
                    <div className="md:col-span-2 space-y-1.5">
                        <label className="text-xs text-gray-500 font-medium">Buy Asset</label>
                        <select
                            className="w-full bg-gray-950 border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-200 outline-none focus:border-brand-500 transition-colors"
                            value={buyAsset}
                            onChange={(e) => handleBuyAssetChange(e.target.value)}
                        >
                            {baseAssets.map(b => (
                                <option key={b} value={b}>{b}</option>
                            ))}
                        </select>
                    </div>

                    {/* Pay With (quote currency) */}
                    <div className="space-y-1.5">
                        <label className="text-xs text-gray-500 font-medium flex items-center gap-1">
                            <ArrowRight size={12} className="text-gray-600" />
                            Pay With
                        </label>
                        <select
                            className="w-full bg-gray-950 border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-200 outline-none focus:border-brand-500 transition-colors"
                            value={quoteCurrency}
                            onChange={(e) => setQuoteCurrency(e.target.value)}
                        >
                            {quotesForBase.map(q => (
                                <option key={q} value={q}>{q}</option>
                            ))}
                        </select>
                    </div>

                    {/* Spend Amount */}
                    <div className="space-y-1.5">
                        <label className="text-xs text-gray-500 font-medium">
                            Amount ({fromCurrency})
                        </label>
                        <input
                            type="number"
                            step="any"
                            className="w-full bg-gray-950 border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-200 outline-none focus:border-brand-500 transition-colors"
                            value={amount}
                            onChange={(e) => setAmount(e.target.value)}
                            placeholder="1000"
                            required
                        />
                    </div>

                    {/* Notes + Submit */}
                    <div className="md:col-span-2 space-y-1.5">
                        <label className="text-xs text-gray-500 font-medium flex justify-between">
                            <span>Notes{pair && <span className="ml-2 text-gray-600">({pair})</span>}</span>
                            {priceData && (
                                <span className="text-brand-400">Live: {priceData.price.toFixed(6)}</span>
                            )}
                        </label>
                        <div className="flex gap-2">
                            <input
                                type="text"
                                className="flex-1 bg-gray-950 border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-200 outline-none focus:border-brand-500 transition-colors"
                                value={notes}
                                onChange={(e) => setNotes(e.target.value)}
                                placeholder="Optional hypothesis..."
                            />
                            <button
                                type="submit"
                                disabled={createMut.isPending || !amount || !pair}
                                className="bg-brand-600 hover:bg-brand-500 text-white font-medium rounded-lg px-4 py-2 text-sm flex items-center justify-center transition-colors disabled:opacity-50"
                            >
                                {createMut.isPending ? '...' : <><Plus size={16} className="mr-1" /> Open</>}
                            </button>
                        </div>
                    </div>
                </form>
            </div>

            {/* Active Simulations */}
            <div className="space-y-4">
                <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wider border-b border-gray-800 pb-2">
                    Open Positions ({openSims.length})
                </h3>

                {isLoading && <div className="text-sm text-gray-500">Loading simulations...</div>}
                {!isLoading && openSims.length === 0 && (
                    <div className="text-sm text-gray-500 bg-gray-900 border border-gray-800 rounded-xl p-8 text-center">
                        No active simulated trades. Open one above!
                    </div>
                )}

                <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                    {openSims.map(sim => (
                        <div key={sim.id} className={`p-4 rounded-xl border relative overflow-hidden transition-colors ${pnlBg(sim.pnl_abs)}`}>
                            <div className="flex justify-between items-start mb-3">
                                <div>
                                    <div className="flex items-center gap-2">
                                        <span className="font-bold text-lg text-gray-100">{sim.pair}</span>
                                        <span className="text-xs bg-gray-950/50 px-2 py-0.5 rounded text-gray-400">#{sim.id}</span>
                                    </div>
                                    <div className="text-xs text-gray-400 mt-1">
                                        Opened {dayjs(sim.ts).format('MMM D, HH:mm')}
                                    </div>
                                </div>
                                <button
                                    onClick={() => {
                                        if (confirm(`Close simulation #${sim.id}?`)) closeMut.mutate(sim.id)
                                    }}
                                    disabled={closeMut.isPending}
                                    className="text-gray-500 hover:text-red-400 hover:bg-red-400/10 p-1.5 rounded transition-colors disabled:opacity-50"
                                    title="Close Position"
                                >
                                    <X size={16} />
                                </button>
                            </div>

                            <div className="grid grid-cols-2 gap-4 mb-3">
                                <div>
                                    <div className="text-xs text-gray-500 mb-0.5">Size</div>
                                    <div className="text-sm font-medium text-gray-200">
                                        {sim.quantity.toFixed(6)} {sim.to_currency}{' '}
                                        <span className="text-gray-500 font-normal">({sim.from_amount} {sim.from_currency})</span>
                                    </div>
                                </div>
                                <div>
                                    <div className="text-xs text-gray-500 mb-0.5">Live PnL</div>
                                    <div className={`text-sm font-bold ${pnlColor(sim.pnl_abs)}`}>
                                        {sim.pnl_abs > 0 ? '+' : ''}{sim.pnl_abs.toFixed(2)} {sim.from_currency}
                                        <span className="ml-1.5 inline-block opacity-80">
                                            ({sim.pnl_pct > 0 ? '+' : ''}{sim.pnl_pct.toFixed(2)}%)
                                        </span>
                                    </div>
                                </div>
                                <div>
                                    <div className="text-xs text-gray-500 mb-0.5">Entry Price</div>
                                    <div className="text-sm text-gray-300">{sim.entry_price.toFixed(6)}</div>
                                </div>
                                <div>
                                    <div className="text-xs text-gray-500 mb-0.5">Current Price</div>
                                    <div className="text-sm text-gray-300">{sim.current_price.toFixed(6)}</div>
                                </div>
                            </div>

                            {sim.notes && (
                                <div className="text-xs text-gray-400 bg-gray-950/30 p-2 rounded italic mt-2">
                                    <span className="font-semibold text-gray-500 not-italic mr-2">Note:</span>
                                    {sim.notes}
                                </div>
                            )}
                        </div>
                    ))}
                </div>
            </div>

            {/* Closed Simulations */}
            <div className="border border-gray-800 rounded-xl overflow-hidden bg-gray-900 shadow-sm mt-8">
                <button
                    onClick={() => setShowClosed(!showClosed)}
                    className="w-full flex items-center justify-between px-5 py-4 text-left hover:bg-gray-800 transition-colors"
                >
                    <div className="flex items-center gap-2">
                        <History className="text-gray-400" size={18} />
                        <h3 className="font-semibold text-gray-300">Historical / Closed</h3>
                    </div>
                    <span className="text-xs text-gray-500 bg-gray-950 px-2 py-1 rounded">
                        {showClosed ? 'Hide' : 'Show'}
                    </span>
                </button>

                {showClosed && (
                    <div className="p-0 border-t border-gray-800">
                        {closedSims.length === 0 ? (
                            <div className="p-6 text-center text-sm text-gray-500">No closed simulations found.</div>
                        ) : (
                            <table className="w-full text-sm">
                                <thead>
                                    <tr className="bg-gray-950/50 text-gray-500 text-xs text-left uppercase border-b border-gray-800">
                                        <th className="px-5 py-3 font-medium">Pair / Time</th>
                                        <th className="px-5 py-3 font-medium">Asset</th>
                                        <th className="px-5 py-3 font-medium">Prices</th>
                                        <th className="px-5 py-3 font-medium text-right">Final PnL</th>
                                    </tr>
                                </thead>
                                <tbody className="divide-y divide-gray-800/50">
                                    {closedSims.map(sim => (
                                        <tr key={sim.id} className="hover:bg-gray-800/30 transition-colors">
                                            <td className="px-5 py-3">
                                                <div className="font-bold text-gray-200">{sim.pair}</div>
                                                <div className="text-xs text-gray-500 mt-0.5">
                                                    {dayjs(sim.closed_at).format('MMM D, HH:mm')}
                                                </div>
                                            </td>
                                            <td className="px-5 py-3">
                                                <div className="text-gray-300">
                                                    {sim.quantity.toFixed(6)} {sim.to_currency}
                                                </div>
                                                <div className="text-xs text-gray-500 mt-0.5">
                                                    Cost: {sim.from_amount} {sim.from_currency}
                                                </div>
                                            </td>
                                            <td className="px-5 py-3">
                                                <div className="text-gray-300">
                                                    Out: {sim.close_price?.toFixed(6) || '?'}
                                                </div>
                                                <div className="text-xs text-gray-500 mt-0.5">
                                                    In: {sim.entry_price.toFixed(6)}
                                                </div>
                                            </td>
                                            <td className={`px-5 py-3 text-right font-bold tracking-wide ${pnlColor(sim.close_pnl_abs ?? 0)}`}>
                                                {(sim.close_pnl_abs ?? 0) > 0 ? '+' : ''}{(sim.close_pnl_abs ?? 0).toFixed(2)} {sim.from_currency}
                                                <div className="text-xs opacity-80 mt-0.5 font-normal">
                                                    {(sim.close_pnl_pct ?? 0) > 0 ? '+' : ''}{(sim.close_pnl_pct ?? 0).toFixed(2)}%
                                                </div>
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        )}
                    </div>
                )}
            </div>
        </div>
        </PageTransition>
    )
}
