'use strict'

// 应用内「检查更新 / 下载安装」（更新源优先 GitHub Releases，回退平台 App 包管理）：
// - desktop:check-release：查 GitHub 最新 Release（公开仓库，无需 token），按平台/架构
//   选安装包资产，交给前端做版本比较。
// - desktop:install-update：断点续传下载安装包到 userData/updates/<fileName>，
//   过程/结果事件（autodroid:update-progress）推给渲染进程；macOS 下载完成后自动
//   挂载 dmg、校验版本、同卷暂存新 app（desktop:apply-update 时替换并重启）。
// - desktop:apply-update：按已暂存的状态替换应用包并重启（无渲染进程传参，防越权）。
// - desktop:update-status：查询是否有已下载/已暂存的更新（重启入口直接复用）。
// - desktop:download-package：包管理里下载任意安装包（同样断点续传 + 大小校验），
//   隧道截断可续传，落地到系统「下载」目录。
// - 托盘「检查更新」经 autodroid:check-update 通知渲染进程，走同一套检查流程。
// 壳不存储平台登录 token：更新下载 URL 由前端（含 query token）现拼现用，不落盘。
const { app, ipcMain, shell } = require('electron')
const { spawn } = require('node:child_process')
const fs = require('node:fs')
const http = require('node:http')
const https = require('node:https')
const os = require('node:os')
const path = require('node:path')

const PROGRESS_CHANNEL = 'autodroid:update-progress'
const DOWNLOAD_CHANNEL = 'autodroid:download-progress'
const CHECK_UPDATE_CHANNEL = 'autodroid:check-update'
// 进度事件节流间隔（下载过程事件 ~200ms 一发）
const PROGRESS_THROTTLE_MS = 200
// socket 空闲超时：连接/收流长时间无数据即中断，归一为 error 事件
const IDLE_TIMEOUT_MS = 30000
const MAX_REDIRECTS = 5
// 断点续传重试上限：隧道约 100s 掐断大文件，每次续传接着已下载字节继续；
// 只要每轮仍有进展就不轻易放弃（stalled 计数只在"零进展"时累加）
const MAX_DOWNLOAD_ATTEMPTS = 12
const MAX_STALLED_ATTEMPTS = 3
// GitHub Release 查询超时
const RELEASE_QUERY_TIMEOUT_MS = 10000
const RELEASE_API_URL = 'https://api.github.com/repos/liqiu-debug/AutoDroid-Desktop/releases/latest'

// 同一时刻只允许一个下载任务；下载中重复调用直接 reject
let downloading = false
// 已暂存待重启安装的更新（内存态；持久化见 updateStatePath，进程重启后可恢复）
let stagedUpdate = null

function updatesDir() {
  return path.join(app.getPath('userData'), 'updates')
}

function updateStatePath() {
  return path.join(updatesDir(), 'update-state.json')
}

function partPathFor(dest) {
  return `${dest}.part`
}

// 只允许 http(s) 下载地址（file:/ftp:/javascript: 等一律拒绝）
function isHttpUrl(raw) {
  let parsed
  try {
    parsed = new URL(String(raw))
  } catch (error) {
    return false
  }
  return parsed.protocol === 'http:' || parsed.protocol === 'https:'
}

// 安装包文件名：不允许带路径分隔符 / 目录穿越，仅取纯文件名
function isSafeFileName(raw) {
  const name = String(raw || '')
  return name !== '' && name !== '.' && name !== '..' && !/[/\\]/.test(name)
}

