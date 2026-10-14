import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { type Translations, useI18n } from '@/i18n'
import { AlertCircle, Check, Loader2, RefreshCw, ShieldLock } from '@/lib/icons'

import type { GuardianStatus } from '../../../electron/guardian'

import { guardianReadiness } from './guardian-status'
import { ListRow, Pill, SettingsContent, SettingsSection } from './primitives'

function gatewayLabel(status: GuardianStatus, copy: Translations['settings']['guardian']): string {
  switch (status.gatewayState) {
    case 'running': return copy.gatewayRunning

    case 'starting': return copy.gatewayStarting

    case 'recovering': return copy.gatewayRecovering

    case 'stopped_by_user': return copy.gatewayStoppedByUser

    case 'unavailable': return copy.gatewayUnavailable

    case 'not_managed': return copy.gatewayNotManaged

    default: return copy.unknown
  }
}

export function GuardianSettings() {
  const { locale, t } = useI18n()
  const copy = t.settings.guardian
  const [status, setStatus] = useState<GuardianStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)

  useEffect(() => {
    let active = true

    const refresh = async () => {
      try {
        const next = await window.hermesDesktop.guardian.getStatus()

        if (active) {setStatus(next)}
      } catch {
        if (active) {setStatus(null)}
      } finally {
        if (active) {setLoading(false)}
      }
    }

    void refresh()
    const timer = window.setInterval(() => void refresh(), 10_000)

    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [refreshKey])

  const toggle = async () => {
    if (!status?.supported || saving) {return}
    setSaving(true)

    try {
      const next = await window.hermesDesktop.guardian.setEnabled(!status.enabled)
      setStatus(next)
    } finally {
      setSaving(false)
    }
  }

  const readiness = guardianReadiness(status, loading)

  const readinessTone = readiness === 'active'
    ? 'success'
    : readiness === 'notRunning' || readiness === 'heartbeatStale'
      ? 'warn'
      : 'muted'

  const heartbeat = status?.heartbeatAt
    ? `${new Date(status.heartbeatAt).toLocaleString(locale)}${status.heartbeatFresh ? '' : ` · ${copy.heartbeatStale}`}`
    : copy.noHeartbeat

  const gateway = status ? gatewayLabel(status, copy) : copy.unknown
  const lastError = status?.lastError ? copy.errors[status.lastError as keyof typeof copy.errors] ?? copy.errors.genericError : null

  return (
    <SettingsContent>
      <SettingsSection icon={ShieldLock} title={copy.title}>
        <p className="mb-4 text-sm text-muted-foreground">{copy.blurb}</p>
        <div className="rounded-lg border border-border/70 bg-card px-4">
          <ListRow
            action={(
              <Button
                aria-pressed={Boolean(status?.enabled)}
                disabled={!status?.supported || saving || loading}
                onClick={() => void toggle()}
                size="sm"
                type="button"
                variant={status?.enabled ? 'default' : 'outline'}
              >
                {saving ? <Loader2 className="mr-1.5 size-3.5 animate-spin" /> : null}
                {status?.enabled ? copy.turnOff : copy.turnOn}
              </Button>
            )}
            description={copy.switchDescription}
            title={copy.switchTitle}
          />
          <ListRow
            action={(
              <Pill tone={readinessTone}>{copy[readiness]}</Pill>
            )}
            description={copy.statusDescription}
            title={copy.statusTitle}
          />
          <ListRow
            action={<Pill tone={status?.autostartInstalled ? 'success' : 'muted'}>{status?.autostartInstalled ? copy.enabled : copy.disabled}</Pill>}
            description={copy.autostartDescription}
            title={copy.autostartTitle}
          />
          <ListRow
            action={<Pill tone={status?.gatewayState === 'running' ? 'success' : status?.gatewayState === 'recovering' ? 'warn' : 'muted'}>{gateway}</Pill>}
            description={copy.gatewayDescription}
            title={copy.gatewayTitle}
          />
          <ListRow
            action={(
              <span className="max-w-56 text-right text-xs text-muted-foreground">
                {readiness === 'active' ? <Check className="mr-1 inline size-3.5 text-emerald-600" /> : null}
                {heartbeat}
              </span>
            )}
            description={copy.heartbeatDescription}
            title={copy.heartbeatTitle}
          />
          {status?.appPid ? (
            <ListRow action={<span className="font-mono text-xs text-muted-foreground">PID {status.appPid}</span>} title={copy.desktopProcess} />
          ) : null}
          {status?.supervisorRunning && status.heartbeatFresh && status.gatewayPid ? (
            <ListRow action={<span className="font-mono text-xs text-muted-foreground">PID {status.gatewayPid}</span>} title={copy.gatewayProcess} />
          ) : null}
          {status?.supported ? (
            <ListRow
              action={(
                <span className="max-w-64 text-right text-xs text-muted-foreground">
                  {copy.appRestarts(status.appRestartCount)} · {copy.gatewayRestarts(status.gatewayRestartCount)}
                </span>
              )}
              title={copy.restartCounts}
            />
          ) : null}
        </div>

        {!status?.supported ? (
          <div className="mt-4 flex gap-2 rounded-md border border-border/70 bg-muted/30 p-3 text-sm text-muted-foreground">
            <AlertCircle className="mt-0.5 size-4 shrink-0" />
            <span>{status?.reason ? copy.unsupported[status.reason] : loading ? copy.checking : copy.unavailable}</span>
          </div>
        ) : null}
        {lastError ? (
          <div className="mt-4 flex gap-2 rounded-md border border-amber-500/30 bg-amber-500/5 p-3 text-sm text-amber-800 dark:text-amber-200">
            <AlertCircle className="mt-0.5 size-4 shrink-0" />
            <span>{lastError}</span>
          </div>
        ) : null}
        <p className="mt-4 text-xs leading-relaxed text-muted-foreground">{copy.networkNote}</p>
        <div className="mt-4 flex justify-end">
          <Button onClick={() => setRefreshKey(key => key + 1)} size="sm" type="button" variant="ghost">
            <RefreshCw className="mr-1.5 size-3.5" />
            {copy.refresh}
          </Button>
        </div>
      </SettingsSection>
    </SettingsContent>
  )
}
