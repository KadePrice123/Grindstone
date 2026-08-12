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

export const ChartMiniIcon = () => (
  <Icon label="Symbol">
    <path d="M8 3.5 V6" />
    <rect x="6" y="6" width="4" height="8" rx="1" />
    <path d="M8 14 V17" />
    <path d="M16 7 V9.5" />
    <rect x="14" y="9.5" width="4" height="8" rx="1" />
    <path d="M16 17.5 V20.5" />
  </Icon>
)

export const NewsMiniIcon = () => (
  <Icon label="News">
    <rect x="3.5" y="5" width="17" height="14" rx="2" />
    <rect x="7" y="8.5" width="4.5" height="4.5" rx="0.75" />
    <path d="M14.5 9.5 H17" />
    <path d="M14.5 12.5 H17" />
    <path d="M7 16 H17" />
  </Icon>
)

export const PageMiniIcon = () => (
  <Icon label="Page">
    <rect x="5" y="3.5" width="14" height="17" rx="2" />
    <path d="M8.5 8 H15.5" />
    <path d="M8.5 12 H15.5" />
    <path d="M8.5 16 H13" />
  </Icon>
)

export const SettingsIcon = () => (
  <Icon label="Settings">
    <path d="M4 6.5 H6.6" />
    <circle cx="9" cy="6.5" r="2.2" />
    <path d="M11.4 6.5 H20" />
    <path d="M4 12 H12.6" />
    <circle cx="15" cy="12" r="2.2" />
    <path d="M17.4 12 H20" />
    <path d="M9.4 17.5 H20" />
    <circle cx="7" cy="17.5" r="2.2" />
    <path d="M4 17.5 H4.6" />
  </Icon>
)

export const SearchMiniIcon = () => (
  <Icon label="Search">
    <circle cx="10.5" cy="10.5" r="6" />
    <path d="M15 15 L20.5 20.5" />
  </Icon>
)

export const BrowserMiniIcon = () => (
  <Icon label="Web page">
    <circle cx="12" cy="12" r="8.5" />
    <path d="M3.5 12 H20.5" />
    <path d="M12 3.5 C15.5 7 15.5 17 12 20.5 C8.5 17 8.5 7 12 3.5 Z" />
  </Icon>
)

export const DataIcon = () => (
  <Icon label="Data">
    <ellipse cx="12" cy="5.5" rx="7" ry="2.8" />
    <path d="M5 5.5 V18.5 C5 20 8.1 21.3 12 21.3 C15.9 21.3 19 20 19 18.5 V5.5" />
    <path d="M5 12 C5 13.5 8.1 14.8 12 14.8 C15.9 14.8 19 13.5 19 12" />
  </Icon>
)

export const BacktestIcon = () => (
  <Icon label="Backtest">
    {/* replay arrow around candles: running history again */}
    <path d="M4.5 12 A7.5 7.5 0 1 1 8 18.2" />
    <path d="M4.5 8.5 V12 H8" />
    <path d="M10 13.5 V9.5" />
    <rect x="9" y="9.5" width="2" height="3" rx="0.5" />
    <path d="M14 14.5 V8" />
    <rect x="13" y="9" width="2" height="4" rx="0.5" />
  </Icon>
)

export const HomeIcon = () => (
  <Icon label="Home">
    {/* the Grindstone triangle, same geometry as the Logo mark */}
    <path d="M12 4.5 L19.5 17.5 H4.5 Z" />
    <path d="M12 15 L9.2 10.2 H14.8 Z" />
  </Icon>
)

export const AppsIcon = () => (
  <Icon label="Apps">
    {/* 3×3 launcher grid; stroke-drawn dots keep the set's line weight */}
    {[5.5, 12, 18.5].map((y) =>
      [5.5, 12, 18.5].map((x) => <circle key={`${x},${y}`} cx={x} cy={y} r="1.4" />)
    )}
  </Icon>
)

export const InsureIcon = () => (
  <Icon label="Insure">
    {/* a shield — selling puts is writing insurance, and the glyph says so */}
    <path d="M12 3.5 L19 6.2 V11.5 C19 16 16.2 19.3 12 21 C7.8 19.3 5 16 5 11.5 V6.2 Z" />
    <path d="M9 12 L11.2 14.2 L15.2 9.5" />
  </Icon>
)

/** The page registry's keys (GET /api/pages, urls.ts PAGES) -> glyphs, shared
 *  by the favorite tiles and the apps launcher. Look up via pageIcon() so
 *  unmapped or future pages fall back to the generic page glyph. */
export const PAGE_ICONS: Record<string, () => React.JSX.Element> = {
  home: HomeIcon,
  accounts: AccountsIcon,
  apis: ApisIcon,
  ai: AiIcon,
  positions: PositionsIcon,
  data: DataIcon,
  backtest: BacktestIcon,
  news: NewsMiniIcon,
  charts: ChartMiniIcon,
  settings: SettingsIcon,
  search: SearchMiniIcon,
  article: PageMiniIcon,
  help: PageMiniIcon,
  insure: InsureIcon,
}

export function pageIcon(key: string): () => React.JSX.Element {
  return PAGE_ICONS[key] ?? PageMiniIcon
}
