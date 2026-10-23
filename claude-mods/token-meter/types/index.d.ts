export type Limit = { kind: string; percentUsed: number; resetsAt?: string }
export type Meter = {
  tokens: number | null
  window: number
  percent: number | null
  limits: Limit[]
  usd: number | null
}

declare module 'claude-code' {
  interface PluginState {
    'token-meter': { meter: Meter | null; isHidden: boolean }
  }
}
