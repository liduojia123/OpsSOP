<script setup lang="ts">
import RequestProgress from './components/RequestProgress.vue'
import WorkspaceEntrance from './components/WorkspaceEntrance.vue'
import { workspaceEntrance } from './workspaceEntrance'
</script>

<template>
  <RequestProgress />
  <div class="ops-app-surface" :inert="workspaceEntrance.phase !== 'idle'" :aria-hidden="workspaceEntrance.phase !== 'idle' ? true : undefined">
    <router-view />
  </div>
  <WorkspaceEntrance />
</template>

<style>
.ops-app-surface { height: 100%; min-height: 0; }
</style>
