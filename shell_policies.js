'use strict'

// AutoDroid 壳策略（纯函数，供 main.js 与 tests/electron_bridge_check.js 共用）：
// - 重新加载快捷键判定（Cmd/Ctrl+R）
// - will-prevent-unload 确认框分支（取消=保留页面 / 确认=强制重载），按场景出文案
// - 引擎自动重启 × beforeunload 竞态判定（ERR_ABORTED=用户取消重载）
// - 引擎意外退出的自动重启退避与最终失败判定
// - 引擎生命周期竞态（G1）：exit 事件分流（stale 子进程忽略）/ 重启收尾补排（F1 合并）
// - 内置 adb（platform-tools）环境注入
// 拆出纯函数的目的：Electron 事件回调难以单测，策略判定可在 check:bridge 断言。

const path = require('node:path')

// ==================== 重新加载 ====================

// Cmd（macOS）或 Ctrl（Windows/Linux）+ R：普通刷新。
// 允许附带 Shift（强制刷新变体），排除 Alt（浏览器/系统保留组合）。
function isReloadShortcut(input) {
  if (!input || typeof input !== 'object') {
    return false
  }
  const key = String(input.key || '').toLowerCase()
  if (key !== 'r') {
    return false
  }
  return Boolean(input.meta || input.ctrl) && !input.alt
}

// ==================== beforeunload 逃生口 ====================

// 文案按场景区分：reload=显式重新加载/导航；quit=退出/关窗（丢弃修改的破坏性不同）
const UNLOAD_GUARD_SCENARIOS = {
  reload: {
    title: '重新加载',
    message: '有未保存的修改，确定重新加载？',
    detail: '重新加载将丢弃未保存的修改。',
    buttons: ['取消', '重新加载'],
  },
  quit: {
    title: '退出',
    message: '有未保存的修改，确定退出？',
    detail: '退出将丢弃未保存的修改。',
    buttons: ['取消', '退出'],
  },
}

// 兼容旧导出：缺省（重载）场景的文案/按钮
const UNLOAD_GUARD_MESSAGE = UNLOAD_GUARD_SCENARIOS.reload.message
const UNLOAD_GUARD_DETAIL = UNLOAD_GUARD_SCENARIOS.reload.detail
// 取消在前、破坏性操作在后：defaultId/cancelId 指向「取消」，回车 = 不卸载
const UNLOAD_GUARD_BUTTONS = UNLOAD_GUARD_SCENARIOS.reload.buttons.slice()

// 确认框参数（scenario: 'reload' | 'quit'，未知场景回退 reload）
function getUnloadGuardDialogOptions(scenario) {
  const preset = UNLOAD_GUARD_SCENARIOS[scenario] || UNLOAD_GUARD_SCENARIOS.reload
  return {
    type: 'warning',
    buttons: preset.buttons.slice(),
    defaultId: 0,
    cancelId: 0,
    title: preset.title,
    message: preset.message,
    detail: preset.detail,
  }
}

// 页面 beforeunload 拦截时 Electron 默认静默取消 reload（壳里没有刷新入口时
// 表现为「整个应用点不动、只能重启」）。这里弹原生确认框给逃生口：
// 返回 true = 用户确认强制卸载（调用方 event.preventDefault() 放行 unload）；
// 返回 false = 取消，保持当前页面与未保存的修改（退出场景调用方需复位 quitting）。
function shouldForceReloadAfterUnloadGuard(dialogApi, win, scenario) {
  const choice = dialogApi.showMessageBoxSync(win, getUnloadGuardDialogOptions(scenario || 'reload'))
  return choice === 1
}

// ==================== 引擎意外退出自动重启 ====================

// 自动重启上限（含封顶 30s 的序列：1s → 2s → 4s → 8s → 16s → 30s）
const ENGINE_RESTART_MAX_ATTEMPTS = 6
const ENGINE_RESTART_BASE_DELAY_MS = 1000
const ENGINE_RESTART_MAX_DELAY_MS = 30000
// 引擎「跑起来了」的判定：稳定运行该时长后才清空失败计数。
// （只认启动成功会把「起来即崩」的循环变成无限自动重启，弹不出最终失败提示。）
const ENGINE_RESTART_STABLE_MS = 30000

// 指数退避：1s → 2s → 4s → 8s → 16s → 30s（封顶）…
// attemptNo 为「第几次自动重启」（1 起）：第 1 次重启前等 1000ms。
function engineRestartDelayMs(attemptNo) {
  const n = Math.max(1, Number(attemptNo) || 1)
  return Math.min(ENGINE_RESTART_BASE_DELAY_MS * 2 ** (n - 1), ENGINE_RESTART_MAX_DELAY_MS)
}

// attemptsSoFar：已执行的自动重启次数；还能再自动重启一次吗。
// 超过上限后交由调用方弹最终失败框（不再静默无限重试）。
function shouldRetryEngineRestart(attemptsSoFar) {
  return (Number(attemptsSoFar) || 0) < ENGINE_RESTART_MAX_ATTEMPTS
}

// 引擎稳定运行（uptimeMs）后才允许清空失败计数。
function isEngineRunStable(uptimeMs) {
  return (Number(uptimeMs) || 0) >= ENGINE_RESTART_STABLE_MS
}

