// Thin wrapper around react-plotly.js, bound to the lightweight
// plotly.js-dist-min bundle (via react-plotly.js/factory) rather than the
// full plotly.js package. Every chart in this app is a ready-to-render
// Plotly figure `{data, layout}` JSON object returned by the backend — we
// never build chart traces ourselves.

import createPlotlyComponent from 'react-plotly.js/factory'
import Plotly from 'plotly.js-dist-min'
import type { PlotlyFigureJSON } from '../types'

const Plot = createPlotlyComponent(Plotly)

interface ChartFigureProps {
  figure: PlotlyFigureJSON
  height?: number
}

export default function ChartFigure({ figure, height = 380 }: ChartFigureProps) {
  return (
    <div style={{ width: '100%', height }}>
      <Plot
        data={figure.data}
        layout={figure.layout}
        useResizeHandler
        style={{ width: '100%', height: '100%' }}
        config={{ responsive: true, displaylogo: false }}
      />
    </div>
  )
}