function errorMessage(error, fallback) {
  const message = String((error && error.message) || error || '')
  return message || fallback
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

// 跟随有限次重定向（下载端点可能 302 到对象存储），每一跳仍须 http(s)
function getResponse(url, redirectsLeft, headers) {
  return new Promise((resolve, reject) => {
    if (!isHttpUrl(url)) {
      reject(new Error('仅支持 http(s) 下载地址'))
      return
    }
    const parsed = new URL(String(url))
    const mod = parsed.protocol === 'https:' ? https : http
    const requestHeaders = Object.assign(
      { 'user-agent': requestUserAgent() },
      headers || {}
    )
    const req = mod.get(parsed, { timeout: IDLE_TIMEOUT_MS, headers: requestHeaders }, (res) => {
      const status = res.statusCode || 0
      if (status >= 300 && status < 400 && res.headers.location) {
        res.resume()
        if (redirectsLeft <= 0) {
          reject(new Error('下载失败（重定向次数过多）'))
          return
        }
        let next
        try {
          next = new URL(res.headers.location, parsed).toString()
        } catch (error) {
          reject(new Error('下载失败（重定向地址非法）'))
          return
        }
        resolve(getResponse(next, redirectsLeft - 1, headers))
        return
      }
      resolve(res)
    })
    req.on('timeout', () => {
      req.destroy(new Error('下载超时'))
    })
    req.on('error', (error) => {
      reject(new Error(errorMessage(error, '下载失败')))
    })
  })
}

function requestUserAgent() {
  return `AutoDroid-Desktop/${app.getVersion()}`
}

// 状态码是否值得续传重试：网络抖动 / 隧道 5xx 可重试；4xx 为确定性失败
function isRetryableStatus(status) {
  return status === 408 || status === 425 || status === 429 || status >= 500
}

/**
 * 断点续传下载：先落到 `<dest>.part`，完成后改名到 dest。
 *
 * 每次尝试带 `Range: bytes=<已下载字节>-`：
 * - 206：续传，按 content-range 的总长校验；
 * - 200：服务端不支持续传，重头下载；
 * - 416：本地已比服务端大（服务端换了文件）→ 清掉重来。
 * 传输中断/5xx 自动重试：只要每轮仍有字节进展就继续（隧道 ~100s 掐断大文件靠这个
 * 续完）；连续 MAX_STALLED_ATTEMPTS 轮零进展或达到 MAX_DOWNLOAD_ATTEMPTS 才放弃。
 * 完成后按「总字节数」校验完整性（expectedSize 可选，来源可信时作为硬校验）。
 *
 * 进度回调 onProgress({ percent, received, total })：percent 为 0..100，总长未知时为 null
 * （调用方改显示已下载字节）。sizeHint 是近似总长（如包管理按 MB 记录的大小），只在服务端
 * 没声明总长时用来估算百分比，不参与完整性校验。下载完成后补发一次 100%。
 */
async function downloadWithResume(url, dest, options = {}) {
  const expectedSize = Number(options.expectedSize) || 0
  const sizeHint = Number(options.sizeHint) || 0
  const onProgress = typeof options.onProgress === 'function' ? options.onProgress : () => {}
  const partPath = partPathFor(dest)
  fs.mkdirSync(path.dirname(dest), { recursive: true })

  let lastError = null
  let stalled = 0
  let lastSize = -1
  for (let attempt = 1; attempt <= MAX_DOWNLOAD_ATTEMPTS; attempt += 1) {
    let offset = 0
    try {
      offset = fs.statSync(partPath).size
    } catch (error) {
      offset = 0
    }
    if (offset === lastSize) {
      stalled += 1
    } else {
      stalled = 0
    }
    lastSize = offset
    if (stalled >= MAX_STALLED_ATTEMPTS) break
    try {
      const total = await downloadOnce(url, partPath, offset, onProgress, { expectedSize, sizeHint })
      const size = fs.statSync(partPath).size
      const expected = expectedSize || total
      if (expected > 0 && size !== expected) {
        // 少字节：留现场让下一轮 Range 续传；多字节：本地与服务端不一致，清理重来
        if (size > expected) {
          fs.rmSync(partPath, { force: true })
        }
        throw new Error(`下载不完整（${size}/${expected} 字节）`)
      }
      fs.renameSync(partPath, dest)
      // 节流可能吞掉最后几块的进度；macOS 之后还要挂载校验暂存几秒，先把进度落到 100%
      onProgress({ percent: 100, received: size, total: size })
      return { size }
    } catch (error) {
      lastError = error
      if (error && error.permanent) break
      const retryable = !error || error.retryable !== false
      if (!retryable || attempt >= MAX_DOWNLOAD_ATTEMPTS) break
      await sleep(Math.min(8000, 500 * 2 ** (stalled + 1)))
    }
  }
  throw lastError || new Error('下载失败')
}

/**
 * 进度百分比用的总长：服务端声明（content-range / 200 的 content-length）优先；206 没有可解析的
 * content-range 时按「续传起点 + 本段长度」推算；都没有（响应被转成 chunked）时退到调用方给的
 * 精确大小，再退到近似大小（estimated：只做估算，真正下完前不显示 100%）。
 */
function resolveProgressTotal({ declaredTotal, status, offset, contentLength, expectedSize, sizeHint }) {
  if (declaredTotal > 0) return { total: declaredTotal, estimated: false }
  if (status === 206 && contentLength > 0) return { total: offset + contentLength, estimated: false }
  if (expectedSize > 0) return { total: expectedSize, estimated: false }
  if (sizeHint > 0) return { total: sizeHint, estimated: true }
  return { total: 0, estimated: false }
}

/** 下载百分比：总长未知返回 null；近似总长封顶 99（以免没下完就显示 100%）。 */
function progressPercent(received, total, estimated) {
  if (!(total > 0)) return null
  const percent = Math.round((received / total) * 100)
  return Math.min(estimated ? 99 : 100, Math.max(0, percent))
}

// 单次下载尝试：返回服务端声明的总字节数（0=未知），失败抛错并标记是否可续传重试
function downloadOnce(url, partPath, offset, onProgress, sizes = {}) {
  return new Promise((resolve, reject) => {
    const headers = offset > 0 ? { range: `bytes=${offset}-` } : {}
    getResponse(url, MAX_REDIRECTS, headers).then((res) => {
      const status = res.statusCode || 0
      if (status === 416) {
        // 本地已下载 >= 服务端总长：清掉重来（服务端文件可能换了）
        res.resume()
        fs.rmSync(partPath, { force: true })
        const error = new Error('下载失败（本地残留与服务端不一致，已重置）')
        error.retryable = true
        reject(error)
        return
      }
      if (status !== 200 && status !== 206) {
        res.resume()
        const error = new Error(`下载失败（HTTP ${status}）`)
        error.retryable = isRetryableStatus(status)
        error.permanent = !error.retryable
        reject(error)
        return
      }
      const contentRange = String(res.headers['content-range'] || '')
      const rangeTotal = /^bytes\s+\d+-\d+\/(\d+)$/i.exec(contentRange)
      const contentLength = Number(res.headers['content-length']) || 0
      const total = rangeTotal ? Number(rangeTotal[1]) : (status === 200 ? contentLength : 0)
      const restart = status === 200 && offset > 0
      const progress = resolveProgressTotal({
        declaredTotal: total,
        status,
        offset: restart ? 0 : offset,
        contentLength,
        expectedSize: Number(sizes.expectedSize) || 0,
        sizeHint: Number(sizes.sizeHint) || 0,
      })
      const writePath = partPath
      let writable
      try {
        writable = restart
          ? fs.createWriteStream(writePath, { flags: 'w' })
          : fs.createWriteStream(writePath, { flags: 'a' })
      } catch (error) {
        reject(new Error(errorMessage(error, '无法写入下载文件')))
        return
      }
      let received = restart ? 0 : offset
      let lastEmit = 0
      let finished = false
      const fail = (error) => {
        if (finished) return
        finished = true
        res.destroy()
        writable.destroy()
        reject(error)
      }
      res.on('data', (chunk) => {
        received += chunk.length
        const now = Date.now()
        if (now - lastEmit >= PROGRESS_THROTTLE_MS) {
          lastEmit = now
          onProgress({
            percent: progressPercent(received, progress.total, progress.estimated),
            received,
            total: progress.total,
          })
        }
      })
      res.on('error', (error) => {
        fail(new Error(errorMessage(error, '下载失败')))
      })
      writable.on('error', (error) => {
        fail(new Error(errorMessage(error, '无法写入下载文件')))
      })
      writable.on('finish', () => {
        if (finished) return
        finished = true
        resolve(total)
      })
      res.pipe(writable)
    }, (error) => {
      reject(error)
    })
  })
}

// ==================== macOS 内置安装（挂载 → 校验 → 同卷暂存 → 换包重启） ====================

function runCommand(command, args) {
  return new Promise((resolve, reject) => {
    let child
    try {
      child = spawn(command, args, { stdio: ['ignore', 'pipe', 'pipe'] })
    } catch (error) {
      reject(new Error(`${command} 启动失败: ${errorMessage(error)}`))
      return
    }
    let stderr = ''
    if (child.stderr) {
      child.stderr.on('data', (chunk) => {
        stderr += String(chunk)
      })
    }
    child.on('error', (error) => {
      reject(new Error(`${command} 执行失败: ${errorMessage(error)}`))
    })
    child.on('close', (code) => {
      if (code === 0) {
        resolve()
        return
      }
      reject(new Error(`${command} 退出码 ${code}${stderr ? `: ${stderr.trim().slice(0, 200)}` : ''}`))
    })
  })
}

/** 当前运行的应用包路径（.app）；非打包态（开发模式）返回 null。 */
function currentAppBundlePath() {
  if (process.platform !== 'darwin' || !app.isPackaged) return null
  let candidate = path.dirname(String(process.execPath || ''))
  for (let i = 0; i < 4; i += 1) {
    if (candidate.endsWith('.app') && fs.existsSync(candidate)) return candidate
    const parent = path.dirname(candidate)
    if (!parent || parent === candidate) break
    candidate = parent
  }
  return null
}

function findAppInDir(dir) {
  let entries = []
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true })
  } catch (error) {
    return null
  }
  for (const entry of entries) {
    if (entry.isDirectory() && entry.name.endsWith('.app')) {
      return path.join(dir, entry.name)
    }
  }
  return null
}

