/**
 * PageTransition — framer-motion wrapper for smooth page entrance animations.
 */
import { motion } from 'framer-motion'
import type { ReactNode } from 'react'

export default function PageTransition({ children }: { children: ReactNode }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.22, ease: 'easeOut' }}
      style={{ height: '100%' }}
    >
      {children}
    </motion.div>
  )
}
