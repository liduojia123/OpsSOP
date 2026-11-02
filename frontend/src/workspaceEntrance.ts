import { reactive } from 'vue'

export const workspaceEntrance = reactive({ phase: 'idle' as 'idle' | 'covered' | 'opening' })
export function coverWorkspace() { workspaceEntrance.phase = 'covered' }
export function openWorkspace() {
  if (workspaceEntrance.phase === 'covered') workspaceEntrance.phase = 'opening'
}
export function cancelWorkspaceEntrance() { workspaceEntrance.phase = 'idle' }
