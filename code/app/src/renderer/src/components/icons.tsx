/** Page icons, inlined from assets/branding/icons (same geometry). */
import type { ReactNode } from 'react'

function Icon({ children, label }: { children: ReactNode; label: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width="24"
      height="24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-label={label}
    >
      {children}
    </svg>
  )
}

export const AccountsIcon = () => (
  <Icon label="Accounts">
    <path d="M4 9 L12 3.5 L20 9 Z" />
    <path d="M6.5 12 V17" />
    <path d="M12 12 V17" />
    <path d="M17.5 12 V17" />
    <path d="M4.5 20 H19.5" />
  </Icon>
)

export const ApisIcon = () => (
  <Icon label="APIs">
    <circle cx="8" cy="12" r="3.5" />
    <path d="M11.5 12 H21" />
    <path d="M18 12 V15.5" />
    <path d="M21 12 V14.5" />
  </Icon>
)

export const AiIcon = () => (
  <Icon label="AI">
    <path d="M12 3.5 C12.8 8 13.5 9.5 20.5 12 C13.5 14.5 12.8 16 12 20.5 C11.2 16 10.5 14.5 3.5 12 C10.5 9.5 11.2 8 12 3.5 Z" />
  </Icon>
)

export const PositionsIcon = () => (
  <Icon label="Positions">
    <path d="M12 4 L20 8.5 L12 13 L4 8.5 Z" />
    <path d="M4 13 L12 17.5 L20 13" />
    <path d="M4 17 L12 21.5 L20 17" />
  </Icon>
)