async function readBundleVersion(appBundle) {
  const infoPlist = path.join(appBundle, 'Contents', 'Info.plist')
  if (!fs.existsSync(infoPlist)) return ''
  return new Promise((resolve) => {
    let output = ''
    let child
    try {
      child = spawn('/usr/libexec/PlistBuddy', ['-c', 'Print CFBundleShortVersionString', infoPlist], {
        stdio: ['ignore', 'pipe', 'ignore'],
      })
    } catch (error) {
      resolve('')
      return
    }
    child.stdout.on('data', (chunk) => {
      output += String(chunk)
    })
    child.on('error', () => resolve(''))
    child.on('close', () => resolve(output.trim()))
  })
}

/**
 * 暂存 macOS 更新：挂载 dmg → 校验包内版本 → ditto 到当前应用同目录隐藏暂存位。
 *
 * 暂存到同卷（同目录）保证 apply 时 mv 是原子改名；任一步失败抛错由调用方回退
 * 「Finder 打开 dmg 手动安装」。
 */
async function stageMacUpdate(dmgPath, expectedVersion) {
  const appPath = currentAppBundlePath()
  if (!appPath) {
    throw new Error('非打包态或不支持的位置，跳过内置安装')
  }
  const targetParent = path.dirname(appPath)
  const mountPoint = fs.mkdtempSync(path.join(os.tmpdir(), 'autodroid-update-'))
  let mounted = false
  let stagedPath = ''
  try {
    await runCommand('/usr/bin/hdiutil', ['attach', '-nobrowse', '-readonly', '-mountpoint', mountPoint, dmgPath])
    mounted = true
    const appInDmg = findAppInDir(mountPoint)
    if (!appInDmg) {
      throw new Error('更新包内未找到应用')
    }
    const bundledVersion = await readBundleVersion(appInDmg)
    const wanted = String(expectedVersion || '').trim()
    if (wanted && bundledVersion && bundledVersion !== wanted) {
      throw new Error(`更新包版本不匹配（包内 ${bundledVersion}，期望 ${wanted}）`)
    }
    stagedPath = path.join(
      targetParent,
      `.${path.basename(appPath, '.app')}-update-${bundledVersion || 'new'}.app`
    )
    fs.rmSync(stagedPath, { recursive: true, force: true })
    await runCommand('/usr/bin/ditto', [appInDmg, stagedPath])
    // 自己下载的文件本不带隔离属性；尽善尽美清一次，失败不影响安装
    try {
      await runCommand('/usr/bin/xattr', ['-dr', 'com.apple.quarantine', stagedPath])
    } catch (error) {
      // 无隔离属性时 xattr 返回非 0，忽略
    }
    return { appPath, stagedPath, version: bundledVersion || wanted }
  } catch (error) {
    if (stagedPath) {
      fs.rmSync(stagedPath, { recursive: true, force: true })
    }
    throw error
  } finally {
    if (mounted) {
      try {
        await runCommand('/usr/bin/hdiutil', ['detach', mountPoint])
      } catch (error) {
        // 卸载失败不影响已完成的暂存；挂载点残留由系统回收
      }
    }
    fs.rmSync(mountPoint, { recursive: true, force: true })
  }
}

