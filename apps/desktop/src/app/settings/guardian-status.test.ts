import { describe, expect, it } from 'vitest'

import { guardianReadiness } from './guardian-status'

const healthy = {
  supported: true,
  enabled: true,
  supervisorRunning: true,
  heartbeatFresh: true
} as const

describe('guardianReadiness', () => {
  it('reports healthy only when the supervisor is live and its heartbeat is fresh', () => {
    expect(guardianReadiness(healthy, false)).toBe('active')
    expect(guardianReadiness({ ...healthy, heartbeatFresh: false }, false)).toBe('heartbeatStale')
    expect(guardianReadiness({ ...healthy, supervisorRunning: false }, false)).toBe('notRunning')
  })

  it('distinguishes disabled, unsupported, and still-loading states', () => {
    expect(guardianReadiness({ ...healthy, enabled: false }, false)).toBe('inactive')
    expect(guardianReadiness({ ...healthy, supported: false }, false)).toBe('unavailable')
    expect(guardianReadiness(null, true)).toBe('checking')
  })
})
