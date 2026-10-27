'use strict'

// window.desktop 桥：客户端模式能力（无此对象 = 纯 Web 行为，前端需优雅降级）
// 密码只在主进程用 safeStorage 加密落盘，preload 不接触文件系统。
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('desktop', {
  /** @returns {Promise<{platform: string, version: string}>} */
  getClientInfo: () => ipcRenderer.invoke('desktop:get-client-info'),

  /** @returns {Promise<{server: string, username: string, password: string, rememberInfo: boolean, autoLogin: boolean, loggedOut: boolean} | null>} */
  getSavedLogin: () => ipcRenderer.invoke('desktop:get-saved-login'),

  /** @param {{server: string, username: string, password: string, rememberInfo: boolean, autoLogin: boolean} | null} data */
  saveLogin: (data) => ipcRenderer.invoke('desktop:save-login', data),

  /** @returns {Promise<void>} */
  markLoggedOut: () => ipcRenderer.invoke('desktop:mark-logged-out'),

  /**
   * 下载更新包到 userData/updates/<fileName> 并拉起安装器（只允许 http(s) URL）。
   * 过程事件经 onUpdateProgress 订阅：{phase:'downloading',percent} | {phase:'done',path} | {phase:'error',message}。
   * @param {{url: string, fileName: string, version?: string}} payload
   * @returns {Promise<{launched: true}>} 失败 reject Error(message)
   */
  installUpdate: async (payload) => {
    const result = await ipcRenderer.invoke('desktop:install-update', payload)
    if (result && result.ok === false) {
      throw new Error(result.message || '更新失败')
    }
    return result
  },

  /**
   * 订阅更新过程事件。
   * @param {(event: {phase: 'downloading', percent: number} | {phase: 'done', path: string} | {phase: 'error', message: string}) => void} cb
   * @returns {() => void} 退订函数
   */
  onUpdateProgress: (cb) => {
    if (typeof cb !== 'function') {
      throw new TypeError('onUpdateProgress 需要函数参数')
    }
    const handler = (_event, payload) => cb(payload)
    ipcRenderer.on('autodroid:update-progress', handler)
    // 注意：removeListener 返回 EventEmitter 本身，跨 contextBridge 不可克隆，
    // 退订函数必须显式返回 undefined。
    return () => {
      ipcRenderer.removeListener('autodroid:update-progress', handler)
    }
  },

  /**
   * 订阅「检查更新」请求（托盘菜单触发），前端走同一套检查流程。
   * @param {() => void} cb
   * @returns {() => void} 退订函数
   */
  onCheckUpdateRequest: (cb) => {
    if (typeof cb !== 'function') {
      throw new TypeError('onCheckUpdateRequest 需要函数参数')
    }
    const handler = () => cb()
    ipcRenderer.on('autodroid:check-update', handler)
    return () => {
      ipcRenderer.removeListener('autodroid:check-update', handler)
    }
  },
})
