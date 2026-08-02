/**
 * Omnibox address detection — the browser-bar rule: if it looks like a place,
 * go there; otherwise search.
 *
 * Deliberately conservative about what counts as a domain, because tickers
 * and company names must never be mistaken for addresses. "google.com" is an
 * address; "google" is a search; "AAPL" is a ticker.
 */

// Common TLDs plus anything that looks like a country code. Not exhaustive by
// design — an unknown TLD falls through to search, which is recoverable,
// whereas a wrong navigation is not.
const TLD = /\.(com|net|org|io|co|ai|dev|app|gov|edu|news|finance|xyz|me|us|uk|ca|de|fr|jp|au|info|biz|tv|cloud|so|sh|to|ly|[a-z]{2})$/i

export function asUrl(input: string): string | null {
  const text = input.trim()
  if (!text || /\s/.test(text)) return null // spaces mean it is a query

  if (/^https?:\/\//i.test(text)) {
    try {
      const u = new URL(text)
      return u.hostname ? u.toString() : null
    } catch {
      return null
    }
  }
  if (/^(file|javascript|data|about|chrome):/i.test(text)) return null // never navigate to these

  // localhost[:port][/path] is an address.
  if (/^localhost(:\d+)?(\/|$)/i.test(text)) return `http://${text}`

  const host = text.split(/[/?#]/)[0]
  if (!host.includes('.') || host.startsWith('.') || host.endsWith('.')) return null
  if (!TLD.test(host)) return null
  // A bare number-dotted string is not a host we want (e.g. "1.5").
  if (/^[\d.]+$/.test(host) && !/^(\d{1,3}\.){3}\d{1,3}$/.test(host)) return null

  return `https://${text}`
}
