/**
 * CandlestickChart — TradingView Lightweight Charts wrapper.
 * Renders OHLCV candle data with optional trade markers.
 */
import { useEffect, useRef } from 'react'
import { createChart, CandlestickSeries, createSeriesMarkers, type IChartApi, type ISeriesApi, type ISeriesMarkersPluginApi, ColorType } from 'lightweight-charts'

interface Candle {
  time: string | number
  open: number
  high: number
  low: number
  close: number
}

interface Marker {
  time: string | number
  position: 'aboveBar' | 'belowBar'
  color: string
  shape: 'arrowDown' | 'arrowUp' | 'circle'
  text: string
}

interface Props {
  candles: Candle[]
  markers?: Marker[]
  height?: number
  pair?: string
}

export default function CandlestickChart({ candles, markers, height = 400, pair }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const markersRef = useRef<ISeriesMarkersPluginApi<import('lightweight-charts').Time> | null>(null)

  useEffect(() => {
    if (!containerRef.current) return

    const chart = createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: '#8b949e',
        fontSize: 11,
      },
      grid: {
        vertLines: { color: '#21262d' },
        horzLines: { color: '#21262d' },
      },
      crosshair: {
        mode: 0,
        vertLine: { color: '#30363d', width: 1, style: 2, labelBackgroundColor: '#161b22' },
        horzLine: { color: '#30363d', width: 1, style: 2, labelBackgroundColor: '#161b22' },
      },
      rightPriceScale: {
        borderColor: '#21262d',
      },
      timeScale: {
        borderColor: '#21262d',
        timeVisible: true,
        secondsVisible: false,
      },
    })

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#22c55e',
      downColor: '#ef4444',
      borderUpColor: '#22c55e',
      borderDownColor: '#ef4444',
      wickUpColor: '#22c55e80',
      wickDownColor: '#ef444480',
    })

    chartRef.current = chart
    seriesRef.current = candleSeries
    markersRef.current = createSeriesMarkers(candleSeries)

    // Handle resize
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0]
      if (entry) {
        chart.applyOptions({ width: entry.contentRect.width })
      }
    })
    observer.observe(containerRef.current)

    return () => {
      observer.disconnect()
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
    }
  }, [height])

  // Update data
  useEffect(() => {
    if (!seriesRef.current || !candles.length) return

    // Sort by time ascending
    const sorted = [...candles].sort((a, b) => {
      const ta = typeof a.time === 'string' ? new Date(a.time).getTime() / 1000 : a.time
      const tb = typeof b.time === 'string' ? new Date(b.time).getTime() / 1000 : b.time
      return ta - tb
    })

    const data = sorted.map((c) => ({
      time: (typeof c.time === 'string' ? Math.floor(new Date(c.time).getTime() / 1000) : c.time) as import('lightweight-charts').UTCTimestamp,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }))

    seriesRef.current.setData(data)

    if (markers?.length && markersRef.current) {
      const sortedMarkers = [...markers]
        .map((m) => ({
          ...m,
          time: (typeof m.time === 'string' ? Math.floor(new Date(m.time).getTime() / 1000) : m.time) as import('lightweight-charts').UTCTimestamp,
        }))
        .sort((a, b) => (a.time as number) - (b.time as number))
      markersRef.current.setMarkers(sortedMarkers)
    }

    chartRef.current?.timeScale().fitContent()
  }, [candles, markers])

  return (
    <div className="relative rounded-xl border border-gray-800 overflow-hidden" style={{ background: '#0a0e14' }}>
      {pair && (
        <div className="absolute top-3 left-4 z-10 text-xs font-mono text-gray-500">
          {pair}
        </div>
      )}
      <div ref={containerRef} />
    </div>
  )
}
