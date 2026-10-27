'use strict'

// AutoDroid 桌面端 Electron 壳：
// - 单实例锁；窗口 1440x900（min 1024x700）；托盘（显示 / 检查更新 / 退出）
// - 以 child_process 启动本地 Python 引擎（只绑 127.0.0.1），
//   解析 stdout 的 AUTODROID_ENGINE_PORT=<n> 后加载 http://127.0.0.1:<n>
// - 退出（含托盘退出）时杀掉引擎
// - 保存登录信息：密码用 safeStorage 加密存 userData/logins.json，明文绝不落盘

const { app, BrowserWindow, Menu, Tray, dialog, nativeImage, shell } = require('electron')
const { spawn } = require('node:child_process')
const fs = require('node:fs')
const path = require('node:path')
const loginStore = require('./login_store')
const updater = require('./updater')

const ENGINE_START_TIMEOUT_MS = 30000
const ENGINE_KILL_GRACE_MS = 3000

let mainWindow = null
let tray = null
let engineProcess = null
let enginePort = null
let quitting = false
// child.killed 只表示「kill 信号已发出」，无法用来判断进程是否真正退出；
// SIGKILL 兜底必须看 exit 标志（由 child.on('exit') 置位）。
let engineExited = true

// ==================== 单实例 ====================

const gotSingleInstanceLock = app.requestSingleInstanceLock()
if (!gotSingleInstanceLock) {
  app.quit()
} else {
  app.on('second-instance', () => {
    showMainWindow()
  })

  app.on('ready', () => {
    bootstrap().catch((error) => {
      dialog.showErrorBox('AutoDroid 启动失败', String((error && error.message) || error))
      quitting = true
      stopEngine()
      app.exit(1)
    })
  })
}

// ==================== 窗口 / 托盘 ====================

function showMainWindow() {
  if (!mainWindow) {
    return
  }
  if (mainWindow.isMinimized()) {
    mainWindow.restore()
  }
  mainWindow.show()
  mainWindow.focus()
}

// 秒开加载页：窗口先于引擎就绪显示，消除「启动后长时间无窗口」的观感。
const LOADING_URL =
  'data:text/html;charset=utf-8,' +
  encodeURIComponent(
    `<!doctype html><html><head><meta charset="utf-8"><title>AutoDroid</title>` +
      `<style>html,body{margin:0;height:100%;background:#0F1626;color:#E9EDF4;` +
      `font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;` +
      `display:flex;flex-direction:column;align-items:center;justify-content:center}` +
      `h1{font-size:22px;font-weight:600;letter-spacing:.08em;margin:18px 0 6px}` +
      `p{color:#8FA0B8;margin:0}.spin{width:36px;height:36px;border-radius:50%;` +
      `border:3px solid #2A3A55;border-top-color:#22C3E6;animation:sp 1s linear infinite}` +
      `@keyframes sp{to{transform:rotate(360deg)}}</style></head><body>` +
      `<div class="spin"></div><h1>AutoDroid</h1><p id="msg">正在启动本地引擎…</p></body></html>`
  )

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 700,
    show: false,
    title: 'AutoDroid',
    icon: path.join(__dirname, 'assets', 'tray.png'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
    },
  })

  // 立即加载本地加载页（data URL 无 IO，毫秒级），ready-to-show 随即触发出窗
  mainWindow.loadURL(LOADING_URL)

  mainWindow.once('ready-to-show', () => {
    mainWindow.show()
  })

  // 外部链接交给系统浏览器，避免在应用窗口里跳出会话
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) {
      shell.openExternal(url)
    }
    return { action: 'deny' }
  })

  // 关闭窗口不退出：常驻托盘，由托盘「退出」或 Cmd/Ctrl+Q 结束
  mainWindow.on('close', (event) => {
    if (!quitting && tray) {
      event.preventDefault()
      mainWindow.hide()
    }
  })
  mainWindow.on('closed', () => {
    mainWindow = null
  })
}

