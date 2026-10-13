import { useStore } from '@nanostores/react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { useI18n } from '@/i18n'
import { Cloud, RefreshCw } from '@/lib/icons'
import { confirm } from '@/store/confirm'
import { $settingsScopeProfile } from '@/store/settings-scope'

import {
  createWebDAVBackup,
  listWebDAVBackups,
  restoreWebDAVBackup,
  testWebDAV,
  type WebDAVBackupFile,
  type WebDAVConnection
} from '@/api/cloud-backup'
import { getCloudBackupCopy } from './cloud-backup-copy'
import { SettingsContent, SettingsSection } from './primitives'

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(value >= 10 ? 1 : 2)} ${units[unit]}`
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

export function CloudBackupSettings() {
  const { locale } = useI18n()
  const copy = getCloudBackupCopy(locale)
  const scopeProfile = useStore($settingsScopeProfile)
  const [url, setUrl] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [passphrase, setPassphrase] = useState('')
  const [files, setFiles] = useState<WebDAVBackupFile[] | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const connection: WebDAVConnection = { url: url.trim(), username, password }
  const profile = scopeProfile || 'default'
  const canUse = Boolean(connection.url)
  const canBackUp = canUse && passphrase.length >= 12 && !busy

  const run = async (key: string, action: () => Promise<void>) => {
    setBusy(key)
    setMessage('')
    setError('')
    try {
      await action()
    } catch (caught) {
      setError(`${copy.error}: ${errorMessage(caught)}`)
    } finally {
      setBusy(null)
    }
  }

  const refresh = async () => {
    const result = await listWebDAVBackups(connection, profile)
    setFiles(result.files)
  }

  const restore = async (file: WebDAVBackupFile) => {
    if (passphrase.length < 12) {
      setError(copy.required)
      return
    }
    const approved = await confirm({
      title: copy.restoreTitle,
      description: copy.restoreConfirm,
      details: [{ label: 'Backup', value: file.filename }],
      confirmLabel: copy.restore,
      cancelLabel: copy.cancel,
      destructive: true
    })
    if (!approved) return

    await run(file.filename, async () => {
      await restoreWebDAVBackup(connection, passphrase, file.filename, profile)
      setMessage(copy.restored)
    })
  }

  return (
    <SettingsContent>
      <div className="mx-auto max-w-3xl">
        <div className="mb-5">
          <h2 className="text-lg font-semibold">{copy.title}</h2>
          <p className="mt-1 text-sm text-muted-foreground">{copy.intro}</p>
        </div>

        <div className="mb-5 rounded-lg border border-amber-500/30 bg-amber-500/5 px-4 py-3 text-sm leading-relaxed text-muted-foreground">
          {copy.warning}
        </div>

        <SettingsSection icon={Cloud} title={copy.destination}>
          <div className="grid gap-3 rounded-lg border border-border/70 p-4">
            <label className="grid gap-1.5 text-sm">
              <span>{copy.url}</span>
              <Input
                autoComplete="url"
                onChange={event => {
                  setUrl(event.target.value)
                  setFiles(null)
                }}
                placeholder="https://server.example/dav/backups/"
                value={url}
              />
            </label>
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="grid gap-1.5 text-sm">
                <span>{copy.username}</span>
                <Input autoComplete="username" onChange={event => setUsername(event.target.value)} value={username} />
              </label>
              <label className="grid gap-1.5 text-sm">
                <span>{copy.password}</span>
                <Input
                  autoComplete="current-password"
                  onChange={event => setPassword(event.target.value)}
                  type="password"
                  value={password}
                />
              </label>
            </div>
            <label className="grid gap-1.5 text-sm">
              <span>{copy.passphrase}</span>
              <Input
                autoComplete="new-password"
                onChange={event => setPassphrase(event.target.value)}
                type="password"
                value={passphrase}
              />
              <span className="text-xs text-muted-foreground">{copy.passphraseHint}</span>
            </label>
            <div className="flex flex-wrap gap-2 pt-1">
              <Button
                disabled={!canUse || Boolean(busy)}
                onClick={() =>
                  void run('test', async () => {
                    await testWebDAV(connection, profile)
                    setMessage(copy.connected)
                  })
                }
                type="button"
                variant="outline"
              >
                {busy === 'test' ? copy.loading : copy.testConnection}
              </Button>
              <Button
                disabled={!canBackUp}
                onClick={() =>
                  void run('create', async () => {
                    const created = await createWebDAVBackup(connection, passphrase, profile)
                    setMessage(`${copy.uploaded} ${created.filename}`)
                    const result = await listWebDAVBackups(connection, profile)
                    setFiles(result.files)
                  })
                }
                type="button"
              >
                {busy === 'create' ? copy.loading : copy.createBackup}
              </Button>
              <Button
                disabled={!canUse || Boolean(busy)}
                onClick={() =>
                  void run('list', async () => {
                    await refresh()
                  })
                }
                type="button"
                variant="ghost"
              >
                <RefreshCw className="mr-1.5 size-4" />
                {busy === 'list' ? copy.loading : copy.refreshList}
              </Button>
            </div>
          </div>
        </SettingsSection>

        {message && <p className="mb-3 rounded-md bg-emerald-500/10 px-3 py-2 text-sm text-emerald-700">{message}</p>}
        {error && <p className="mb-3 rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</p>}

        <SettingsSection icon={Cloud} title={copy.backups}>
          {files === null ? (
            <p className="py-3 text-sm text-muted-foreground">{copy.empty}</p>
          ) : files.length === 0 ? (
            <p className="py-3 text-sm text-muted-foreground">{copy.empty}</p>
          ) : (
            <div className="divide-y divide-border/70 rounded-lg border border-border/70">
              {files.map(file => {
                const parsedDate = file.modified ? new Date(file.modified) : null
                const modified = parsedDate && !Number.isNaN(parsedDate.getTime())
                  ? parsedDate.toLocaleString(locale)
                  : file.modified
                return (
                  <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3" key={file.filename}>
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium">{file.filename}</div>
                      <div className="mt-1 text-xs text-muted-foreground">
                        {copy.size}: {formatSize(file.size)}
                        {modified ? ` · ${copy.modified}: ${modified}` : ''}
                      </div>
                    </div>
                    <Button
                      disabled={!canUse || passphrase.length < 12 || Boolean(busy)}
                      onClick={() => void restore(file)}
                      type="button"
                      variant="outline"
                    >
                      {busy === file.filename ? copy.loading : copy.restore}
                    </Button>
                  </div>
                )
              })}
            </div>
          )}
        </SettingsSection>
      </div>
    </SettingsContent>
  )
}
