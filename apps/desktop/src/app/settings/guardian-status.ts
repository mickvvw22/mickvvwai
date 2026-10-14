import type { GuardianStatus } from '../../../electron/guardian'

export type GuardianReadiness = 'checking' | 'unavailable' | 'inactive' | 'notRunning' | 'heartbeatStale' | 'active'

type ReadinessInput = Pick<GuardianStatus, 'supported' | 'enabled' | 'supervisorRunning' | 'heartbeatFresh'>

/** Only report active when both the per-user supervisor and its heartbeat are healthy. */
export function guardianReadiness(status: ReadinessInput | null, loading: boolean): GuardianReadiness {
  if (loading) {return 'checking'}

  if (!status?.supported) {return 'unavailable'}

  if (!status.enabled) {return 'inactive'}

  if (!status.supervisorRunning) {return 'notRunning'}

  if (!status.heartbeatFresh) {return 'heartbeatStale'}

  return 'active'
}
