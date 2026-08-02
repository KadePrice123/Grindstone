/**
 * Right-button forwarding for the gesture wheel, installed in the chrome and
 * content modes (browser tabs have their own minimal preload doing exactly
 * this; the auth screen deliberately has none — no wheel while locked).
 *
 * Capture phase, so no component can swallow the gesture; isTrusted, so no
 * synthetic event can fake one. The default context menu is suppressed —
 * the wheel IS this app's right-click behavior (FR-SHELL-4).
 */

export function installWheelEvents(): void {
  let rightDown = false

  const send = (kind: 'down' | 'move' | 'up', e: MouseEvent) =>
    window.grindstone.wheelEvt(kind, Math.round(e.clientX), Math.round(e.clientY))

  window.addEventListener(
    'mousedown',
    (e) => {
      if (e.button === 2 && e.isTrusted) {
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
