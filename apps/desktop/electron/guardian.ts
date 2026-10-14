import { spawn } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

const GUARDIAN_VERSION = 2
const AUTOSTART_MARKER = 'X-Mickvvw-Guardian=true'
const HEARTBEAT_FRESH_MS = 45_000
const SUPERVISOR_STARTUP_TIMEOUT_MS = 30_000
const SUPERVISOR_RESTART_DELAYS_MS = [2_000, 5_000, 15_000, 30_000, 60_000] as const
const DIAGNOSTIC_LOG_LIMIT_BYTES = 256 * 1024

const GUI_ENV_KEYS = [
  'DISPLAY',
  'WAYLAND_DISPLAY',
  'XAUTHORITY',
  'XDG_RUNTIME_DIR',
  'DBUS_SESSION_BUS_ADDRESS',
  'XDG_SESSION_TYPE',
  'XDG_CURRENT_DESKTOP',
  'DESKTOP_SESSION',
  'GDK_BACKEND'
] as const

export interface GuardianStatus {
  supported: boolean
  reason: 'unsupported_platform' | 'unpackaged' | 'bundled_runtime_unavailable' | null
  enabled: boolean
  autostartInstalled: boolean
  supervisorRunning: boolean
  heartbeatFresh: boolean
  heartbeatAt: string | null
  appRunning: boolean
  appPid: number | null
  gatewayState: 'running' | 'starting' | 'recovering' | 'stopped_by_user' | 'unavailable' | 'not_managed' | 'unknown'
  gatewayPid: number | null
  appRestartCount: number
  gatewayRestartCount: number
  lastError: string | null
}

interface GuardianOptions {
  platform: string
  packaged: boolean
  resourcesPath: string
  userDataPath: string
  home: string
  hermesHome: string
  profile: string | null
  appCommand: string
  appArgs?: string[]
  manageGateway: boolean
  environment?: Record<string, string | undefined>
}

interface BundledRuntime {
  pythonPath: string
  helperPath: string
  hermesCommand: string
  repoDir: string
  sitePackages: string
  toolsDir: string
}

interface StoredConfig {
  version: number
  enabled: boolean
  intervalSeconds: number
  app: { command: string; args: string[]; cwd: string; pid: number; startTime: string | null }
  gateway: { command: string; args: string[]; cwd: string; home: string } | null
  runtime: { repoDir: string; sitePackages: string }
  environment: Record<string, string>
}

function readJson(file: string): Record<string, any> | null {
  try {
    const value = JSON.parse(fs.readFileSync(file, 'utf8'))

    return value && typeof value === 'object' && !Array.isArray(value) ? value : null
  } catch {
    return null
  }
}

function processStartTime(pid: number): string | null {
  if (process.platform !== 'linux') {
    return null
  }

  try {
    const raw = fs.readFileSync(`/proc/${pid}/stat`, 'utf8')
    const end = raw.lastIndexOf(')')

    if (end < 0) {return null}
    const fields = raw.slice(end + 2).trim().split(/\s+/)

    return fields.length > 19 ? fields[19] : null
  } catch {
    return null
  }
}

function processIdentityAlive(pid: unknown, startTime: unknown): boolean {
  if (!Number.isInteger(pid) || Number(pid) <= 0 || startTime == null) {return false}

  return processStartTime(Number(pid)) === String(startTime)
}

function atomicWrite(file: string, content: string, mode: number): void {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 })
  const temp = `${file}.${process.pid}.tmp`

  try {
    fs.writeFileSync(temp, content, { encoding: 'utf8', mode })
    fs.chmodSync(temp, mode)
    fs.renameSync(temp, file)
    fs.chmodSync(file, mode)
  } finally {
    try {
      fs.unlinkSync(temp)
    } catch {
      // The temporary file was already renamed or never created.
    }
  }
}

function resolveBundledRuntime(resourcesPath: string): BundledRuntime | null {
  const payloadRoot = path.join(resourcesPath, 'agent-payload')
  const manifest = readJson(path.join(payloadRoot, 'manifest.json'))
  const runtime = manifest?.runtime

  if (!runtime || typeof runtime !== 'object') {return null}
  const required = ['storePython', 'repoDir', 'sitePackages', 'toolsDir']

  if (required.some(key => typeof runtime[key] !== 'string')) {return null}
  const hermesRelative = runtime.commands?.hermes

  if (typeof hermesRelative !== 'string') {return null}

  const resolved = {
    pythonPath: path.join(payloadRoot, runtime.storePython),
    helperPath: path.join(resourcesPath, 'guardian-supervisor.py'),
    hermesCommand: path.join(payloadRoot, hermesRelative),
    repoDir: path.join(payloadRoot, runtime.repoDir),
    sitePackages: path.join(payloadRoot, runtime.sitePackages),
    toolsDir: path.join(payloadRoot, runtime.toolsDir)
  }

  if (Object.values(resolved).some(value => !fs.existsSync(value))) {return null}

  return resolved
}