// ==================== 自动重启 × beforeunload 竞态（C3×C2） ====================

// 自动重启后的 loadURL 在脏编辑器页会被 beforeunload 拦截，用户点「取消」时
// loadURL 以 ERR_ABORTED 拒绝——这是「引擎已恢复、用户选择留在当前页」，
// 不是重启失败：不计入失败、不再排连锁重启（否则取消一次就连锁重启没完）。
function isUserCancelledReloadError(error) {
  if (!error) {
    return false
  }
  const message = String((error && error.message) || error)
  return error.code === 'ERR_ABORTED' || error.errno === -3 || /ERR_ABORTED/.test(message)
}

// ==================== 引擎生命周期竞态（G1 / F1） ====================

// child.on('exit') 分流（G1）：stale 子进程（已被新引擎顶掉/启动失败已丢弃）的
// 迟到退出必须整体忽略——不能清 engineProcess（会抹掉新引擎引用）、不能补排
// 重启（会制造孤儿引擎）；启动期退出按启动失败 settle；退出流程不排自动重启。
function planEngineExitAction(state) {
  const { child, currentProcess, settled, quitting } = state
  if (child !== currentProcess) {
    return 'ignore-stale'
  }
  if (!settled) {
    return 'startup-failed'
  }
  if (quitting) {
    return 'ignore-quitting'
  }
  return 'unexpected-exit'
}

// 自动重启一轮收尾决策（F1×F2）：
// - loadURL 失败先判「用户取消重载」（引擎已恢复）：不记失败、不连锁重启；
// - 真实失败要继续退避重试；
// - 重启在途期间引擎二次退出（pendingReason）不能丢，合并进本轮补排一次
//   （合并成单次调用，同步段内完成，不会重复叠排）。
// 返回 { userCancelled, failure, shouldReschedule, reason }。
function planEngineRestartTeardown({ startedPort, error, pendingReason }) {
  const userCancelled = Boolean(startedPort) && isUserCancelledReloadError(error)
  const failure = error && !userCancelled ? String((error && error.message) || error) : null
  const shouldReschedule = Boolean(failure || pendingReason)
  let reason = null
  if (failure) {
    reason = `自动重启失败: ${failure}${pendingReason ? `（期间引擎再次退出: ${pendingReason}）` : ''}`
  } else if (pendingReason) {
    reason = String(pendingReason)
  }
  return { userCancelled, failure, shouldReschedule, reason }
}

// ==================== 内置 adb 注入（ADB 解析链） ====================

// 内置 platform-tools（构建前置 fetch 脚本产出，不入库）：
// 打包态在 resourcesPath/platform-tools/adb[.exe]（extraResources 平铺），
// 开发态在仓库 assets/platform-tools/<darwin|windows>/（分平台目录，fetch 脚本
// 产物；兼容旧扁平布局 assets/platform-tools/）。存在才注入 AUTODROID_ADB
//（引擎侧 find_adb_path 最高优先级），已显式设置时不覆盖；解析结果由引擎统一注入 adbutils。
function platformToolsDirName(platform) {
  return platform === 'win32' ? 'windows' : platform === 'darwin' ? 'darwin' : 'linux'
}

function resolveBundledAdbPath(ctx) {
  const exeName = ctx.platform === 'win32' ? 'adb.exe' : 'adb'
  if (ctx.isPackaged) {
    return path.join(ctx.resourcesPath, 'platform-tools', exeName)
  }
  // 开发态：分平台目录优先，缺了再回退旧扁平布局
  const perPlatform = path.join(ctx.appDir, 'assets', 'platform-tools', platformToolsDirName(ctx.platform), exeName)
  const flat = path.join(ctx.appDir, 'assets', 'platform-tools', exeName)
  if (ctx.existsSync && !ctx.existsSync(perPlatform) && ctx.existsSync(flat)) {
    return flat
  }
  return perPlatform
}

function applyBundledAdbEnv(env, ctx) {
  if (env.AUTODROID_ADB) {
    // 用户显式指定的 adb 优先，不覆盖
    return env
  }
  const candidate = resolveBundledAdbPath(ctx)
  if (ctx.existsSync(candidate)) {
    env.AUTODROID_ADB = candidate
  }
  return env
}

module.exports = {
  isReloadShortcut,
  shouldForceReloadAfterUnloadGuard,
  getUnloadGuardDialogOptions,
  UNLOAD_GUARD_SCENARIOS,
  UNLOAD_GUARD_MESSAGE,
  UNLOAD_GUARD_DETAIL,
  UNLOAD_GUARD_BUTTONS,
  engineRestartDelayMs,
  shouldRetryEngineRestart,
  isEngineRunStable,
  isUserCancelledReloadError,
  planEngineExitAction,
  planEngineRestartTeardown,
  resolveBundledAdbPath,
  platformToolsDirName,
  applyBundledAdbEnv,
  ENGINE_RESTART_MAX_ATTEMPTS,
  ENGINE_RESTART_BASE_DELAY_MS,
  ENGINE_RESTART_MAX_DELAY_MS,
  ENGINE_RESTART_STABLE_MS,
}
