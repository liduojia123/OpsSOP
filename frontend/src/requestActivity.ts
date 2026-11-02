import { reactive } from 'vue'

export const requestActivity = reactive({ pending: 0, failed: false })
const active = new WeakSet<object>()

export function startRequest(config: object) {
  if (active.has(config)) return
  if (requestActivity.pending === 0) requestActivity.failed = false
  active.add(config)
  requestActivity.pending += 1
}

export function endRequest(config?: object, failed = false) {
  if (!config || !active.delete(config)) return
  requestActivity.failed ||= failed
  requestActivity.pending -= 1
}