function desktopExecArg(value: string): string {
  // Desktop Entry Exec quoting (not shell quoting). Percent must be doubled so
  // a path containing '%' cannot become a field code.
  return `"${value.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/%/g, '%%').replace(/`/g, '\\`').replace(/\$/g, '\\$')}"`
}

export function renderGuardianAutostart(command: string, args: string[] = []): string {
  const commandLine = [command, ...args].map(desktopExecArg).join(' ')

  return [
    '[Desktop Entry]',
    'Type=Application',
    'Name=Mickvvw',
    'Comment=Start Mickvvw with local process recovery enabled',
    `Exec=${commandLine}`,
    `TryExec=${desktopExecArg(command)}`,
    'Terminal=false',
    'StartupNotify=false',
    'NoDisplay=true',
    'X-GNOME-Autostart-enabled=true',
    AUTOSTART_MARKER,
    ''
  ].join('\n')
}

function safeAppArgs(args: string[] | undefined): string[] {
  return (args ?? []).filter(arg => arg === '--no-sandbox')
}

function safeEnvironment(options: GuardianOptions, runtime: BundledRuntime): Record<string, string> {
  const inherited = options.environment ?? {}
  const pathValue = inherited.PATH || process.env.PATH || '/usr/local/bin:/usr/bin:/bin'

  const env: Record<string, string> = {
    HOME: options.home,
    HERMES_HOME: options.hermesHome,
    HERMES_RUNTIME_DIR: runtime.toolsDir,
    PATH: `${runtime.toolsDir}${path.delimiter}${pathValue}`,
    PYTHONUTF8: '1'
  }

  for (const key of ['LANG', 'LC_ALL', 'TZ'] as const) {
    if (inherited[key]) {env[key] = inherited[key] as string}
  }

  const libraryPath = inherited.LD_LIBRARY_PATH ?? process.env.LD_LIBRARY_PATH

  if (libraryPath) {env.LD_LIBRARY_PATH = libraryPath}

  return env
}

function profileHome(home: string, profile: string | null): string {
  return profile && profile !== 'default' ? path.join(home, 'profiles', profile) : home
}

function defaultStatus(reason: GuardianStatus['reason'], enabled = true): GuardianStatus {
  return {
    supported: false,
    reason,
    enabled,
    autostartInstalled: false,
    supervisorRunning: false,
    heartbeatFresh: false,
    heartbeatAt: null,
    appRunning: false,
    appPid: null,
    gatewayState: 'unknown',
    gatewayPid: null,
    appRestartCount: 0,
    gatewayRestartCount: 0,
    lastError: null
  }
}