function writeUpdateState(state) {
  try {
    fs.mkdirSync(updatesDir(), { recursive: true })
    fs.writeFileSync(updateStatePath(), JSON.stringify(state), { encoding: 'utf-8' })
  } catch (error) {
    // 状态只是「稍后重启安装」的便利开关，落盘失败不影响主流程
  }
}

function readUpdateState() {
  try {
    const raw = JSON.parse(fs.readFileSync(updateStatePath(), 'utf-8'))
    if (!raw || typeof raw !== 'object') return null
    if (!isSafeFileName(raw.fileName) && raw.fileName !== undefined) return null
    if (raw.stagedPath && !fs.existsSync(String(raw.stagedPath))) return null
    if (raw.appPath && !fs.existsSync(String(raw.appPath))) return null
    return raw
  } catch (error) {
    return null
  }
}

function clearUpdateState() {
  try {
    fs.rmSync(updateStatePath(), { force: true })
  } catch (error) {
    // 忽略
  }
}

/**
 * 应用已暂存的更新：等本进程退出后改名换包并重启。
 *
 * 换包在独立 shell 脚本里做（进程已退出才能替换运行中的 app 包）：
 * 备份旧包 → 移入新包 → 成功清备份重启；失败回滚旧包并重启。
 */
function applyStagedUpdate() {
  const state = stagedUpdate || readUpdateState()
  if (!state || !state.stagedPath || !state.appPath) {
    throw new Error('没有待安装的更新')
  }
  if (!fs.existsSync(state.stagedPath) || !fs.existsSync(state.appPath)) {
    clearUpdateState()
    throw new Error('更新文件已失效，请重新下载')
  }
  const scriptPath = path.join(updatesDir(), 'apply-update.sh')
  const backupPath = `${state.appPath}.old-${Date.now()}`
  const script = [
    '#!/bin/sh',
    `while kill -0 ${process.pid} 2>/dev/null; do sleep 0.2; done`,
    `BACKUP='${backupPath}'`,
    `APP='${state.appPath}'`,
    `NEW='${state.stagedPath}'`,
    'if mv "$APP" "$BACKUP" 2>/dev/null; then',
    '  if mv "$NEW" "$APP" 2>/dev/null; then',
    '    rm -rf "$BACKUP"',
    '  else',
    '    mv "$BACKUP" "$APP"',
    '    /usr/bin/open "$APP"',
    '    exit 1',
    '  fi',
    'else',
    '  /usr/bin/open "$APP"',
    '  exit 1',
    'fi',
    '/usr/bin/open "$APP"',
  ].join('\n')
  fs.mkdirSync(updatesDir(), { recursive: true })
  fs.writeFileSync(scriptPath, script, { encoding: 'utf-8', mode: 0o755 })
  const child = spawn('/bin/sh', [scriptPath], { detached: true, stdio: 'ignore' })
  child.unref()
  clearUpdateState()
  stagedUpdate = null
  setTimeout(() => app.quit(), 400)
}