function createTray() {
  const iconPath = path.join(__dirname, 'assets', 'tray.png')
  const image = fs.existsSync(iconPath) ? nativeImage.createFromPath(iconPath) : nativeImage.createEmpty()
  tray = new Tray(image.isEmpty() ? nativeImage.createEmpty() : image)
  tray.setToolTip('AutoDroid')
  const menu = Menu.buildFromTemplate([
    {
      label: '显示主窗口',
      click: () => showMainWindow(),
    },
    {
      label: '检查更新',
      click: () => {
        // 前端收到 autodroid:check-update 后走同一套检查流程（查询/确认/下载）
        showMainWindow()
        updater.notifyCheckUpdateRequested(mainWindow)
      },
    },
    { type: 'separator' },
    {
      label: '退出',
      click: () => {
        quitting = true
        app.quit()
      },
    },
  ])
  tray.setContextMenu(menu)
  tray.on('click', () => showMainWindow())
  tray.on('double-click', () => showMainWindow())
}

// ==================== 引擎管理 ====================

function resolvePythonCommand() {
  if (process.env.AUTODROID_PYTHON) {
    return process.env.AUTODROID_PYTHON
  }
  return process.platform === 'win32' ? 'python' : 'python3'
}

// 内置引擎（PyInstaller onedir，extraResources engine-bin/）优先；开发态可放
// release-engine/ 本地验证；都没有时回退 `python -m engine`。
// onedir 产物是目录（<dir>/<name> + _internal/），onefile 旧布局是单文件——两者都兼容。
function resolveBundledEnginePath() {
  // PyInstaller onedir 布局：目录名恒为 autodroid-engine（不带扩展名），
  // 可执行体在目录内（Windows 下是 autodroid-engine.exe）——不能用同一个
  // name 变量拼目录与文件名，否则 Windows 找不到引擎。
  const exeName = process.platform === 'win32' ? 'autodroid-engine.exe' : 'autodroid-engine'
  const dirName = 'autodroid-engine'
  const candidates = []
  if (app.isPackaged) {
    candidates.push(path.join(process.resourcesPath, 'engine-bin', dirName, exeName))
    candidates.push(path.join(process.resourcesPath, 'engine-bin', exeName))
  }
  candidates.push(path.join(__dirname, 'release-engine', dirName, exeName))
  candidates.push(path.join(__dirname, 'release-engine', exeName))
  for (const candidate of candidates) {
    try {
      if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) {
        return candidate
      }
    } catch (error) {
      // 探测失败按不存在处理
    }
  }
  return null
}

function resolveEngineEnv() {
  const env = Object.assign({}, process.env)
  env.AUTODROID_USER_DATA = app.getPath('userData')
  if (!env.AUTODROID_FRONTEND_DIST) {
    env.AUTODROID_FRONTEND_DIST = app.isPackaged
      ? path.join(process.resourcesPath, 'frontend-dist')
      : path.join(__dirname, '..', 'AutoDroid', 'frontend', 'dist')
  }
  // 打包态资源（fastbot jar / scrcpy-server.jar）在 asar 外的 extraResources
  if (app.isPackaged && !env.AUTODROID_ENGINE_ASSETS_DIR) {
    env.AUTODROID_ENGINE_ASSETS_DIR = path.join(process.resourcesPath, 'assets')
  }
  return env
}

