import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { getHermesConfigRecord, saveHermesConfig } from '@/hermes'
import { useI18n } from '@/i18n'
import { Archive, Loader2 } from '@/lib/icons'
import { $settingsRequestProfile } from '@/store/settings-scope'
import { notify, readableError } from '@/store/notifications'
import type { HermesConfigRecord } from '@/types/hermes'

import { ListRow, SectionHeading, SettingsContent, ToggleRow } from './primitives'
import { SettingsProfileScope } from './profile-scope'

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {}
}

export function TelegramFileSettings() {
  const { t } = useI18n()
  const copy = t.settings.gateway
  const scopeProfile = useStore($settingsRequestProfile)
  const [record, setRecord] = useState<HermesConfigRecord | null>(null)
  const [cleanupEnabled, setCleanupEnabled] = useState(false)
  const [cleanupAfterDays, setCleanupAfterDays] = useState(30)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')

    void getHermesConfigRecord(scopeProfile)
      .then(config => {
        if (cancelled) return
        const telegram = asRecord(config.telegram)
        const fileHandling = asRecord(telegram.file_handling)
        const days = Number(fileHandling.cleanup_after_days)
        setRecord(config)
        setCleanupEnabled(fileHandling.cleanup_enabled === true)
        setCleanupAfterDays(Number.isFinite(days) ? Math.max(1, Math.min(3650, Math.trunc(days))) : 30)
      })
      .catch(err => {
        if (!cancelled) setError(readableError(err, copy.failedLoad).message)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [scopeProfile])

  const save = async () => {
    if (!record) return
    setSaving(true)
    setError('')
    const telegram = asRecord(record.telegram)
    const current = asRecord(telegram.file_handling)
    const days = Math.max(1, Math.min(3650, Math.trunc(cleanupAfterDays) || 30))
    const next: HermesConfigRecord = {
      ...record,
      telegram: {
        ...telegram,
        file_handling: {
          ...current,
          cleanup_enabled: cleanupEnabled,
          cleanup_after_days: days
        }
      }
    }

    try {
      const result = await saveHermesConfig(next, scopeProfile)
      if (!result.ok) throw new Error(copy.telegramFilesSaveFailed)
      setRecord(next)
      setCleanupAfterDays(days)
      notify({ kind: 'info', message: copy.telegramFilesSaved })
    } catch (err) {
      setError(readableError(err, copy.telegramFilesSaveFailed).message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <SettingsContent>
      <SettingsProfileScope className="mb-4" />
      <SectionHeading icon={Archive} title={copy.telegramFilesTitle} />
      <p className="mb-4 text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-(--ui-text-tertiary)">
        {copy.telegramFilesDescription}
      </p>

      <ToggleRow
        checked={cleanupEnabled}
        description={copy.telegramCleanupDescription}
        disabled={loading || saving}
        label={copy.telegramCleanupLabel}
        onChange={setCleanupEnabled}
      />

      <ListRow
        action={
          <Input
            aria-label={copy.telegramRetentionLabel}
            className="w-28"
            disabled={loading || saving || !cleanupEnabled}
            max={3650}
            min={1}
            onChange={event => setCleanupAfterDays(Math.max(1, Math.min(3650, Number(event.target.value) || 1)))}
            type="number"
            value={cleanupAfterDays}
          />
        }
        description={copy.telegramRetentionDescription}
        title={copy.telegramRetentionLabel}
      />

      <p className="mt-3 text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-(--ui-text-tertiary)">
        {copy.telegramStorageNote}
      </p>
      <p className="mt-2 text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-(--ui-text-tertiary)">
        {copy.telegramArchiveSupportNote}
      </p>

      {error ? (
        <p className="mt-3 text-[length:var(--conversation-caption-font-size)] text-destructive" role="alert">
          {error}
        </p>
      ) : null}

      <div className="mt-4 flex items-center gap-2">
        <Button disabled={loading || saving || !record} onClick={() => void save()} type="button">
          {saving ? <Loader2 className="mr-2 size-4 animate-spin" /> : null}
          {saving ? copy.telegramFilesSaving : copy.telegramFilesSave}
        </Button>
        <span className="text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
          {copy.telegramFilesRestartNote}
        </span>
      </div>
    </SettingsContent>
  )
}