// ==================== 更新源查询（GitHub Releases） ====================

function fetchJson(url) {
  return new Promise((resolve, reject) => {
    const req = https.get(
      url,
      {
        timeout: RELEASE_QUERY_TIMEOUT_MS,
        headers: {
          accept: 'application/vnd.github+json',
          'user-agent': requestUserAgent(),
        },
      },
      (res) => {
        const status = res.statusCode || 0
        if (status !== 200) {
          res.resume()
          reject(new Error(`查询更新源失败（HTTP ${status}）`))
          return
        }
        let body = ''
        res.setEncoding('utf-8')
        res.on('data', (chunk) => {
          body += chunk
        })
        res.on('end', () => {
          try {
            resolve(JSON.parse(body))
          } catch (error) {
            reject(new Error('查询更新源失败（响应格式异常）'))
          }
        })
      }
    )
    req.on('timeout', () => req.destroy(new Error('查询更新源超时')))
    req.on('error', (error) => reject(new Error(errorMessage(error, '查询更新源失败'))))
  })
}

/** 从 release 资产里按平台/架构挑安装包：darwin→dmg（按 arch 选）、win32→Setup exe。 */
function pickReleaseAsset(release, platform = process.platform, arch = process.arch) {
  const assets = Array.isArray(release?.assets) ? release.assets : []
  const names = assets.filter((item) => item && typeof item.name === 'string' && isHttpUrl(item.browser_download_url))
  if (platform === 'darwin') {
    const dmgs = names.filter((item) => item.name.toLowerCase().endsWith('.dmg'))
    if (dmgs.length === 0) return null
    const wanted = arch === 'x64' ? dmgs.find((item) => !/-arm64\.dmg$/i.test(item.name)) : dmgs.find((item) => /-arm64\.dmg$/i.test(item.name))
    return wanted || dmgs[0]
  }
  if (platform === 'win32') {
    return names.find((item) => /\.exe$/i.test(item.name)) || null
  }
  return null
}