function startEngine() {
  return new Promise((resolve, reject) => {
    const bundled = resolveBundledEnginePath()
    const command = bundled || resolvePythonCommand()
    const args = bundled ? [] : ['-m', 'engine']
    let child
    try {
      child = spawn(command, args, {
        // 打包态 __dirname 位于 app.asar（archive 文件而非目录），作 cwd 会 spawn ENOTDIR；
        // 内置引擎不依赖 cwd，改用真实目录 resourcesPath。开发态保持 __dirname 供
        // `python -m engine` 回退找到 engine 包。
        cwd: app.isPackaged ? process.resourcesPath : __dirname,
        env: resolveEngineEnv(),
        stdio: ['ignore', 'pipe', 'pipe'],
      })
    } catch (error) {
      reject(new Error(`无法启动引擎进程 ${command}: ${error.message}`))
      return
    }
    engineProcess = child
    engineExited = false

    let settled = false
    const timer = setTimeout(() => {
      if (!settled) {
        settled = true
        reject(new Error(`引擎启动超时（${ENGINE_START_TIMEOUT_MS / 1000}s 内未输出 AUTODROID_ENGINE_PORT）`))
      }
    }, ENGINE_START_TIMEOUT_MS)

    const settle = (error, port) => {
      if (settled) {
        return
      }
      settled = true
      clearTimeout(timer)
      if (error) {
        reject(error)
      } else {
        enginePort = port
        resolve(port)
      }
    }

    let stdoutBuffer = ''
    child.stdout.on('data', (chunk) => {
      stdoutBuffer += chunk.toString('utf-8')
      let newline = stdoutBuffer.indexOf('\n')
      while (newline >= 0) {
        const line = stdoutBuffer.slice(0, newline).trim()
        stdoutBuffer = stdoutBuffer.slice(newline + 1)
        const match = /^AUTODROID_ENGINE_PORT=(\d+)$/.exec(line)
        if (match) {
          settle(null, Number(match[1]))
        }
        newline = stdoutBuffer.indexOf('\n')
      }
    })

    child.stderr.on('data', (chunk) => {
      process.stderr.write(`[engine] ${chunk}`)
    })

    child.on('error', (error) => {
      const launchDesc = bundled ? `内置引擎 ${bundled}` : `${command} -m engine`
      settle(new Error(`引擎进程异常（${launchDesc}）: ${error.message}`))
    })

    child.on('exit', (code, signal) => {
      engineExited = true
      if (!settled) {
        const hint = bundled
          ? `内置引擎 ${path.basename(bundled)} 提前退出`
          : '请确认已安装 Python 依赖（pip install -r requirements.txt）'
        settle(new Error(`引擎进程提前退出（code=${code} signal=${signal || 'none'}），${hint}`))
        return
      }
      engineProcess = null
      if (!quitting) {
        dialog.showErrorBox('本地引擎已退出', `引擎进程意外退出（code=${code}），设备同步与 API 转发不可用。`)
      }
    })
  })
}

function stopEngine() {
  const child = engineProcess
  engineProcess = null
  if (!child || engineExited) {
    return
  }
  try {
    if (process.platform === 'win32') {
      spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'])
    } else {
      child.kill('SIGTERM')
      const timer = setTimeout(() => {
        try {
          // child.killed 在 kill() 后恒为 true，不能用来判断存活；
          // 只有 exit 事件没触发（engineExited 仍 false）才升级 SIGKILL。
          if (!engineExited) {
            child.kill('SIGKILL')
          }
        } catch (error) {
          // 进程可能已退出
        }
      }, ENGINE_KILL_GRACE_MS)
      if (timer.unref) {
        timer.unref()
      }
    }
  } catch (error) {
    process.stderr.write(`[engine] 停止失败: ${error.message}\n`)
  }
}

// ==================== 启动流程 ====================

async function bootstrap() {
  loginStore.registerIpc()
  updater.registerIpc()
  createTray()
  createWindow()

  const port = await startEngine()
  if (!mainWindow) {
    return
  }
  process.stderr.write(`[shell] UI <- http://127.0.0.1:${port} (engine pid=${engineProcess ? engineProcess.pid : 'n/a'})\n`)
  await mainWindow.loadURL(`http://127.0.0.1:${port}`)
}

app.on('window-all-closed', () => {
  // 常驻托盘：窗口关完不退出；无托盘时（异常）直接退出
  if (!tray) {
    app.quit()
  }
})

app.on('before-quit', () => {
  quitting = true
})

app.on('will-quit', () => {
  stopEngine()
  if (tray) {
    tray.destroy()
    tray = null
  }
})
