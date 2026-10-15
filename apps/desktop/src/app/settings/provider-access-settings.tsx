import { useCallback, useEffect, useState, type ReactElement } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  getCustomEndpoints,
  getHermesConfigRecord,
  saveHermesConfigRecord,
  setProviderAccessPassword,
  type CustomEndpoint,
  type HermesConfigRecord
} from '@/hermes'
import { useI18n } from '@/i18n'
import { notify, notifyError } from '@/store/notifications'

import { Pill, SectionHeading, SettingsContent, SettingsSection, SettingsSkeleton, ToggleRow } from './primitives'

interface ProviderAccessSection {
  enabled?: boolean
  allow_delete?: boolean
  sources?: Record<string, string>
}

function readSection(config: HermesConfigRecord | null): Required<Omit<ProviderAccessSection, 'sources'>> & Pick<ProviderAccessSection, 'sources'> {
  const raw = (config?.provider_access ?? {}) as ProviderAccessSection
  return {
    enabled: raw.enabled !== false,
    allow_delete: raw.allow_delete === true,
    sources: raw.sources ?? {}
  }
}

const SOURCE_BADGE: Record<string, string> = {
  bot: 'badgeBot',
  chat: 'badgeChat',
  desktop: 'badgeDesktop'
}

export function ProviderAccessSettings(): ReactElement {
  const { t } = useI18n()
  const copy = t.settings.providerAccess
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [section, setSection] = useState<ReturnType<typeof readSection>>(() => readSection(null))
  const [endpoints, setEndpoints] = useState<CustomEndpoint[]>([])
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [changingPassword, setChangingPassword] = useState(false)

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      const [config, custom] = await Promise.all([getHermesConfigRecord(), getCustomEndpoints()])
      setSection(readSection(config))
      setEndpoints(custom.endpoints ?? [])
    } catch (err) {
      notifyError(err, copy.loadFailed)
    } finally {
      setLoading(false)
    }
  }, [copy.loadFailed])

  useEffect(() => {
    void reload()
  }, [reload])

  const persist = useCallback(
    async (next: ReturnType<typeof readSection>) => {
      setSaving(true)
      try {
        const config = await getHermesConfigRecord()
        await saveHermesConfigRecord({ ...config, provider_access: next })
        setSection(next)
        notify({ kind: 'success', message: copy.saved })
      } catch (err) {
        notifyError(err, copy.saveFailed)
      } finally {
        setSaving(false)
      }
    },
    [copy.saved, copy.saveFailed]
  )

  const handleChangePassword = useCallback(async () => {
    if (!newPassword) {
      notifyError(new Error(copy.passwordEmpty), copy.passwordEmpty)
      return
    }
    if (newPassword !== confirmPassword) {
      notifyError(new Error(copy.passwordMismatch), copy.passwordMismatch)
      return
    }
    setChangingPassword(true)
    try {
      // The password itself is hashed server-side; we only ever send the
      // plaintext over the local loopback API, never store it.
      await setProviderAccessPassword(newPassword)
      setNewPassword('')
      setConfirmPassword('')
      notify({ kind: 'success', message: copy.passwordChanged })
    } catch (err) {
      notifyError(err, copy.saveFailed)
    } finally {
      setChangingPassword(false)
    }
  }, [newPassword, confirmPassword, copy])

  if (loading) {
    return <SettingsSkeleton search sections={[{ rows: 4 }]} />
  }

  const badgeLabel = (source?: string): string | null => {
    if (!source || !(source in SOURCE_BADGE)) return null
    return copy[SOURCE_BADGE[source] as keyof typeof copy] as string
  }

  return (
    <SettingsContent>
      <SettingsSection>
        <SectionHeading title={copy.title} description={copy.blurb} />
        <ToggleRow
          checked={section.enabled}
          disabled={saving}
          label={copy.enabledLabel}
          description={copy.enabledDescription}
          onChange={on => void persist({ ...section, enabled: on })}
        />
        <ToggleRow
          checked={section.allow_delete}
          disabled={saving}
          label={copy.allowDeleteLabel}
          description={copy.allowDeleteDescription}
          onChange={on => void persist({ ...section, allow_delete: on })}
        />
      </SettingsSection>

      <SettingsSection>
        <SectionHeading title={copy.passwordTitle} description={copy.passwordDescription} />
        <div className="grid gap-2">
          <Input
            type="password"
            autoComplete="new-password"
            placeholder={copy.newPasswordPlaceholder}
            value={newPassword}
            onChange={e => setNewPassword(e.target.value)}
          />
          <Input
            type="password"
            autoComplete="new-password"
            placeholder={copy.confirmPasswordPlaceholder}
            value={confirmPassword}
            onChange={e => setConfirmPassword(e.target.value)}
          />
          <div>
            <Button disabled={changingPassword || !newPassword} onClick={() => void handleChangePassword()}>
              {changingPassword ? copy.changing : copy.changePassword}
            </Button>
          </div>
        </div>
      </SettingsSection>

      <SettingsSection>
        <SectionHeading title={copy.addedTitle} description={copy.addedDescription} />
        {endpoints.length === 0 ? (
          <p className="text-sm text-muted-foreground">{copy.noEndpoints}</p>
        ) : (
          <div className="grid gap-2">
            {endpoints.map(ep => {
              const source = ep.added_via ?? section.sources?.[ep.id]
              const badge = badgeLabel(source)
              return (
                <div key={ep.id} className="flex items-center justify-between rounded-[6px] border px-3 py-2">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium">{ep.name}</div>
                    <div className="truncate text-xs text-muted-foreground">{ep.base_url}</div>
                  </div>
                  {badge ? <Pill>{badge}</Pill> : null}
                </div>
              )
            })}
          </div>
        )}
      </SettingsSection>
    </SettingsContent>
  )
}
