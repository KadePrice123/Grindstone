/**
 * mode=divider — the slim draggable bar between the two panes of a split.
 * It is its own WebContentsView, so pointer capture keeps the drag alive
 * even as the cursor crosses the neighbouring panes' views. It reports raw
 * screenX: this view is only 8px wide, so clientX carries no information —
 * main converts screenX to a ratio against the window's content bounds.
 */
import { useRef } from 'react'
import '../split.css'

export function SplitDivider() {
  const dragging = useRef(false)
  return (
    <div
      className="split-divider"
      onPointerDown={(e) => {
        if (e.button !== 0) return
        dragging.current = true
        e.currentTarget.setPointerCapture(e.pointerId)
      }}
      onPointerMove={(e) => {
        if (dragging.current) window.grindstoneSplit.drag(Math.round(e.screenX))
      }}
      onPointerUp={() => {
        dragging.current = false
      }}
      onPointerCancel={() => {
        dragging.current = false
      }}
    >
      <div className="split-divider-grip" />
    </div>
  )
}