export function createGuardianManager(options: GuardianOptions) {
  const runtime = options.platform === 'linux' && options.packaged
    ? resolveBundledRuntime(options.resourcesPath)
    : null

  const reason: GuardianStatus['reason'] = options.platform !== 'linux'
    ? 'unsupported_platform'
    : !options.packaged
      ? 'unpackaged'
      : !runtime
        ? 'bundled_runtime_unavailable'
        : null

  const supported = reason === null && runtime !== null
  const guardDir = path.join(options.userDataPath, 'guardian')
  const configPath = path.join(guardDir, 'config.json')
  const statusPath = path.join(guardDir, 'status.json')
  const lockPath = path.join(guardDir, 'supervisor.lock')
  const userStopPath = path.join(guardDir, 'user-stop')
  const configHome = options.environment?.XDG_CONFIG_HOME || process.env.XDG_CONFIG_HOME || path.join(options.home, '.config')
  const autostartPath = path.join(configHome, 'autostart', 'mickvvw-guardian.desktop')
  const diagnosticsPath = path.join(guardDir, 'diagnostics.jsonl')
  let managerError: string | null = null
  let supervisorRestartTimer: NodeJS.Timeout | null = null
  let supervisorStartupTimer: NodeJS.Timeout | null = null
  let supervisorRestartAttempt = 0
  let stoppingForUser = false

  function readEnabled(): boolean {
    const saved = readJson(configPath)

    return typeof saved?.enabled === 'boolean' ? saved.enabled : true
  }

  function writeDiagnostic(event: string, details: Record<string, string | number | null> = {}): void {
    try {
      fs.mkdirSync(guardDir, { recursive: true, mode: 0o700 })

      if (fs.existsSync(diagnosticsPath) && fs.statSync(diagnosticsPath).size >= DIAGNOSTIC_LOG_LIMIT_BYTES) {
        fs.writeFileSync(diagnosticsPath, '', { encoding: 'utf8', mode: 0o600 })
      }

      fs.appendFileSync(
        diagnosticsPath,
        `${JSON.stringify({ at: new Date().toISOString(), event, ...details })}\n`,
        { encoding: 'utf8', mode: 0o600 }
      )
      fs.chmodSync(diagnosticsPath, 0o600)
    } catch {
      // Diagnostics must never prevent the desktop app or supervisor from running.
    }
  }

  function clearSupervisorTimers(): void {
    if (supervisorStartupTimer) {
      clearTimeout(supervisorStartupTimer)
      supervisorStartupTimer = null
    }

    if (supervisorRestartTimer) {
      clearTimeout(supervisorRestartTimer)
      supervisorRestartTimer = null
    }
  }

  function hasFreshSupervisorHeartbeat(): boolean {
    const status = readJson(statusPath)

    if (
      !status || status.version !== GUARDIAN_VERSION || status.supervisorRunning !== true ||
      !processIdentityAlive(status.supervisorPid, status.supervisorStartTime)
    ) {
      return false
    }

    const heartbeatTime = Date.parse(String(status.heartbeatAt || ''))

    return Number.isFinite(heartbeatTime) && Date.now() - heartbeatTime <= HEARTBEAT_FRESH_MS
  }

  function scheduleSupervisorRestart(reason: string): void {
    if (!supported || stoppingForUser || !readEnabled() || fs.existsSync(userStopPath) || supervisorRestartTimer) {return}

    const attempt = supervisorRestartAttempt + 1
    const delayMs = SUPERVISOR_RESTART_DELAYS_MS[Math.min(attempt - 1, SUPERVISOR_RESTART_DELAYS_MS.length - 1)]
    supervisorRestartAttempt = attempt
    writeDiagnostic('supervisor_retry_scheduled', { reason, attempt, delayMs })
    supervisorRestartTimer = setTimeout(() => {
      supervisorRestartTimer = null

      if (!stoppingForUser && readEnabled() && !fs.existsSync(userStopPath)) {spawnSupervisor()}
    }, delayMs)
    supervisorRestartTimer.unref()
  }

  function buildConfig(enabled: boolean): StoredConfig | null {
    if (!runtime) {return null}
    const profile = options.profile && options.profile !== 'default' ? options.profile : null
    const gatewayHome = profileHome(options.hermesHome, profile)
    const env = safeEnvironment(options, runtime)
    const appCommand = path.resolve(options.appCommand)

    const gateway = options.manageGateway
      ? {
          command: runtime.hermesCommand,
          args: [...(profile ? ['--profile', profile] : []), 'gateway', 'run'],
          cwd: gatewayHome,
          home: gatewayHome
        }
      : null

    return {
      version: GUARDIAN_VERSION,
      enabled,
      intervalSeconds: 15,
      app: {
        command: appCommand,
        args: safeAppArgs(options.appArgs),
        cwd: path.dirname(appCommand),
        pid: process.pid,
        startTime: processStartTime(process.pid)
      },
      gateway,
      runtime: { repoDir: runtime.repoDir, sitePackages: runtime.sitePackages },
      environment: env
    }
  }

  function writeConfig(enabled: boolean): boolean {
    const config = buildConfig(enabled)

    if (!config) {return false}
    atomicWrite(configPath, `${JSON.stringify(config, null, 2)}\n`, 0o600)

    return true
  }

  function autostartOwned(): boolean {
    try {
      return fs.readFileSync(autostartPath, 'utf8').includes(AUTOSTART_MARKER)
    } catch {
      return false
    }
  }

  function installAutostart(): boolean {
    const existing = (() => {
      try { return fs.readFileSync(autostartPath, 'utf8') } catch { return null }
    })()

    if (existing !== null && !existing.includes(AUTOSTART_MARKER)) {
      managerError = 'autostart_path_conflict'

      return false
    }

    const appCommand = path.resolve(options.appCommand)
    const args = safeAppArgs(options.appArgs)
    atomicWrite(autostartPath, renderGuardianAutostart(appCommand, args), 0o644)

    return true
  }

  function removeAutostart(): void {
    if (!autostartOwned()) {return}

    try {
      fs.unlinkSync(autostartPath)
    } catch {
      managerError = 'autostart_remove_failed'
    }
  }

  function terminateSupervisor(): void {
    const status = readJson(statusPath)

    if (!status || !processIdentityAlive(status.supervisorPid, status.supervisorStartTime)) {return}

    try {
      process.kill(Number(status.supervisorPid), 'SIGTERM')
    } catch {
      // It may have exited between the identity check and signal.
    }
  }

  function stopForUser(): void {
    if (!supported) {return}
    stoppingForUser = true
    clearSupervisorTimers()

    try {
      atomicWrite(userStopPath, `${Date.now()}\n`, 0o600)
    } catch {
      managerError = 'user_stop_marker_failed'
    }

    terminateSupervisor()
  }

  function spawnSupervisor(): boolean {
    if (!runtime) {return false}
    const status = readJson(statusPath)

    if (status && processIdentityAlive(status.supervisorPid, status.supervisorStartTime)) {
      const heartbeatTime = Date.parse(String(status.heartbeatAt || ''))

      if (Number.isFinite(heartbeatTime) && Date.now() - heartbeatTime <= HEARTBEAT_FRESH_MS && status.version === GUARDIAN_VERSION) {
        return true
      }

      terminateSupervisor()
    }

    try {
      const env: Record<string, string> = {
        HOME: options.home,
        HERMES_HOME: options.hermesHome,
        HERMES_RUNTIME_DIR: runtime.toolsDir,
        PATH: options.environment?.PATH || process.env.PATH || '/usr/local/bin:/usr/bin:/bin',
        PYTHONUTF8: '1'
      }

      const libraryPath = options.environment?.LD_LIBRARY_PATH ?? process.env.LD_LIBRARY_PATH

      if (libraryPath) {env.LD_LIBRARY_PATH = libraryPath}

      for (const key of GUI_ENV_KEYS) {
        const value = options.environment?.[key] ?? process.env[key]

        if (value) {env[key] = value}
      }

      for (const key of ['LANG', 'LC_ALL', 'TZ'] as const) {
        const value = options.environment?.[key] ?? process.env[key]

        if (value) {env[key] = value}
      }

      const child = spawn(runtime.pythonPath, [runtime.helperPath, configPath], {
        cwd: guardDir,
        detached: true,
        stdio: 'ignore',
        env
      })

      child.once('spawn', () => {
        writeDiagnostic('supervisor_spawned', { pid: child.pid ?? null })
        supervisorStartupTimer = setTimeout(() => {
          supervisorStartupTimer = null

          if (stoppingForUser || !readEnabled() || fs.existsSync(userStopPath) || hasFreshSupervisorHeartbeat()) {return}

          managerError = 'supervisor_startup_timeout'
          writeDiagnostic('supervisor_startup_timeout', { pid: child.pid ?? null })

          try {
            child.kill('SIGTERM')
          } catch {
            // The child may have exited between the heartbeat check and signal.
          }

          scheduleSupervisorRestart('startup_timeout')
        }, SUPERVISOR_STARTUP_TIMEOUT_MS)
        supervisorStartupTimer.unref()
      })
      child.once('error', error => {
        if (supervisorStartupTimer) {
          clearTimeout(supervisorStartupTimer)
          supervisorStartupTimer = null
        }

        const systemError = error as NodeJS.ErrnoException

        managerError = 'supervisor_start_failed'
        writeDiagnostic('supervisor_spawn_error', {
          code: typeof systemError.code === 'string' ? systemError.code : null,
          errno: typeof systemError.errno === 'number' ? systemError.errno : null,
          syscall: typeof systemError.syscall === 'string' ? systemError.syscall : null
        })
        scheduleSupervisorRestart('spawn_error')
      })
      child.once('exit', (code, signal) => {
        if (supervisorStartupTimer) {
          clearTimeout(supervisorStartupTimer)
          supervisorStartupTimer = null
        }

        if (stoppingForUser || !readEnabled() || fs.existsSync(userStopPath) || hasFreshSupervisorHeartbeat()) {return}

        managerError = 'supervisor_exited_before_ready'
        writeDiagnostic('supervisor_exit_before_ready', { pid: child.pid ?? null, exitCode: code, signal })
        scheduleSupervisorRestart('exit_before_ready')
      })
      child.unref()

      return true
    } catch (error) {
      const systemError = error as NodeJS.ErrnoException

      managerError = 'supervisor_start_failed'
      writeDiagnostic('supervisor_spawn_throw', {
        code: typeof systemError?.code === 'string' ? systemError.code : null,
        errno: typeof systemError?.errno === 'number' ? systemError.errno : null,
        syscall: typeof systemError?.syscall === 'string' ? systemError.syscall : null
      })
      scheduleSupervisorRestart('spawn_throw')

      return false
    }
  }

  function getStatus(): GuardianStatus {
    if (!supported || !runtime) {return defaultStatus(reason)}
    const config = readJson(configPath)
    const enabled = typeof config?.enabled === 'boolean' ? config.enabled : true
    const raw = readJson(statusPath)
    const supervisorAlive = Boolean(raw && processIdentityAlive(raw.supervisorPid, raw.supervisorStartTime))
    const heartbeatAt = typeof raw?.heartbeatAt === 'string' ? raw.heartbeatAt : null
    const heartbeatTime = heartbeatAt ? Date.parse(heartbeatAt) : NaN
    const heartbeatFresh = Number.isFinite(heartbeatTime) && Date.now() - heartbeatTime <= HEARTBEAT_FRESH_MS
    const appRunning = Boolean(raw && processIdentityAlive(raw.appPid, raw.appStartTime))

    if (supervisorAlive && heartbeatFresh && raw?.version === GUARDIAN_VERSION && raw?.supervisorRunning === true) {
      if (supervisorStartupTimer) {
        clearTimeout(supervisorStartupTimer)
        supervisorStartupTimer = null
      }

      supervisorRestartAttempt = 0

      if (managerError?.startsWith('supervisor_')) {managerError = null}
    }

    const gatewayState = supervisorAlive && heartbeatFresh && typeof raw?.gatewayState === 'string' && [
      'running', 'starting', 'recovering', 'stopped_by_user', 'unavailable', 'not_managed'
    ].includes(raw.gatewayState) ? raw.gatewayState as GuardianStatus['gatewayState'] : 'unknown'

    return {
      supported: true,
      reason: null,
      enabled,
      autostartInstalled: autostartOwned(),
      supervisorRunning: supervisorAlive,
      heartbeatFresh,
      heartbeatAt,
      appRunning,
      appPid: appRunning && Number.isInteger(raw?.appPid) ? Number(raw?.appPid) : null,
      gatewayState,
      gatewayPid: supervisorAlive && heartbeatFresh && Number.isInteger(raw?.gatewayPid) ? Number(raw?.gatewayPid) : null,
      appRestartCount: Number.isInteger(raw?.appRestartCount) ? Number(raw?.appRestartCount) : 0,
      gatewayRestartCount: Number.isInteger(raw?.gatewayRestartCount) ? Number(raw?.gatewayRestartCount) : 0,
      lastError: managerError || (typeof raw?.lastError === 'string' ? raw.lastError : null)
    }
  }

  async function initialize(): Promise<GuardianStatus> {
    if (!supported) {return getStatus()}
    clearSupervisorTimers()

    try {
      fs.unlinkSync(userStopPath)
    } catch {
      // No stop marker on first launch; a stale marker is harmless if absent.
    }

    const enabled = readEnabled()

    if (!writeConfig(enabled)) {return getStatus()}

    if (enabled) {
      installAutostart()
      spawnSupervisor()
    } else {
      clearSupervisorTimers()
      removeAutostart()
      terminateSupervisor()
    }

    await new Promise(resolve => setTimeout(resolve, 250))

    return getStatus()
  }

  async function setEnabled(enabled: boolean): Promise<GuardianStatus> {
    if (!supported) {return getStatus()}
    managerError = null

    if (!writeConfig(enabled)) {return getStatus()}
    clearSupervisorTimers()

    if (enabled) {
      supervisorRestartAttempt = 0
      installAutostart()
      spawnSupervisor()
    } else {
      removeAutostart()
      terminateSupervisor()
    }

    await new Promise(resolve => setTimeout(resolve, 250))

    return getStatus()
  }

  return { initialize, getStatus, setEnabled, stopForUser }
}