/** 查询最新 Release：返回 {version, fileName, url, size} 或抛错（前端回退平台包管理源）。 */
async function checkRelease() {
  const release = await fetchJson(RELEASE_API_URL)
  const tag = String(release?.tag_name || '').trim()
  const version = tag.replace(/^v/i, '')
  if (!version) {
    throw new Error('查询更新源失败（缺少版本号）')
  }
  const asset = pickReleaseAsset(release)
  if (!asset) {
    throw new Error('更新源未提供当前平台的安装包')
  }
  return {
    version,
    fileName: asset.name,
    url: asset.browser_download_url,
    size: Number(asset.size) || 0,
    notes: typeof release?.body === 'string' ? release.body : '',
    // Release 页面地址：两个下载源都失败时提示用户用浏览器手动下载
    pageUrl: typeof release?.html_url === 'string' ? release.html_url : '',
  }
}

// ==================== 安装器拉起 ====================

// 拉起安装器：
// - darwin：先试内置安装（挂载校验 + 同卷暂存，返回 {ready:true} 等前端确认重启）；
//   不支持（非打包态/位置不可写/包异常）时回退 Finder 打开 dmg（现状）。
// - win32：spawn(NSIS 向导)。
function defaultLaunchInstaller(filePath, payload = {}, emit = () => {}) {
  if (process.platform === 'win32') {
    return new Promise((resolve, reject) => {
      let settled = false
      let child
      try {
        child = spawn(filePath, [], { detached: true, stdio: 'ignore' })
      } catch (error) {
        reject(new Error(`无法启动安装程序: ${errorMessage(error, 'spawn 失败')}`))
        return
      }
      child.on('error', (error) => {
        if (!settled) {
          settled = true
          reject(new Error(`无法启动安装程序: ${errorMessage(error, 'spawn 失败')}`))
        }
      })
      child.on('spawn', () => {
        if (!settled) {
          settled = true
          try {
            child.unref()
          } catch (error) {
            // 进程可能已退出
          }
          resolve({ launched: true })
        }
      })
    })
  }
  if (process.platform === 'darwin') {
    const isDmg = String(filePath).toLowerCase().endsWith('.dmg')
    const tryBuiltinInstall = isDmg && app.isPackaged && payload.autoInstall !== false
    const builtin = tryBuiltinInstall
      ? stageMacUpdate(filePath, payload.version).then((staged) => {
          stagedUpdate = {
            version: String(payload.version || staged.version || ''),
            fileName: path.basename(filePath),
            appPath: staged.appPath,
            stagedPath: staged.stagedPath,
            size: payload.expectedSize || 0,
          }
          writeUpdateState(stagedUpdate)
          emit({ phase: 'ready', version: stagedUpdate.version, path: filePath })
          return { ready: true, path: filePath }
        })
      : Promise.reject(new Error('跳过内置安装'))
    return builtin.catch((error) => {
      // 回退：Finder 打开 dmg，由用户手动拖入 /Applications（原行为）
      if (tryBuiltinInstall) {
        emit({ phase: 'downloaded', path: filePath, message: `自动安装不可用（${errorMessage(error)}），已为你打开安装包` })
      }
      return shell.openPath(filePath).then((errorMessageText) => {
        if (errorMessageText) {
          throw new Error(`无法打开安装包: ${errorMessageText}`)
        }
        return { launched: true }
      })
    })
  }
  return Promise.reject(new Error('当前平台不支持自动安装更新'))
}

