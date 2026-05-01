/**
 * MotionFade — framer-motion entrance wrapper that respects reduced-motion.
 * Use to fade-in cards/sections on mount.
 */
import { motion, useReducedMotion } from 'framer-motion'
import type { ReactNode } from 'react'

interface Props {
  children: ReactNode
  delay?: number
  /** Pixels to translate up from. */
  y?: number
  className?: string
}

export default function MotionFade({ children, delay = 0, y = 8, className }: Props) {
  const reduce = useReducedMotion()
  if (reduce) {
    return <div className={className}>{children}</div>
  }
  return (
    <motion.div
      className={className}
      initial={{ opacity: 0, y }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.32, ease: [0.22, 1, 0.36, 1], delay }}
    >
      {children}
    </motion.div>
  )
}
