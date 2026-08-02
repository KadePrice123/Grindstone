/** The Grindstone mark (placeholder identity) — inline so it themes via CSS. */
export function Logo({ size = 64, spin = true }: { size?: number; spin?: boolean }) {
  return (
    <svg
      className="mark"
      width={size}
      height={size}
      viewBox="0 0 64 64"
      xmlns="http://www.w3.org/2000/svg"
      aria-label="Grindstone"
    >
      <g className={spin ? 'spin' : undefined}>
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