// ==================== 下载 + 安装主流程 ====================

// payload：{ url, fileName, version, expectedSize?, sizeHint?, autoInstall? }
async function installUpdate(sender, payload, launchInstaller) {
  const data = payload && typeof payload === 'object' ? payload : {}
  const url = data.url
  const fileName = data.fileName
  if (!isHttpUrl(url)) {
    throw new Error('仅支持 http(s) 下载地址')
  }
  if (!isSafeFileName(fileName)) {
    throw new Error('非法的安装包文件名')
  }
  if (downloading) {
    throw new Error('已有下载任务进行中')
  }
  downloading = true
  const emit = (event) => {
    try {
      sender.send(PROGRESS_CHANNEL, event)
    } catch (error) {
      // 窗口已关闭等场景：事件丢失无碍，主流程照常收尾
    }
  }
  const dest = path.join(updatesDir(), fileName)
  let downloaded = false
  try {
    fs.mkdirSync(updatesDir(), { recursive: true })
    await downloadWithResume(url, dest, {
      expectedSize: Number(data.expectedSize) || 0,
      sizeHint: Number(data.sizeHint) || 0,
      onProgress: (progress) => emit({ phase: 'downloading', ...progress }),
    })
    downloaded = true
    const result = await launchInstaller(dest, data, emit)
    if (result && result.ready) {
      // macOS 内置安装：已暂存，等前端确认后 desktop:apply-update 换包重启
      return { launched: false, ready: true, version: data.version || '' }
    }
    emit({ phase: 'done', path: dest })
    return { launched: true }
  } catch (error) {
    // 下载未完成时清掉残包（.part 由下载器按续传策略管理）
    if (!downloaded) {
      try {
        fs.unlinkSync(dest)
      } catch (cleanupError) {
        // 未产生文件时忽略
      }
    }
    const message = errorMessage(error, '更新失败')
    emit({ phase: 'error', message })
    throw new Error(message)
  } finally {
    downloading = false
  }
}

