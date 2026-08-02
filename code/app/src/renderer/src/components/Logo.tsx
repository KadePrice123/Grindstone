/**
 * The Grindstone mark (placeholder identity).
 *
 * The spin is applied to the <svg> HOST element, never to a <g> inside it.
 * Chromium cannot run a transform on an SVG child on the compositor thread —
 * it repaints the frame every tick. Measured on the real app, the inner-<g>
 * version cost 10.7% of a core forever on a static login screen (83% of that
 * in the GPU process) and made pointer movement feel laggy.
 */
export function Logo({ size = 64, spin = false }: { size?: number; spin?: boolean }) {
  return (
    <svg
      className={spin ? 'mark spin' : 'mark'}
      width={size}
      height={size}
      viewBox="0 0 64 64"
      xmlns="http://www.w3.org/2000/svg"
      aria-label="Grindstone"
    >
      <g>
        <path
          d="M32 10 L51.05 43 L12.95 43 Z"
          fill="none"
          stroke="currentColor"
          strokeWidth="4"
          strokeLinejoin="round"
        />
        <path className="facet" d="M32 41 L24.2 27.5 L39.8 27.5 Z" />
      </g>
    </svg>
  )
}
