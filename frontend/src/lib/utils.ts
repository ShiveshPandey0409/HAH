export function money(minor: number, currency = 'USD') {
  try {
    return new Intl.NumberFormat('en', { style: 'currency', currency }).format(minor / 100)
  } catch {
    return `${currency} ${(minor / 100).toFixed(2)}`
  }
}

export function date(value: string | null | undefined, fallback = 'No deadline') {
  if (!value) return fallback
  return new Intl.DateTimeFormat('en', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value))
}

export function relativeDate(value: string) {
  const delta = new Date(value).getTime() - Date.now()
  const days = Math.round(delta / 86_400_000)
  if (Math.abs(days) >= 1) return new Intl.RelativeTimeFormat('en', { numeric: 'auto' }).format(days, 'day')
  const hours = Math.round(delta / 3_600_000)
  return new Intl.RelativeTimeFormat('en', { numeric: 'auto' }).format(hours, 'hour')
}

export function titleCase(value: string) {
  return value.replace(/_/g, ' ').replace(/\b\w/g, (letter: string) => letter.toUpperCase())
}

export function toIso(localValue: string): string | null {
  return localValue ? new Date(localValue).toISOString() : null
}

export function toLocalInput(iso: string | null | undefined): string {
  if (!iso) return ''
  const value = new Date(iso)
  value.setMinutes(value.getMinutes() - value.getTimezoneOffset())
  return value.toISOString().slice(0, 16)
}
