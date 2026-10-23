import { atom, read, update } from 'claude-code'
import type { Register, SessionUsage } from 'claude-code'

import type { Meter } from '../types'

const meter = atom({ plugin: 'token-meter', key: 'meter' } as const, null)
const isHidden = atom({ plugin: 'token-meter', key: 'isHidden' } as const, false)

const BAR_WIDTH = 20

function toMeter(u: Pick<SessionUsage, 'context' | 'rateLimits' | 'cost'>): Meter {
  return {
    tokens: u.context.tokens ?? null,
    window: u.context.window,
    percent: u.context.percent ?? null,
    limits: u.rateLimits.map(r => ({ kind: r.kind, percentUsed: r.percentUsed, resetsAt: r.resetsAt })),
    usd: u.cost?.usd ?? null,
  }
}

function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n % 1_000_000 === 0 ? 0 : 1)}M`
  if (n >= 1_000) return `${Math.round(n / 1_000)}k`
  return String(n)
}

function bar(percent: number): string {
  const filled = Math.max(0, Math.min(BAR_WIDTH, Math.round((percent / 100) * BAR_WIDTH)))
  return '█'.repeat(filled) + '░'.repeat(BAR_WIDTH - filled)
}

function colorFor(percent: number): string {
  if (percent >= 85) return 'error'
  if (percent >= 60) return 'warning'
  return 'success'
}

const LIMIT_NAMES: Record<string, string> = { five_hour: '5h', seven_day: '7d', spend_limit: 'spend' }

function resetIn(iso: string | undefined, now: number): string {
  if (!iso) return ''
  const ms = Date.parse(iso) - now
  if (!(ms > 0)) return ''
  const h = Math.floor(ms / 3_600_000)
  const m = Math.floor((ms % 3_600_000) / 60_000)
  return h >= 24 ? ` ↻${Math.floor(h / 24)}d` : h > 0 ? ` ↻${h}h${m}m` : ` ↻${m}m`
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const result = await next(e)
    const usage = await $.session.usage()
    await update($, meter, () => toMeter(usage))
    $.command.register({ name: 'tokens', description: 'Show or hide the token meter' })
    return result
  })

  // Pushed by the engine after every turn and whenever a rate-limit window moves.
  on('session.measure', async ($, e, next) => {
    await update($, meter, () => toMeter(e))
    const percent = e.context.percent ?? 0
    if (e.changed.includes('context') && percent >= 90) {
      $.ui.toast(`Context ${percent}% full, consider /compact`)
    }
    return next(e)
  })

  on('command.run', { command: 'tokens' }, async ($, e, next) => {
    const hidden = await update($, isHidden, v => !v)
    return { text: hidden ? 'Token meter hidden' : 'Token meter shown' }
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const m = await read($, meter)
    if (e.props.hasSurvey || m === null || (await read($, isHidden))) {
      return next(e)
    }

    const { Box, Text } = $.ui.resolve(e)
    const now = await $.clock.now()
    const percent = m.percent ?? 0
    const used = m.tokens === null ? '—' : compact(m.tokens)

    return (
      <Box flexDirection="row" gap={1}>
        <Text dimColor>Context</Text>
        <Text key="bar" color={colorFor(percent)}>{bar(percent)}</Text>
        <Text key="ctx" bold>
          {m.percent === null ? '—' : `${percent}%`}
        </Text>
        <Text dimColor>
          {used} / {compact(m.window)}
        </Text>
        {m.limits.map(l => (
          <Text key={`limit-${l.kind}`} color={colorFor(l.percentUsed)}>
            · {LIMIT_NAMES[l.kind] ?? l.kind} {l.percentUsed}%{resetIn(l.resetsAt, now)}
          </Text>
        ))}
        {m.usd === null ? null : <Text key="cost" dimColor>· ${m.usd.toFixed(2)}</Text>}
      </Box>
    )
  })
}
