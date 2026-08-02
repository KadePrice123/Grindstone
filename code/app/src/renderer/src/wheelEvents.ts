/**
 * Right-button forwarding for the gesture wheel, installed in the chrome and
 * content modes (browser tabs have their own minimal preload doing exactly
 * this; the auth screen deliberately has none — no wheel while locked).
 *
 * Capture phase, so no component can swallow the gesture; isTrusted, so no
 * synthetic event can fake one. The default context menu is suppressed —
 * the wheel IS this app's right-click behavior (FR-SHELL-4).
 */

/** What sits under a right-click: charts declare themselves (and their live
 *  state) via data attributes on the container the Chart component renders,
 *  so the wheel that spawns matches what was clicked — chart wheel over ANY
 *  chart, the default wheel everywhere else. */
function contextAt(target: EventTarget | null): {
  context: string
  symbols?: string[]
  indicators?: string[]
  hidden?: string[]
} | undefined {
  if (!(target instanceof Element)) return undefined
  const host = target.closest<HTMLElement>('[data-wheel-context]')
  if (!host) return undefined
  const list = (v: string | undefined) =>
    v ? v.split(',').map((s) => s.trim()).filter(Boolean) : []
  return {
    context: host.dataset.wheelContext ?? '',
    symbols: list(host.dataset.chartSymbols),
    indicators: list(host.dataset.chartIndicators),
    hidden: list(host.dataset.chartHidden),
  }
}

export function installWheelEvents(): void {
  let rightDown = false

  const send = (kind: 'down' | 'move' | 'up', e: MouseEvent) =>
    window.grindstone.wheelEvt(
      kind,
      Math.round(e.clientX),
      Math.round(e.clientY),
      kind === 'down' ? contextAt(e.target) : undefined
    )

  window.addEventListener(
    'mousedown',
    (e) => {
      if (e.button === 2 && e.isTrusted) {
        // A TAB owns its own right-click: the strip shows the native tab
        // menu (split view etc.), not the wheel.
        if (e.target instanceof Element && e.target.closest('.strip-tab')) return
        rightDown = true
        send('down', e)
      }
    },
    true
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
  window.addEventListener('contextmenu', (e) => e.preventDefault(), true)
}
