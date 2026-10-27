'use strict'

// AutoDroid 桌面端 Electron 壳：
// - 单实例锁；窗口 1440x900（min 1024x700）；托盘（显示 / 检查更新 / 退出）
// - 以 child_process 启动本地 Python 引擎（只绑 127.0.0.1），
//   解析 stdout 的 AUTODROID_ENGINE_PORT=<n> 后加载 http://127.0.0.1:<n>
// - 引擎意外退出时指数退避自动重启并重载窗口；最终失败才弹框
// - 显式应用菜单（视图→重新加载）+ Cmd/Ctrl+R + 右键「重新加载」；
//   beforeunload 拦截时弹原生确认框给逃生口（不再静默吞 reload）
// - 退出（含托盘退出）时杀掉引擎
// - 保存登录信息：密码用 safeStorage 加密存 userData/logins.json，明文绝不落盘

const { app, BrowserWindow, Menu, Tray, dialog, nativeImage, shell } = require('electron')
const { spawn } = require('node:child_process')
const fs = require('node:fs')
const path = require('node:path')
const loginStore = require('./login_store')
const updater = require('./updater')
const {
  isReloadShortcut,
  shouldForceReloadAfterUnloadGuard,
  engineRestartDelayMs,
  shouldRetryEngineRestart,
  isEngineRunStable,
  planEngineExitAction,
  planEngineRestartTeardown,
  applyBundledAdbEnv,
  ENGINE_RESTART_MAX_ATTEMPTS,
} = require('./shell_policies')

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
// 引擎自动重启状态（C3）：attempts 计「已执行的自动重启次数」，
// 稳定运行 shell_policies.ENGINE_RESTART_STABLE_MS 后才清零
// （「起来即崩」的循环不重置预算，最终失败能弹出提示框）。
let engineRestartAttempts = 0
let engineRestartTimer = null
let engineRestarting = false
let engineStartedAt = 0
// F1：重启排程/重启在途期间引擎再次退出的记账——不能丢，待本轮
// runEngineRestart 收尾后补一轮重启（否则引擎会静默死透）
let enginePendingExitReason = null

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

  // Cmd/Ctrl+R：页面卡死时的逃生口（菜单加速键之外再兜一层，防止焦点在
  // webview/输入框时菜单加速键被吞）
  mainWindow.webContents.on('before-input-event', (event, input) => {
    if (input && input.type === 'keyDown' && isReloadShortcut(input)) {
      event.preventDefault()
      reloadMainWindow()
    }
  })

  // 右键菜单同样给「重新加载」（无菜单栏/全屏时的鼠标逃生口）
  mainWindow.webContents.on('context-menu', () => {
    const menu = Menu.buildFromTemplate([
      { label: '重新加载', click: () => reloadMainWindow() },
      { type: 'separator' },
      { role: 'cut', label: '剪切' },
      { role: 'copy', label: '复制' },
      { role: 'paste', label: '粘贴' },
      { role: 'selectAll', label: '全选' },
    ])
    menu.popup({ window: mainWindow })
  })

  // beforeunload 拦截时 Electron 默认静默取消 reload（表现为「点不动、只能
  // 重启应用」）。这里弹原生确认框保注意图同时给逃生口：确认=忽略 beforeunload
  // 强制卸载，取消=保持当前页面与未保存的修改。文案按场景区分（重载/退出）。
  mainWindow.webContents.on('will-prevent-unload', (event) => {
    // 退出/关窗流程（before-quit 置位 quitting）用退出文案；其余是重载/导航
    const scenario = quitting ? 'quit' : 'reload'
    if (shouldForceReloadAfterUnloadGuard(dialog, mainWindow, scenario)) {
      // preventDefault() = 忽略 beforeunload 处理器，放行本次 unload
      event.preventDefault()
      return
    }
    // F3：退出流程点「取消」必须复位 quitting——否则本会话关窗进托盘永久失效、
    // 后续点 X 直接杀窗（quit 已被 unload 取消，状态不能留在「退出中」）
    if (scenario === 'quit') {
      quitting = false
    }
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

function reloadMainWindow() {
  // 重新加载主窗口（数据 URL 加载页阶段 reload 也无害）
  if (!mainWindow || mainWindow.isDestroyed()) {
    return
  }
  mainWindow.webContents.reload()
}

// 显式应用菜单：壳必须自带刷新入口（视图→重新加载），
// 页面卡死时用户无需退出重启应用。
function createApplicationMenu() {
  const isMac = process.platform === 'darwin'
  const template = [
    ...(isMac
      ? [{ role: 'appMenu', label: 'AutoDroid' }]
      : []),
    {
      label: '文件',
      submenu: [isMac ? { role: 'close', label: '关闭窗口' } : { role: 'quit', label: '退出' }],
    },
    {
      label: '编辑',
      submenu: [
        { role: 'undo', label: '撤销' },
        { role: 'redo', label: '重做' },
        { type: 'separator' },
        { role: 'cut', label: '剪切' },
        { role: 'copy', label: '复制' },
        { role: 'paste', label: '粘贴' },
        { role: 'selectAll', label: '全选' },
      ],
    },
    {
      label: '视图',
      submenu: [
        {
          label: '重新加载',
          accelerator: 'CmdOrCtrl+R',
          click: () => reloadMainWindow(),
        },
        { type: 'separator' },
        { role: 'resetZoom', label: '实际大小' },
        { role: 'zoomIn', label: '放大' },
        { role: 'zoomOut', label: '缩小' },
        { type: 'separator' },
        { role: 'togglefullscreen', label: '切换全屏' },
      ],
    },
    {
      label: '窗口',
      submenu: [{ role: 'minimize', label: '最小化' }, { role: 'zoom', label: '缩放' }],
    },
  ]
  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
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
  // ADB 解析链：内置 platform-tools 存在才注入 AUTODROID_ADB（引擎侧最高优先级，
  // 解析结果再统一注入 adbutils/uiautomator2 全链路）；已显式设置不覆盖
  applyBundledAdbEnv(env, {
    isPackaged: app.isPackaged,
    resourcesPath: process.resourcesPath,
    appDir: __dirname,
    platform: process.platform,
    existsSync: fs.existsSync,
  })
  return env
}

// G1：丢弃一个引擎子进程——摘除其全部监听并 SIGKILL；
// 仅当它仍是当前引擎时才清 engineProcess/engineExited（不误伤新引擎状态）。
// 否则被丢弃的子进程「迟到退出」时，exit 监听会把 engineProcess 误置 null
//（抹掉新引擎引用）并经 F1 记账给健康引擎补排重启，制造孤儿引擎。
function discardEngineChild(target) {
  try {
    target.removeAllListeners('exit')
    target.removeAllListeners('error')
    if (target.stdout) {
      target.stdout.removeAllListeners('data')
    }
    if (target.stderr) {
      target.stderr.removeAllListeners('data')
    }
  } catch (error) {
    // 流可能已销毁
  }
  if (engineProcess === target) {
    engineProcess = null
    engineExited = true
  }
  try {
    target.kill('SIGKILL')
  } catch (error) {
    // 进程可能已退出
  }
}

function startEngine() {
  return new Promise((resolve, reject) => {
    // G1 残留：顶掉旧引擎前先收尾——runEngineRestart 补排（startEngine 成功但
    // loadURL 真实失败）会再次 startEngine，不收尾仍存活的旧子进程就会留下
    // 孤儿引擎（旧引用被覆盖、进程永不回收）。
    if (engineProcess) {
      process.stderr.write('[shell] 启动新引擎前先收尾旧引擎进程\n')
      discardEngineChild(engineProcess)
    }
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
        // 超时丢弃：kill 子进程并摘监听（见 discardEngineChild 注释）
        discardEngineChild(child)
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
        // 启动失败丢弃：kill 子进程并摘监听（见 discardEngineChild 注释）
        discardEngineChild(child)
        reject(error)
      } else {
        enginePort = port
        engineStartedAt = Date.now()
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
      // G1：先做身份校验——stale 子进程（启动失败已被丢弃/已被新引擎顶掉）的
      // 迟到退出整体忽略，不能清 engineProcess、不能补排重启
      const action = planEngineExitAction({ child, currentProcess: engineProcess, settled, quitting })
      if (action === 'ignore-stale') {
        return
      }
      engineExited = true
      if (action === 'startup-failed') {
        const hint = bundled
          ? `内置引擎 ${path.basename(bundled)} 提前退出`
          : '请确认已安装 Python 依赖（pip install -r requirements.txt）'
        // settle 失败分支内部会 discardEngineChild（清引用 + 摘监听 + kill）
        settle(new Error(`引擎进程提前退出（code=${code} signal=${signal || 'none'}），${hint}`))
        return
      }
      engineProcess = null
      if (action === 'unexpected-exit') {
        handleEngineUnexpectedExit(`code=${code} signal=${signal || 'none'}`)
      }
      // 'ignore-quitting'：退出流程中不再排自动重启
    })
  })
}

