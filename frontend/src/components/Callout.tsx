// Port of Streamlit's st.info/st.error/st.success/st.warning/st.caption —
// small inline message boxes with consistent styling.

import type { ReactNode } from 'react'

type Variant = 'info' | 'error' | 'success' | 'warning' | 'caption'

export default function Callout({ variant, children }: { variant: Variant; children: ReactNode }) {
  if (variant === 'caption') {
    return <p className="caption">{children}</p>
  }
  return <div className={`callout callout-${variant}`}>{children}</div>
}
