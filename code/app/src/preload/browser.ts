/**
 * The ONLY preload in-app browser tabs get, and it is a one-way street.
 *
 * Purpose: gesture wheels must spawn on right-click ANYWHERE in the app,
 * including over third-party pages. This forwards raw right-button events to
 * main — nothing else.
 *
 * Hardening, in order of importance:
 *  - NO contextBridge. The page gains no API surface at all; this file runs
 *    in the isolated world and the page cannot see or call into it.
 *  - Only real input: `isTrusted` is checked, so a page dispatching
 *    synthetic MouseEvents cannot puppet the wheel.
 *  - Fixed channel, primitive payloads: a kind string and two clamped
 *    numbers. There is nothing here worth compromising.
 *
 * Known edge: events inside cross-origin iframes never reach the top frame,
 * so right-click over an embedded frame does not spawn the wheel. Acceptable
 * — the alternative (preloads in every subframe) widens the surface for a
 * corner case.
 */
import { ipcRenderer } from 'electron'

type Kind = 'down' | 'move' | 'up'

let rightDown = false

function send(kind: Kind, e: MouseEvent): void {
  // Coordinates may go NEGATIVE mid-hold: capture keeps routing events here
  // while the cursor is above/left of this view, and clamping them would bend
  // the drag angle the wheel selects by.
  ipcRenderer.send('wheel:evt', kind, Math.round(e.clientX), Math.round(e.clientY))
}

window.addEventListener(
  'mousedown',
  (e) => {
    if (e.button === 2 && e.isTrusted) {
      rightDown = true
      send('down', e)
    }
  },
  true // capture: a page calling stopPropagation cannot starve the wheel
)
window.addEventListener(
  'mousemove',
  (e) => {
    if (rightDown && e.isTrusted) send('move', e)
  },
  true
)
window.addEventListener(
  'mouseup',
  (e) => {
    if (e.button === 2 && rightDown && e.isTrusted) {
      rightDown = false
      send('up', e)
    }
  },
  true
)