function stopEngine() {
  // 主动停止：取消在途的自动重启排程与记账（退出/失败清理路径不再抢跑重启）
  if (engineRestartTimer) {
    clearTimeout(engineRestartTimer)
    engineRestartTimer = null
  }
  enginePendingExitReason = null
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

// ==================== 引擎自动重启 ====================
// 引擎意外退出：指数退避（1s→2s→4s→…封顶 30s，见 shell_policies）自动重启
// 并重载窗口，用户无感自愈；自动重启超上限后才弹最终失败框（原先的
// 「一崩就弹框且只能重启应用」路径）。

function handleEngineUnexpectedExit(reason) {
  if (quitting) {
    // G1：退出流程中不再排自动重启（stopEngine 已清排程与记账，这里兜底拒收）
    return
  }
  if (engineStartedAt && isEngineRunStable(Date.now() - engineStartedAt)) {
    // 稳定运行后才清计数（成功后清零）；「起来即崩」的循环保留计数
    engineRestartAttempts = 0
  }
  engineStartedAt = 0
  if (engineRestartTimer || engineRestarting) {
    // F1：已有排程/重启在途期间引擎又退出——不能丢，记账待本轮
    // runEngineRestart 收尾后补一轮重启
    enginePendingExitReason = reason
    return
  }
  if (!shouldRetryEngineRestart(engineRestartAttempts)) {
    dialog.showErrorBox(
      '本地引擎已退出',
      `引擎进程意外退出（${reason}）且自动重启未成功（已尝试 ${engineRestartAttempts} 次），设备同步与 API 转发不可用。请退出后重新打开 AutoDroid。`
    )
    return
  }
  engineRestartAttempts += 1
  const delayMs = engineRestartDelayMs(engineRestartAttempts)
  process.stderr.write(
    `[shell] 引擎意外退出（${reason}），${delayMs}ms 后自动重启（第 ${engineRestartAttempts}/${ENGINE_RESTART_MAX_ATTEMPTS} 次）\n`
  )
  engineRestartTimer = setTimeout(runEngineRestart, delayMs)
  if (engineRestartTimer.unref) {
    engineRestartTimer.unref()
  }
}

async function runEngineRestart() {
  engineRestartTimer = null
  engineRestarting = true
  let loadError = null
  let startedPort = null
  try {
    startedPort = await startEngine()
    process.stderr.write(`[shell] 引擎已自动重启 UI <- http://127.0.0.1:${startedPort}\n`)
    if (mainWindow && !mainWindow.isDestroyed()) {
      await mainWindow.loadURL(`http://127.0.0.1:${startedPort}`)
    }
  } catch (error) {
    loadError = error
  } finally {
    engineRestarting = false
  }
  // 本轮收尾统一决策（F1×F2，纯函数）：
  // - F2：loadURL ERR_ABORTED = 用户取消重载（引擎已恢复）→ 不算失败、不排连锁重启；
  // - 真实失败要继续退避重试；
  // - F1：重启在途期间引擎二次退出（pending）不能丢，合并进本轮补排一次
  //   （同步段内完成，无事件穿插，不会重复叠排）。
  const pending = enginePendingExitReason
  enginePendingExitReason = null
  const plan = planEngineRestartTeardown({ startedPort, error: loadError, pendingReason: pending })
  if (plan.userCancelled) {
    process.stderr.write('[shell] 用户取消了自动重载，保留在当前页（引擎已恢复）\n')
  }
  if (plan.failure) {
    process.stderr.write(`[shell] 引擎自动重启失败: ${plan.failure}\n`)
  }
  if (plan.shouldReschedule) {
    handleEngineUnexpectedExit(plan.reason)
  }
}

// ==================== 启动流程 ====================

async function bootstrap() {
  loginStore.registerIpc()
  updater.registerIpc()
  createApplicationMenu()
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
