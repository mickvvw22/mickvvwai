import fs from 'node:fs'
import path from 'node:path'

export type TelegramFileStorageLocation = 'install-sidecar' | 'install-directory' | 'user-data' | 'profile-cache'

export interface TelegramFileStorageRoot {
  root: string | null
  location: TelegramFileStorageLocation
}

interface ResolveTelegramFileStorageRootOptions {
  packaged: boolean
  platform: string
  resourcesPath?: string
  appImagePath?: string
  appUserDataPath: string
  appName: string
  canWrite?: (directory: string) => boolean
}

function canWriteDirectory(directory: string): boolean {
  let probe: string | null = null
  try {
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 })
    probe = path.join(directory, `.telegram-files-write-test-${process.pid}-${Date.now()}`)
    fs.writeFileSync(probe, '', { flag: 'wx', mode: 0o600 })
    fs.unlinkSync(probe)
    return true
  } catch {
    if (probe) {
      try {
        fs.unlinkSync(probe)
      } catch {
        // Ignore a failed cleanup probe; the candidate is treated as unwritable.
      }
    }
    return false
  }
}

function safeDirectoryName(name: string): string {
  return (
    name
      .trim()
      .replace(/[^a-zA-Z0-9._-]+/g, '-')
      .replace(/^-+|-+$/g, '') || 'Mickvvw'
  )
}

/**
 * Pick a durable Telegram file root. Packaged apps first use a product-specific
 * sibling folder to avoid placing user data inside files replaced by upgrades.
 * AppImage data is always kept beside the image, never in its read-only mount.
 */
export function resolveTelegramFileStorageRoot({
  packaged,
  platform,
  resourcesPath,
  appImagePath,
  appUserDataPath,
  appName,
  canWrite = canWriteDirectory
}: ResolveTelegramFileStorageRootOptions): TelegramFileStorageRoot {
  const installCandidates: Array<{ root: string; location: 'install-sidecar' | 'install-directory' }> = []
  const appFolder = safeDirectoryName(appName)

  if (packaged) {
    if (appImagePath) {
      installCandidates.push({
        root: path.join(path.dirname(appImagePath), `${appFolder}-data`, 'telegram_files'),
        location: 'install-sidecar'
      })
    } else if (resourcesPath) {
      const installRoot = platform === 'darwin' ? path.resolve(resourcesPath, '..', '..') : path.dirname(resourcesPath)
      installCandidates.push({
        root: path.join(path.dirname(installRoot), `${appFolder}-data`, 'telegram_files'),
        location: 'install-sidecar'
      })
      installCandidates.push({
        root: path.join(installRoot, 'telegram_files'),
        location: 'install-directory'
      })
    }
  }

  for (const candidate of installCandidates) {
    if (canWrite(candidate.root)) {
      return { root: candidate.root, location: candidate.location }
    }
  }

  const userDataRoot = path.join(appUserDataPath, 'telegram_files')
  if (canWrite(userDataRoot)) {
    return { root: userDataRoot, location: 'user-data' }
  }

  return { root: null, location: 'profile-cache' }
}