// 包管理下载：断点续传 + 大小校验，落地系统「下载」目录，进度走独立通道
async function downloadPackage(sender, payload) {
  const data = payload && typeof payload === 'object' ? payload : {}
  const url = data.url
  const fileName = data.fileName
  if (!isHttpUrl(url)) {
    throw new Error('仅支持 http(s) 下载地址')
  }
  if (!isSafeFileName(fileName)) {
    throw new Error('非法的安装包文件名')
  }
  if (downloading) {
    throw new Error('已有下载任务进行中')
  }
  downloading = true
  const emit = (event) => {
    try {
      sender.send(DOWNLOAD_CHANNEL, event)
    } catch (error) {
      // 窗口关闭时事件丢失无碍
    }
  }
  const dest = path.join(app.getPath('downloads'), fileName)
  try {
    const { size } = await downloadWithResume(url, dest, {
      expectedSize: Number(data.expectedSize) || 0,
      sizeHint: Number(data.sizeHint) || 0,
      onProgress: (progress) => emit({ phase: 'downloading', ...progress }),
    })
    emit({ phase: 'done', path: dest, size })
    try {
      shell.showItemInFolder(dest)
    } catch (error) {
      // 无法定位文件不影响下载结果
    }
    return { path: dest, size }
  } catch (error) {
    const message = errorMessage(error, '下载失败')
    emit({ phase: 'error', message })
    throw new Error(message)
  } finally {
    downloading = false
  }
}

function notifyCheckUpdateRequested(win) {
  if (win && !win.isDestroyed()) {
    win.webContents.send(CHECK_UPDATE_CHANNEL)
  }
}

// options.launchInstaller 可注入（桥自测 mock 拉起分支），默认按平台拉起安装器。
// handler 不抛异常（否则 Electron 会往 stderr 打 "Error occurred in handler" 堆栈），
// 失败以 {ok:false,message} 回给 preload，由 preload reject Error(message)——渲染进程
// 侧契约不变：成功 resolve {launched:true}，失败 reject Error(message)。
function registerIpc(options) {
  const launchInstaller = (options && options.launchInstaller) || defaultLaunchInstaller
  ipcMain.handle('desktop:install-update', async (event, payload) => {
    try {
      return await installUpdate(event.sender, payload, launchInstaller)
    } catch (error) {
      return { ok: false, message: errorMessage(error, '更新失败') }
    }
  })
  ipcMain.handle('desktop:check-release', async () => {
    try {
      return { ok: true, release: await checkRelease() }
    } catch (error) {
      return { ok: false, message: errorMessage(error, '查询更新源失败') }
    }
  })
  ipcMain.handle('desktop:apply-update', async () => {
    try {
      applyStagedUpdate()
      return { ok: true }
    } catch (error) {
      return { ok: false, message: errorMessage(error, '安装更新失败') }
    }
  })
  ipcMain.handle('desktop:update-status', async () => {
    const state = stagedUpdate || readUpdateState()
    if (!state || !state.stagedPath) return { staged: null }
    return {
      staged: {
        version: String(state.version || ''),
        fileName: String(state.fileName || ''),
        size: Number(state.size) || 0,
      },
    }
  })
  ipcMain.handle('desktop:download-package', async (event, payload) => {
    try {
      return await downloadPackage(event.sender, payload)
    } catch (error) {
      return { ok: false, message: errorMessage(error, '下载失败') }
    }
  })
}

module.exports = {
  CHECK_UPDATE_CHANNEL,
  DOWNLOAD_CHANNEL,
  PROGRESS_CHANNEL,
  applyStagedUpdate,
  checkRelease,
  defaultLaunchInstaller,
  downloadPackage,
  downloadWithResume,
  installUpdate,
  notifyCheckUpdateRequested,
  pickReleaseAsset,
  registerIpc,
  updatesDir,
}
